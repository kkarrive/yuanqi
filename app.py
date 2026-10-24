"""飞书 → Comfy Cloud 海报生成服务。

流程：
飞书自动化「发送 HTTP 请求」→ POST /api/jobs（立刻 202）
  → 后台单 worker：下载产品图 → 调 Comfy Cloud 出图 → 上传素材 → 回写记录 → 发消息

设计取舍：
- 内存队列 + 单 worker：Demo 足够，生产换 Redis + 多 worker。
- 按 record_id 幂等：飞书自动化可能重试，重复请求只做一次。
- 立刻返回：飞书 HTTP 节点有超时，出图要几十秒，必须异步。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from comfy_engine import ComfyEngine, ComfyError
from feishu_client import FeishuClient, FeishuError
from prompts import build_prompt

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("poster")

# ---------- 配置 ----------
COMFY_BASE_URL = os.getenv("COMFY_BASE_URL", "https://cloud.comfy.org")
COMFY_API_KEY = os.getenv("COMFY_API_KEY", "")
COMFY_USAGE_SOURCE = os.getenv("COMFY_USAGE_SOURCE", "feishu-poster-demo")

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_APP_TOKEN = os.getenv("FEISHU_APP_TOKEN", "")
FEISHU_TABLE_ID = os.getenv("FEISHU_TABLE_ID", "")

API_TOKEN = os.getenv("API_TOKEN", "")

F_PRODUCT = os.getenv("FIELD_PRODUCT_IMAGE", "产品图")
F_REQUIREMENT = os.getenv("FIELD_REQUIREMENT", "需求描述")
F_STYLE = os.getenv("FIELD_STYLE", "风格")
F_STATUS = os.getenv("FIELD_STATUS", "状态")
F_RESULT = os.getenv("FIELD_RESULT", "结果图")
F_ERROR = os.getenv("FIELD_ERROR", "错误信息")
F_CREATOR = os.getenv("FIELD_CREATOR", "提交人")

S_PENDING = os.getenv("STATUS_PENDING", "待处理")
S_DOING = os.getenv("STATUS_DOING", "生成中")
S_DONE = os.getenv("STATUS_DONE", "已完成")
S_FAILED = os.getenv("STATUS_FAILED", "失败")

MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "1"))
DEDUP_TTL_S = float(os.getenv("DEDUP_TTL_S", "3600"))


# ---------- 数据结构 ----------
@dataclass
class Job:
    job_id: str
    record_id: str
    requirement: str
    style: str
    image_token: str
    created_at: float = field(default_factory=time.time)


@dataclass
class JobState:
    status: str = "queued"
    error: str = ""
    job_id: str = ""


class JobIn(BaseModel):
    record_id: str
    requirement: str | None = None
    style: str | None = None
    image_token: str | None = None
    record: dict[str, Any] | None = None


# ---------- 全局状态 ----------
queue: asyncio.Queue[Job] = asyncio.Queue()
states: dict[str, JobState] = {}
comfy: ComfyEngine | None = None
feishu: FeishuClient | None = None


def _extract_file_token(raw: str | None, record: dict[str, Any] | None) -> str:
    """从入参或多维表格记录里取第一个附件的 file_token。"""
    candidates: list[str] = []
    if raw:
        candidates.append(raw)
    if record:
        value = record.get(F_PRODUCT)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item.get("file_token"):
                    candidates.append(str(item["file_token"]))
        elif isinstance(value, str):
            candidates.append(value)
    for text in candidates:
        text = text.strip()
        if not text:
            continue
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                    token = parsed[0].get("file_token")
                    if token:
                        return str(token)
            except json.JSONDecodeError:
                pass
        return text
    raise ValueError("未能从请求或多维表格记录中解析出产品图 file_token")


def _creator_open_id(record: dict[str, Any] | None) -> str | None:
    if not record:
        return None
    value = record.get(F_CREATOR)
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0].get("id") or value[0].get("open_id")
    if isinstance(value, dict):
        return value.get("id") or value.get("open_id")
    if isinstance(value, str) and value.startswith("ou_"):
        return value
    return None


def _prune_states() -> None:
    now = time.time()
    for key in [k for k, v in states.items() if v.status in {"done", "failed"} and now - getattr(v, "ts", now) > DEDUP_TTL_S]:
        states.pop(key, None)


# ---------- worker ----------
async def run_job(job: Job) -> None:
    assert feishu is not None and comfy is not None
    state = states.setdefault(job.record_id, JobState())
    try:
        state.status = "running"
        await feishu.update_record(job.record_id, {F_STATUS: S_DOING, F_ERROR: ""})

        image_bytes = await feishu.download_media(job.image_token)
        prompt_text, template = build_prompt(job.style, job.requirement)

        result = await comfy.generate(
            image_bytes=image_bytes,
            filename=f"{job.record_id}.jpg",
            prompt_text=prompt_text,
            template=template,
            seed=abs(hash(job.record_id)) % 2_147_483_647,
        )

        file_token = await feishu.upload_media(result.image_bytes, f"poster-{job.record_id}.png")
        await feishu.update_record(
            job.record_id,
            {
                F_STATUS: S_DONE,
                F_RESULT: [{"file_token": file_token}],
                F_ERROR: "",
            },
        )
        state.status = "done"
        log.info("job %s done in %.1fs (%sx%s)", job.job_id, result.elapsed_s, result.width, result.height)

        open_id = _creator_open_id(None)
        if open_id:
            await feishu.send_text(open_id, f"你的海报已生成完成（{template.label}），请到多维表格查看。")
    except (FeishuError, ComfyError, ValueError) as exc:
        state.status = "failed"
        state.error = str(exc)[:500]
        log.error("job %s failed: %s", job.job_id, exc)
        try:
            await feishu.update_record(job.record_id, {F_STATUS: S_FAILED, F_ERROR: state.error})
        except FeishuError:
            log.error("failed to write error back to record %s", job.record_id)


async def worker() -> None:
    while True:
        job = await queue.get()
        try:
            await run_job(job)
        except Exception:  # noqa: BLE001 - worker 不能因为单任务异常退出
            log.exception("worker crashed on job %s", job.job_id)
        finally:
            queue.task_done()


# ---------- 应用 ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global comfy, feishu
    missing = [
        name
        for name, value in {
            "COMFY_API_KEY": COMFY_API_KEY,
            "FEISHU_APP_ID": FEISHU_APP_ID,
            "FEISHU_APP_SECRET": FEISHU_APP_SECRET,
            "FEISHU_APP_TOKEN": FEISHU_APP_TOKEN,
            "FEISHU_TABLE_ID": FEISHU_TABLE_ID,
        }.items()
        if not value
    ]
    if missing:
        log.warning("缺少环境变量: %s", ", ".join(missing))
    comfy = ComfyEngine(COMFY_BASE_URL, COMFY_API_KEY, COMFY_USAGE_SOURCE)
    feishu = FeishuClient(FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_APP_TOKEN, FEISHU_TABLE_ID)
    task = asyncio.create_task(worker())
    log.info("service started")
    try:
        yield
    finally:
        task.cancel()
        await comfy.aclose()
        await feishu.aclose()


app = FastAPI(title="Feishu Comfy Poster", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "queued": queue.qsize(), "jobs": len(states)}


@app.post("/api/jobs")
async def create_job(payload: JobIn, request: Request, authorization: str | None = Header(default=None)):
    if API_TOKEN:
        expected = f"Bearer {API_TOKEN}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="invalid token")

    _prune_states()

    existing = states.get(payload.record_id)
    if existing and existing.status in {"queued", "running", "done"}:
        return JSONResponse(status_code=202, content={"job_id": existing.job_id, "duplicate": True})

    record = payload.record
    if record is None:
        try:
            record = await feishu.get_record(payload.record_id) if feishu else None
        except FeishuError as exc:
            log.warning("读取记录失败，改用请求参数: %s", exc)

    try:
        image_token = _extract_file_token(payload.image_token, record)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    style = payload.style
    requirement = payload.requirement
    if record:
        style = style or (record.get(F_STYLE) if isinstance(record.get(F_STYLE), str) else None)
        requirement = requirement or (record.get(F_REQUIREMENT) if isinstance(record.get(F_REQUIREMENT), str) else None)

    job = Job(
        job_id=str(uuid.uuid4()),
        record_id=payload.record_id,
        requirement=requirement or "",
        style=style or "",
        image_token=image_token,
    )
    states[payload.record_id] = JobState(status="queued", job_id=job.job_id)
    await queue.put(job)
    log.info("queued job %s record=%s style=%s", job.job_id, job.record_id, job.style or "(默认)")
    return JSONResponse(status_code=202, content={"job_id": job.job_id})


@app.get("/api/jobs/{record_id}")
async def job_status(record_id: str) -> dict[str, Any]:
    state = states.get(record_id)
    if state is None:
        raise HTTPException(status_code=404, detail="unknown record")
    return {"record_id": record_id, "status": state.status, "error": state.error, "job_id": state.job_id}
