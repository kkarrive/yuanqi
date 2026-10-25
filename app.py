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
from prompts import build_prompt, resolve_font, resolve_visual

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
WORKFLOW_VISUAL_NODE = os.getenv("WORKFLOW_VISUAL_NODE", "705857498768221")
WORKFLOW_FONT_NODE = os.getenv("WORKFLOW_FONT_NODE", "4105141153397020")
WORKFLOW_COPY_NODE = os.getenv("WORKFLOW_COPY_NODE", "3286967850890428")

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
F_VISUAL = os.getenv("FIELD_VISUAL", "画面风格")
F_FONT = os.getenv("FIELD_FONT", "字体风格")
F_COPY = os.getenv("FIELD_COPY", "卖点文案")
F_SUBMIT = os.getenv("FIELD_SUBMIT", "提交生成")
F_PARAMS = os.getenv("FIELD_PARAMS", "生成参数")
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

# 显式提交：填表人把「提交生成」选成 V_SUBMIT_READY 才会被扫到；开工即改成 V_SUBMIT_DONE
V_SUBMIT_READY = os.getenv("SUBMIT_READY", "提交")
V_SUBMIT_DONE = os.getenv("SUBMIT_DONE", "已提交")


# 三类产出分别写回三个附件字段，顺序即飞书里的展示顺序。
# 切片节点按位置排成 1..9：63 是第 6 张、64 是第 5 张。
OUTPUT_LAYOUT: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (F_RESULT, ("68",), "九宫格整图"),
    (F_TILES, ("59", "60", "61", "62", "64", "63", "65", "66", "67"), "九宫格"),
    (F_UPSCALED, ("81",), "放大图"),
)

DEDUP_TTL_S = float(os.getenv("DEDUP_TTL_S", "3600"))
SETTLE_SECONDS = float(os.getenv("SETTLE_SECONDS", "15"))

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
    visual: str
    font: str
    copy_text: str
    image_token: str
    creator_open_id: str | None = None
    snapshot: str = ""
    attempts: int = 0
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
    visual: str | None = None
    font: str | None = None
    copy_text: str | None = None
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


_SNAPSHOT_FIELDS = (
    F_PRODUCT, F_REQUIREMENT, F_STYLE, F_SCHEME,
    F_PRODUCT_CARD, F_VISUAL, F_FONT, F_COPY,
)


def _record_snapshot(fields: dict[str, Any]) -> str:
    """把参与生成的字段原始值压成一个哈希，用于提交前二次校验。"""
    payload = {name: fields.get(name) for name in _SNAPSHOT_FIELDS}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _settled(last_modified_time: Any) -> tuple[bool, float]:
    """判断记录是否已停止编辑：返回 (是否稳定, 静置秒数)。"""
    if not last_modified_time:
        return True, float("inf")
    try:
        age = time.time() - float(last_modified_time) / 1000.0
    except (TypeError, ValueError):
        return True, float("inf")
    return age >= SETTLE_SECONDS, age

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
class JobDeferred(RuntimeError):
    """记录在排队后被继续编辑：放弃本次，稍后重试。"""


def _params_line(job: Job, prompt_id: str) -> str:
    """把本次实际用到的输入来源写回记录，便于自查。"""
    return (
        f"产品图={job.record_id}.jpg（飞书）"
        f"｜卡片={'飞书' if job.product_card else '内置'}"
        f"｜文案={'飞书' if job.copy_text else '内置'}"
        f"｜风格={job.style or '默认'} 方案={job.scheme or '默认'}"
        f" 画面={job.visual or '默认'} 字体={job.font or '默认'}"
        f"｜snapshot={job.snapshot or '-'}｜seed={_job_seed(job.record_id)}"
        f"｜prompt_id={prompt_id}"
    )


