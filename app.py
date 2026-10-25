"""飞书 → Comfy Cloud 九宫格海报生成服务。

流程：
飞书自动化「发送 HTTP 请求」→ POST /api/jobs（立刻 202）
  → 后台单 worker：下载产品图 → 调新版 Comfy Cloud 工作流 → 回写结果图

也可以启用轮询模式，由服务主动扫描「待处理」记录，不依赖飞书自动化。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from comfy_engine import ComfyEngine, ComfyImage
from feishu_client import FeishuClient, FeishuError
from prompts import build_prompt

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("poster")

# ---------- 配置 ----------
COMFY_BASE_URL = os.getenv("COMFY_BASE_URL", "https://cloud.comfy.org")
COMFY_API_KEY = os.getenv("COMFY_API_KEY", "")
COMFY_USAGE_SOURCE = os.getenv("COMFY_USAGE_SOURCE", "feishu-poster-demo")
WORKFLOW_PATH = os.getenv("WORKFLOW_PATH", "yuanqi-poster-workflow.json")
WORKFLOW_OUTPUT_NODES = tuple(
    node.strip()
    for node in os.getenv(
        "WORKFLOW_OUTPUT_NODES",
        "68,59,60,61,62,64,63,65,66,67,81",
    ).split(",")
    if node.strip()
)
WORKFLOW_IMAGE_NODE = os.getenv("WORKFLOW_IMAGE_NODE", "11")
WORKFLOW_ACTIVITY_NODE = os.getenv("WORKFLOW_ACTIVITY_NODE", "3761723474387808")
WORKFLOW_SCHEME_NODE = os.getenv("WORKFLOW_SCHEME_NODE", "2565934293372055")
WORKFLOW_PROMPT_NODE = os.getenv("WORKFLOW_PROMPT_NODE", "1705779279442278")
WORKFLOW_CARD_NODE = os.getenv("WORKFLOW_CARD_NODE", "2334937224229421")

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_APP_TOKEN = os.getenv("FEISHU_APP_TOKEN", "")
FEISHU_TABLE_ID = os.getenv("FEISHU_TABLE_ID", "")

API_TOKEN = os.getenv("API_TOKEN", "")

F_PRODUCT = os.getenv("FIELD_PRODUCT_IMAGE", "产品图")
F_REQUIREMENT = os.getenv("FIELD_REQUIREMENT", "需求描述")
F_STYLE = os.getenv("FIELD_STYLE", "风格")
F_SCHEME = os.getenv("FIELD_SCHEME", "方案")
F_PRODUCT_CARD = os.getenv("FIELD_PRODUCT_CARD", "产品卡片")
F_STATUS = os.getenv("FIELD_STATUS", "状态")
F_RESULT = os.getenv("FIELD_RESULT", "结果图")
F_TILES = os.getenv("FIELD_TILES", "九宫格切片")
F_UPSCALED = os.getenv("FIELD_UPSCALED", "放大图")
F_ERROR = os.getenv("FIELD_ERROR", "错误信息")
F_CREATOR = os.getenv("FIELD_CREATOR", "提交人")

S_PENDING = os.getenv("STATUS_PENDING", "待处理")
S_DOING = os.getenv("STATUS_DOING", "生成中")
S_DONE = os.getenv("STATUS_DONE", "已完成")
S_FAILED = os.getenv("STATUS_FAILED", "失败")


# 三类产出分别写回三个附件字段，顺序即飞书里的展示顺序。
# 切片节点按位置排成 1..9：63 是第 6 张、64 是第 5 张。
OUTPUT_LAYOUT: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (F_RESULT, ("68",), "九宫格整图"),
    (F_TILES, ("59", "60", "61", "62", "64", "63", "65", "66", "67"), "九宫格"),
    (F_UPSCALED, ("81",), "放大图"),
)

DEDUP_TTL_S = float(os.getenv("DEDUP_TTL_S", "3600"))

# 轮询模式：不依赖飞书自动化，服务主动扫「待处理」记录。
POLL_ENABLED = os.getenv("POLL_ENABLED", "true").lower() not in {"0", "false", "no"}
POLL_INTERVAL_S = float(os.getenv("POLL_INTERVAL_S", "10"))


# ---------- 数据结构 ----------
@dataclass
class Job:
    job_id: str
    record_id: str
    requirement: str
    style: str
    scheme: str
    product_card: str
    image_token: str
    creator_open_id: str | None = None
    created_at: float = field(default_factory=time.time)


@dataclass
class JobState:
    status: str = "queued"
    error: str = ""
    job_id: str = ""
    updated_at: float = field(default_factory=time.time)


class JobIn(BaseModel):
    record_id: str
    requirement: str | None = None
    style: str | None = None
    scheme: str | None = None
    product_card: str | None = None
    image_token: str | None = None
    record: dict[str, Any] | None = None


# ---------- 全局状态 ----------
queue: asyncio.Queue[Job] = asyncio.Queue()
states: dict[str, JobState] = {}
comfy: ComfyEngine | None = None
feishu: FeishuClient | None = None


def _field_text(value: Any) -> str:
    """把飞书文本、单选对象或用户传入值统一转成字符串。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "name", "value"):
            item = value.get(key)
            if isinstance(item, str):
                return item
    return ""


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
        elif isinstance(value, dict) and value.get("file_token"):
            candidates.append(str(value["file_token"]))
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


