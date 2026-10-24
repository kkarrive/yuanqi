"""Comfy Cloud 引擎：上传参考图 → 提交工作流 → 轮询 → 取图。

关键点（都踩过）：
1. Partner Node 的凭证必须放在 body 的 extra_data.api_key_comfy_org，
   只加 X-API-Key 请求头会在执行时报 "Unauthorized: Please login first"。
2. /api/view 返回 302 到预签名地址，必须跟随重定向，否则拿到 0 字节。
3. /api/history 在 Cloud 已废弃，用 /api/jobs/{id} 或 /api/history_v2。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from prompts import StyleTemplate

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 5.0
JOB_TIMEOUT_S = 300.0


class ComfyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ComfyResult:
    image_bytes: bytes
    filename: str
    width: int
    height: int
    elapsed_s: float


def build_graph(image_name: str, prompt_text: str, template: StyleTemplate, seed: int) -> dict:
    """构造 ComfyUI API 格式工作流：LoadImage → Nano Banana 2 → SaveImage。"""
    return {
        "1": {
            "class_type": "LoadImage",
            "inputs": {"image": image_name},
        },
        "2": {
            "class_type": "GeminiNanoBanana2",
            "inputs": {
                "prompt": prompt_text,
                "model": "Nano Banana 2 (Gemini 3.1 Flash Image)",
                "seed": seed,
                "aspect_ratio": template.aspect_ratio,
                "resolution": template.resolution,
                "response_modalities": "IMAGE",
                "thinking_level": "MINIMAL",
                "images": ["1", 0],
            },
        },
        "3": {
            "class_type": "SaveImage",
            "inputs": {"images": ["2", 0], "filename_prefix": "poster"},
        },
    }


class ComfyEngine:
    def __init__(self, base_url: str, api_key: str, usage_source: str) -> None:
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._usage_source = usage_source
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={"X-API-Key": api_key},
            timeout=httpx.Timeout(120.0, connect=15.0),
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def upload_image(self, data: bytes, filename: str) -> str:
        files = {"image": (filename, data, "image/jpeg")}
        form = {"type": "input", "overwrite": "true"}
        resp = await self._client.post("/api/upload/image", files=files, data=form)
        if resp.status_code != 200:
            raise ComfyError(f"upload failed {resp.status_code}: {resp.text[:200]}")
        return str(resp.json()["name"])

    async def submit(self, graph: dict) -> str:
        payload = {
            "prompt": graph,
            "extra_data": {
                "api_key_comfy_org": self._api_key,
                "comfy_usage_source": self._usage_source,
            },
        }
        resp = await self._client.post("/api/prompt", json=payload)
        if resp.status_code != 200:
            raise ComfyError(f"prompt failed {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        prompt_id = body.get("prompt_id")
        if not prompt_id:
            raise ComfyError(f"no prompt_id in response: {body}")
        return str(prompt_id)

    async def wait(self, prompt_id: str, timeout_s: float = JOB_TIMEOUT_S) -> ComfyResult:
        started = time.monotonic()
        last_status = "unknown"
        while time.monotonic() - started < timeout_s:
            await asyncio.sleep(POLL_INTERVAL_S)
            resp = await self._client.get(f"/api/jobs/{prompt_id}")
            if resp.status_code != 200:
                log.warning("job poll %s -> %s", prompt_id, resp.status_code)
                continue
            job = resp.json()
            last_status = str(job.get("status", "unknown"))
            if last_status == "failed":
                err = (job.get("execution_error") or {}).get("exception_message", "")
                raise ComfyError(f"job failed: {err or 'unknown error'}")
            if last_status != "completed":
                continue
            image = self._first_image(job)
            data = await self._download(image["filename"])
            return ComfyResult(
                image_bytes=data,
                filename=str(image["filename"]),
                width=int((image.get("metadata") or {}).get("width") or 0),
                height=int((image.get("metadata") or {}).get("height") or 0),
                elapsed_s=time.monotonic() - started,
            )
        raise ComfyError(f"timeout after {timeout_s}s, last status={last_status}")

    @staticmethod
    def _first_image(job: dict) -> dict:
        outputs = job.get("outputs") or {}
        for node_output in outputs.values():
            for image in node_output.get("images") or []:
                return dict(image)
        raise ComfyError("job completed but no image in outputs")

    async def _download(self, filename: str) -> bytes:
        resp = await self._client.get(
            "/api/view",
            params={"filename": filename, "subfolder": "", "type": "output"},
        )
        if resp.status_code != 200 or not resp.content:
            raise ComfyError(f"view failed {resp.status_code}, {len(resp.content)} bytes")
        return resp.content

    async def generate(
        self,
        image_bytes: bytes,
        filename: str,
        prompt_text: str,
        template: StyleTemplate,
        seed: int,
    ) -> ComfyResult:
        image_name = await self.upload_image(image_bytes, filename)
        graph = build_graph(image_name, prompt_text, template, seed)
        prompt_id = await self.submit(graph)
        log.info("submitted prompt_id=%s", prompt_id)
        return await self.wait(prompt_id)