async def run_job(job: Job) -> None:
    assert feishu is not None and comfy is not None
    state = states.setdefault(job.record_id, JobState(job_id=job.job_id))

    # 提交前二次校验：排队后被改过就放弃本次（不计失败，交由 worker 重排）
    if job.snapshot:
        detail = await feishu.get_record_detail(job.record_id)
        current = _record_snapshot(detail["fields"])
        if current != job.snapshot:
            raise JobDeferred(f"snapshot {job.snapshot} -> {current}")

    try:
        _mark_state(state, "running")
        await feishu.update_record(
            job.record_id,
            {F_STATUS: S_DOING, F_SUBMIT: V_SUBMIT_DONE, F_ERROR: ""},
        )

        image_bytes = await feishu.download_media(job.image_token)
        prompt_text, template = build_prompt(job.style, job.requirement, job.scheme)
        result = await comfy.generate(
            image_bytes=image_bytes,
            filename=f"{job.record_id}.jpg",
            prompt_text=prompt_text,
            template=template,
            product_card=job.product_card,
            visual_index=resolve_visual(job.visual),
            font_index=resolve_font(job.font),
            copy_text=job.copy_text,
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

        fields[F_PARAMS] = _params_line(job, result.prompt_id)
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
        deferred = False
        try:
            await run_job(job)
        except JobDeferred as exc:
            job.attempts += 1
            if job.attempts <= 3:
                log.warning("job %s deferred (%s), retry %d/3", job.job_id, exc, job.attempts)
                states[job.record_id] = JobState(status="queued", job_id=job.job_id)
                deferred = True
            else:
                log.error("job %s keeps changing, giving up: %s", job.job_id, exc)
                states[job.record_id] = JobState(
                    status="failed", error="记录在提交前仍在变动，已放弃本次", job_id=job.job_id
                )
        except Exception:  # noqa: BLE001 - worker 不能因为单任务异常退出
            log.exception("worker crashed on job %s", job.job_id)
        finally:
            queue.task_done()
        if deferred:
            await asyncio.sleep(max(SETTLE_SECONDS, POLL_INTERVAL_S))
            await queue.put(job)


async def poller() -> None:
    """只扫「提交生成 = 提交」的记录，且要求记录静置够久，避免边填边跑。"""
    if not POLL_ENABLED:
        log.info("poller disabled")
        return
    assert feishu is not None
    log.info(
        "poller started interval=%.0fs trigger=%s=%s settle=%.0fs",
        POLL_INTERVAL_S, F_SUBMIT, V_SUBMIT_READY, SETTLE_SECONDS,
    )
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_S)
            pending = await feishu.search_records(F_SUBMIT, V_SUBMIT_READY)
            for item in pending:
                record_id = item["record_id"]
                if not record_id:
                    continue
                state = states.get(record_id)
                if state and state.status in {"queued", "running", "done"}:
                    continue
                detail = await feishu.get_record_detail(record_id)
                fields = detail["fields"]
                settled, age = _settled(detail.get("last_modified_time"))
                if not settled:
                    log.info("record %s still editing (%.0fs < %.0fs), skip", record_id, age, SETTLE_SECONDS)
                    continue
                try:
                    image_token = _extract_file_token(None, fields)
                except ValueError:
                    log.warning("record %s has no product image, skip", record_id)
                    continue
                job = Job(
                    job_id=str(uuid.uuid4()),
                    record_id=record_id,
                    requirement=_field_text(fields.get(F_REQUIREMENT)),
                    style=_field_text(fields.get(F_STYLE)),
                    scheme=_field_text(fields.get(F_SCHEME)),
                    product_card=_field_text(fields.get(F_PRODUCT_CARD)),
                    visual=_field_text(fields.get(F_VISUAL)),
                    font=_field_text(fields.get(F_FONT)),
                    copy_text=_field_text(fields.get(F_COPY)),
                    image_token=image_token,
                    creator_open_id=_creator_open_id(fields),
                    snapshot=_record_snapshot(fields),
                )
                states[record_id] = JobState(status="queued", job_id=job.job_id)
                await queue.put(job)
                log.info(
                    "polled job %s record=%s settled=%.0fs snapshot=%s card=%s copy=%s",
                    job.job_id, record_id, age, job.snapshot,
                    "feishu" if job.product_card else "builtin",
                    "feishu" if job.copy_text else "builtin",
                )
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
        visual_node=WORKFLOW_VISUAL_NODE,
        font_node=WORKFLOW_FONT_NODE,
        copy_node=WORKFLOW_COPY_NODE,
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
        "workflow_nodes": comfy.workflow_node_count if comfy else 0,
        "output_nodes": list(WORKFLOW_OUTPUT_NODES),
        "fields": {
            "整图": F_RESULT,
            "切片": F_TILES,
            "放大图": F_UPSCALED,
            "画面风格": F_VISUAL,
            "字体风格": F_FONT,
            "卖点文案": F_COPY,
        },
        "trigger": {
            "field": F_SUBMIT,
            "ready": V_SUBMIT_READY,
            "done": V_SUBMIT_DONE,
            "settle_seconds": SETTLE_SECONDS,
            "params_field": F_PARAMS,
        },
    }