def _mark_state(state: JobState, status: str, error: str = "") -> None:
    state.status = status
    state.error = error
    state.updated_at = time.time()


def _prune_states() -> None:
    now = time.time()
    expired = [
        key
        for key, value in states.items()
        if value.status in {"done", "failed"} and now - value.updated_at > DEDUP_TTL_S
    ]
    for key in expired:
        states.pop(key, None)


def _job_seed(record_id: str) -> int:
    digest = hashlib.sha256(record_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % 2_147_483_647


async def _upload_group(record_id: str, prefix: str, images: list[ComfyImage]) -> list[dict[str, str]]:
    """上传一组图片，文件名带序号，便于在飞书附件里按序查看。"""
    assert feishu is not None
    attachments: list[dict[str, str]] = []
    for index, image in enumerate(images, start=1):
        suffix = "" if len(images) == 1 else f"-{index}"
        filename = f"{prefix}{suffix}-{record_id}.png"
        token = await feishu.upload_media(image.image_bytes, filename)
        attachments.append({"file_token": token})
    return attachments


# ---------- worker ----------
async def run_job(job: Job) -> None:
    assert feishu is not None and comfy is not None
    state = states.setdefault(job.record_id, JobState(job_id=job.job_id))
    try:
        _mark_state(state, "running")
        await feishu.update_record(job.record_id, {F_STATUS: S_DOING, F_ERROR: ""})

        image_bytes = await feishu.download_media(job.image_token)
        prompt_text, template = build_prompt(job.style, job.requirement, job.scheme)
        result = await comfy.generate(
            image_bytes=image_bytes,
            filename=f"{job.record_id}.jpg",
            prompt_text=prompt_text,
            template=template,
            product_card=job.product_card,
            seed=_job_seed(job.record_id),
        )

        fields: dict[str, Any] = {F_STATUS: S_DONE, F_ERROR: ""}
        summary: list[str] = []
        for field_name, node_ids, prefix in OUTPUT_LAYOUT:
            images = [result.images[node] for node in node_ids if node in result.images]
            if not images:
                log.warning("job %s: no image for field %s", job.job_id, field_name)
                continue
            fields[field_name] = await _upload_group(job.record_id, prefix, images)
            summary.append(f"{field_name}={len(images)}")

        if not summary:
            raise ComfyError("job produced no image for any configured output field")

        await feishu.update_record(job.record_id, fields)
        _mark_state(state, "done")
        log.info("job %s done in %.1fs (%s)", job.job_id, result.elapsed_s, ", ".join(summary))

        if job.creator_open_id:
            try:
                await feishu.send_text(job.creator_open_id, f"你的海报已生成完成（{template.label}），请到多维表格查看。")
            except FeishuError as exc:
                log.warning("job %s completed but notification failed: %s", job.job_id, exc)
    except Exception as exc:  # noqa: BLE001 - 单任务失败必须回写并保留 worker
        error = str(exc)[:500] or exc.__class__.__name__
        _mark_state(state, "failed", error)
        log.error("job %s failed: %s", job.job_id, error)
        try:
            await feishu.update_record(job.record_id, {F_STATUS: S_FAILED, F_ERROR: error})
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


async def poller() -> None:
    """轮询多维表格里「待处理」的记录，免掉飞书自动化配置。"""
    if not POLL_ENABLED:
        log.info("poller disabled")
        return
    assert feishu is not None
    log.info("poller started, interval=%.0fs", POLL_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_S)
            pending = await feishu.list_pending(F_STATUS, S_PENDING)
            for item in pending:
                record_id = item["record_id"]
                if not record_id:
                    continue
                state = states.get(record_id)
                if state and state.status in {"queued", "running", "done"}:
                    continue
                fields = item["fields"]
                try:
                    image_token = _extract_file_token(None, fields)
                except ValueError:
                    continue
                job = Job(
                    job_id=str(uuid.uuid4()),
                    record_id=record_id,
                    requirement=_field_text(fields.get(F_REQUIREMENT)),
                    style=_field_text(fields.get(F_STYLE)),
                    scheme=_field_text(fields.get(F_SCHEME)),
                    product_card=_field_text(fields.get(F_PRODUCT_CARD)),
                    image_token=image_token,
                    creator_open_id=_creator_open_id(fields),
                )
                states[record_id] = JobState(status="queued", job_id=job.job_id)
                await queue.put(job)
                log.info("polled job %s record=%s", job.job_id, record_id)
        except FeishuError as exc:
            log.warning("poller feishu error: %s", exc)
        except Exception:  # noqa: BLE001
            log.exception("poller crashed, will retry")


# ---------- 应用 ----------
@asynccontextmanager
async def lifespan(_app: FastAPI):
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
    comfy = ComfyEngine(
        COMFY_BASE_URL,
        COMFY_API_KEY,
        COMFY_USAGE_SOURCE,
        WORKFLOW_PATH,
        output_nodes=WORKFLOW_OUTPUT_NODES,
        image_node=WORKFLOW_IMAGE_NODE,
        activity_node=WORKFLOW_ACTIVITY_NODE,
        scheme_node=WORKFLOW_SCHEME_NODE,
        card_node=WORKFLOW_CARD_NODE,
        prompt_node=WORKFLOW_PROMPT_NODE,
    )
    feishu = FeishuClient(FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_APP_TOKEN, FEISHU_TABLE_ID)
    task_worker = asyncio.create_task(worker())
    task_poller = asyncio.create_task(poller())
    log.info(
        "service started workflow=%s output_nodes=%s fields=%s",
        comfy.workflow_name,
        ",".join(WORKFLOW_OUTPUT_NODES),
        "/".join(field for field, _, _ in OUTPUT_LAYOUT),
    )
    try:
        yield
    finally:
        for task in (task_worker, task_poller):
            task.cancel()
        await asyncio.gather(task_worker, task_poller, return_exceptions=True)
        await comfy.aclose()
        await feishu.aclose()
        comfy = None
        feishu = None


app = FastAPI(title="Feishu Comfy Poster", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "queued": queue.qsize(),
        "jobs": len(states),
        "workflow": comfy.workflow_name if comfy else WORKFLOW_PATH,
        "output_nodes": list(WORKFLOW_OUTPUT_NODES),
        "fields": {
            "整图": F_RESULT,
            "切片": F_TILES,
            "放大图": F_UPSCALED,
        },
    }


@app.post("/api/jobs")
async def create_job(payload: JobIn, authorization: str | None = Header(default=None)):
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

    record_style = _field_text(record.get(F_STYLE)) if record else ""
    record_scheme = _field_text(record.get(F_SCHEME)) if record else ""
    record_requirement = _field_text(record.get(F_REQUIREMENT)) if record else ""
    record_card = _field_text(record.get(F_PRODUCT_CARD)) if record else ""
    try:
        image_token = _extract_file_token(payload.image_token, record)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = Job(
        job_id=str(uuid.uuid4()),
        record_id=payload.record_id,
        requirement=payload.requirement or record_requirement,
        style=payload.style or record_style,
        scheme=payload.scheme or record_scheme,
        product_card=payload.product_card or record_card,
        image_token=image_token,
        creator_open_id=_creator_open_id(record),
    )
    states[payload.record_id] = JobState(status="queued", job_id=job.job_id)
    await queue.put(job)
    log.info("queued job %s record=%s style=%s scheme=%s", job.job_id, job.record_id, job.style or "(默认)", job.scheme or "(默认)")
    return JSONResponse(status_code=202, content={"job_id": job.job_id})


@app.get("/api/jobs/{record_id}")
async def job_status(record_id: str) -> dict[str, Any]:
    state = states.get(record_id)
    if state is None:
        raise HTTPException(status_code=404, detail="unknown record")
    return {"record_id": record_id, "status": state.status, "error": state.error, "job_id": state.job_id}