def _check_auth(authorization: str | None) -> None:
    if API_TOKEN and authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="invalid token")


@app.post("/api/jobs")
async def create_job(payload: JobIn, authorization: str | None = Header(default=None)):
    _check_auth(authorization)

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
    record_visual = _field_text(record.get(F_VISUAL)) if record else ""
    record_font = _field_text(record.get(F_FONT)) if record else ""
    record_copy = _field_text(record.get(F_COPY)) if record else ""
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
        visual=payload.visual or record_visual,
        font=payload.font or record_font,
        copy_text=payload.copy_text or record_copy,
        image_token=image_token,
        creator_open_id=_creator_open_id(record),
    )
    states[payload.record_id] = JobState(status="queued", job_id=job.job_id)
    await queue.put(job)
    log.info(
        "queued job %s record=%s style=%s scheme=%s visual=%s font=%s copy=%s",
        job.job_id,
        job.record_id,
        job.style or "(默认)",
        job.scheme or "(默认)",
        job.visual or "(默认)",
        job.font or "(默认)",
        "有" if job.copy_text else "(内置)",
    )
    return JSONResponse(status_code=202, content={"job_id": job.job_id})


@app.get("/api/jobs/{record_id}")
async def job_status(record_id: str) -> dict[str, Any]:
    state = states.get(record_id)
    if state is None:
        raise HTTPException(status_code=404, detail="unknown record")
    return {"record_id": record_id, "status": state.status, "error": state.error, "job_id": state.job_id}


class PreviewIn(BaseModel):
    record_id: str


@app.post("/api/jobs/preview")
async def preview_job(payload: PreviewIn, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """dry-run：只解析记录并构造工作流，不提交 Comfy、不消耗额度。"""
    _check_auth(authorization)
    if feishu is None or comfy is None:
        raise HTTPException(status_code=503, detail="service not ready")

    try:
        detail = await feishu.get_record_detail(payload.record_id)
    except FeishuError as exc:
        raise HTTPException(status_code=502, detail=f"读取记录失败：{exc}") from exc
    fields = detail["fields"]
    settled, age = _settled(detail.get("last_modified_time"))
    try:
        _extract_file_token(None, fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    style = _field_text(fields.get(F_STYLE))
    scheme = _field_text(fields.get(F_SCHEME))
    requirement = _field_text(fields.get(F_REQUIREMENT))
    card = _field_text(fields.get(F_PRODUCT_CARD))
    visual = _field_text(fields.get(F_VISUAL))
    font = _field_text(fields.get(F_FONT))
    copy_text = _field_text(fields.get(F_COPY))

    prompt_text, template = build_prompt(style, requirement, scheme)
    nodes = comfy.node_map
    graph = comfy.build_preview(
        image_name=f"{payload.record_id}.jpg",
        prompt_text=prompt_text,
        template=template,
        seed=_job_seed(payload.record_id),
        product_card=card,
        visual_index=resolve_visual(visual),
        font_index=resolve_font(font),
        copy_text=copy_text,
    )

    def clipped(node_id: str, key: str) -> Any:
        value = ((graph.get(node_id) or {}).get("inputs") or {}).get(key)
        return value[:120] if isinstance(value, str) else value

    return {
        "record_id": payload.record_id,
        "submit": _field_text(fields.get(F_SUBMIT)),
        "snapshot": _record_snapshot(fields),
        "settled": settled,
        "idle_seconds": None if age == float("inf") else round(age, 1),
        "sources": {
            "产品图": "飞书",
            "产品卡片": "飞书" if card else "内置",
            "卖点文案": "飞书" if copy_text else "内置",
            "画面风格": visual or "默认",
            "字体风格": font or "默认",
        },
        "nodes": {
            nodes["product_image"]: {"input": "image", "value": clipped(nodes["product_image"], "image")},
            nodes["product_card"]: {"input": "value", "value": clipped(nodes["product_card"], "value")},
            nodes["copy"]: {"input": "value", "value": clipped(nodes["copy"], "value")},
            nodes["activity"]: {"input": "select", "value": clipped(nodes["activity"], "select")},
            nodes["scheme"]: {"input": "select", "value": clipped(nodes["scheme"], "select")},
            nodes["visual"]: {"input": "select", "value": clipped(nodes["visual"], "select")},
            nodes["font"]: {"input": "select", "value": clipped(nodes["font"], "select")},
        },
    }
