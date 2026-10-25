"""Comfy Cloud 引擎：上传参考图、提交新版九宫格工作流、轮询并取回成品图。

Comfy Cloud 的 Partner Node 需要同时满足两点：
1. 请求头带 ``X-API-Key``；
2. 请求体的 ``extra_data.api_key_comfy_org`` 带同一个凭证。

新版工作流保存在仓库内，由 ``WORKFLOW_PATH`` 指定。每次任务只复制并修改
工作流副本，不会污染下一次任务使用的基准 JSON。
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import mimetypes
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from prompts import StyleTemplate

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 5.0
JOB_TIMEOUT_S = 300.0
DEFAULT_IMAGE_NODE = "11"
DEFAULT_ACTIVITY_NODE = "3761723474387808"
DEFAULT_SCHEME_NODE = "2565934293372055"
DEFAULT_PROMPT_NODE = "1705779279442278"
DEFAULT_CARD_NODE = "2334937224229421"
# 68 = 九宫格整图；59..67 = 按位置切好的九张（63/64 的编号与序号错位，
# 63 是第 6 张、64 是第 5 张）；81 = 从九宫格挑出的一格放大图。
FULL_IMAGE_NODES = ("68",)
TILE_NODES = ("59", "60", "61", "62", "64", "63", "65", "66", "67")
UPSCALED_NODES = ("81",)
DEFAULT_OUTPUT_NODES = (*FULL_IMAGE_NODES, *TILE_NODES, *UPSCALED_NODES)


class ComfyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ComfyImage:
    image_bytes: bytes
    filename: str
    width: int
    height: int


@dataclass(frozen=True)
class ComfyResult:
    images: dict[str, ComfyImage]
    elapsed_s: float


def load_workflow(path: str) -> dict[str, Any]:
    """读取并校验 ComfyUI API 格式的工作流 JSON。"""
    workflow_path = Path(path)
    if not workflow_path.is_absolute():
        workflow_path = Path(__file__).resolve().parent / workflow_path
    try:
        with workflow_path.open("r", encoding="utf-8") as handle:
            workflow = json.load(handle)
    except FileNotFoundError as exc:
        raise ComfyError(f"workflow file not found: {workflow_path}") from exc
    except json.JSONDecodeError as exc:
        raise ComfyError(f"workflow JSON invalid: {workflow_path}: {exc}") from exc
    if not isinstance(workflow, dict) or not workflow:
        raise ComfyError(f"workflow must be a non-empty object: {workflow_path}")
    return workflow


def _node_inputs(graph: dict[str, Any], node_id: str) -> dict[str, Any]:
    node = graph.get(node_id)
    if not isinstance(node, dict):
        raise ComfyError(f"workflow node not found: {node_id}")
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        raise ComfyError(f"workflow node has no inputs: {node_id}")
    return inputs


def build_graph(
    workflow: dict[str, Any],
    image_name: str,
    prompt_text: str,
    template: StyleTemplate,
    seed: int,
    product_card: str = "",
    *,
    image_node: str = DEFAULT_IMAGE_NODE,
    activity_node: str = DEFAULT_ACTIVITY_NODE,
    scheme_node: str = DEFAULT_SCHEME_NODE,
    prompt_node: str = DEFAULT_PROMPT_NODE,
    card_node: str = DEFAULT_CARD_NODE,
) -> dict[str, Any]:
    """复制基准工作流，并替换本次任务的动态输入。"""
    graph = copy.deepcopy(workflow)

    _node_inputs(graph, image_node)["image"] = image_name
    _node_inputs(graph, activity_node)["select"] = template.activity_index
    _node_inputs(graph, scheme_node)["select"] = template.scheme_index

    if product_card.strip():
        # 飞书「产品卡片」非空时覆盖工作流内置卡片，避免换成别的产品后
        # 仍然按内置卡片描述瓶型、标签和卖点。
        _node_inputs(graph, card_node)["value"] = product_card.strip()

    fixed_inputs = _node_inputs(graph, prompt_node)
    fixed_prompt = fixed_inputs.get("value")
    if not isinstance(fixed_prompt, str):
        raise ComfyError(f"workflow prompt node has no string value: {prompt_node}")
    if prompt_text.strip():
        fixed_inputs["value"] = fixed_prompt.rstrip() + "\n\n" + prompt_text.strip()

    # 新版工作流有两个 GPT Image 节点。统一替换 seed，保证同一记录重试时
    # 使用相同的任务种子，同时避免继续使用 JSON 中的固定示例种子。
    for node in graph.values():
        if not isinstance(node, dict) or node.get("class_type") != "OpenAIGPTImageNodeV2":
            continue
        inputs = node.get("inputs")
        if isinstance(inputs, dict) and isinstance(inputs.get("seed"), int):
            inputs["seed"] = seed

    return graph


class ComfyEngine:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        usage_source: str,
        workflow_path: str,
        output_nodes: Sequence[str] = DEFAULT_OUTPUT_NODES,
        image_node: str = DEFAULT_IMAGE_NODE,
        activity_node: str = DEFAULT_ACTIVITY_NODE,
        scheme_node: str = DEFAULT_SCHEME_NODE,
        prompt_node: str = DEFAULT_PROMPT_NODE,
        card_node: str = DEFAULT_CARD_NODE,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._usage_source = usage_source
        self._workflow_path = workflow_path
        self._workflow = load_workflow(workflow_path)
        self._output_nodes = list(output_nodes)
        self._image_node = image_node
        self._activity_node = activity_node
        self._scheme_node = scheme_node
        self._prompt_node = prompt_node
        self._card_node = card_node
        for node_id in [
            *self._output_nodes,
            image_node,
            activity_node,
            scheme_node,
            prompt_node,
            card_node,
        ]:
            _node_inputs(self._workflow, node_id)
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={"X-API-Key": api_key},
            timeout=httpx.Timeout(120.0, connect=15.0),
            follow_redirects=True,
        )

    @property
    def workflow_name(self) -> str:
        return Path(self._workflow_path).name

    async def aclose(self) -> None:
        await self._client.aclose()

    async def upload_image(self, data: bytes, filename: str) -> str:
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        files = {"image": (filename, data, content_type)}
        form = {"type": "input", "overwrite": "true"}
        resp = await self._client.post("/api/upload/image", files=files, data=form)
        if resp.status_code != 200:
            raise ComfyError(f"upload failed {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        image_name = body.get("name")
        if not image_name:
            raise ComfyError(f"upload response has no name: {body}")
        return str(image_name)

    async def submit(self, graph: dict[str, Any]) -> str:
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
            last_status = str(job.get("status", "unknown")).lower()
            if last_status == "failed":
                error = job.get("execution_error")
                if isinstance(error, dict):
                    message = error.get("exception_message") or error.get("message") or ""
                else:
                    message = str(error or "")
                raise ComfyError(f"job failed: {message or 'unknown error'}")
            if last_status != "completed":
                continue

            collected: dict[str, ComfyImage] = {}
            for node_id in self._output_nodes:
                node_output = self._outputs_of(job, node_id)
                if node_output is None:
                    log.warning("job %s has no output for node %s", prompt_id, node_id)
                    continue
                collected[node_id] = await self._fetch(node_output)
            if not collected:
                raise ComfyError("job completed but none of the configured output nodes produced an image")
            return ComfyResult(images=collected, elapsed_s=time.monotonic() - started)
        raise ComfyError(f"timeout after {timeout_s}s, last status={last_status}")

    @staticmethod
    def _outputs_of(job: dict[str, Any], node_id: str) -> dict[str, Any] | None:
        outputs = job.get("outputs")
        if not isinstance(outputs, dict):
            return None
        node_output = outputs.get(node_id)
        if not isinstance(node_output, dict):
            return None
        images = node_output.get("images")
        if not isinstance(images, list) or not images or not isinstance(images[0], dict):
            return None
        image = dict(images[0])
        if not image.get("filename"):
            return None
        return image

    async def _fetch(self, image: dict[str, Any]) -> ComfyImage:
        data = await self._download(image)
        metadata = image.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        width = image.get("width") or metadata.get("width") or 0
        height = image.get("height") or metadata.get("height") or 0
        return ComfyImage(
            image_bytes=data,
            filename=str(image["filename"]),
            width=int(width),
            height=int(height),
        )

    async def _download(self, image: dict[str, Any]) -> bytes:
        params = {
            "filename": str(image["filename"]),
            "subfolder": str(image.get("subfolder") or ""),
            "type": str(image.get("type") or "output"),
        }
        resp = await self._client.get("/api/view", params=params)
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
        product_card: str = "",
    ) -> ComfyResult:
        image_name = await self.upload_image(image_bytes, filename)
        graph = build_graph(
            self._workflow,
            image_name=image_name,
            prompt_text=prompt_text,
            template=template,
            seed=seed,
            product_card=product_card,
            image_node=self._image_node,
            activity_node=self._activity_node,
            scheme_node=self._scheme_node,
            prompt_node=self._prompt_node,
            card_node=self._card_node,
        )
        prompt_id = await self.submit(graph)
        log.info("submitted prompt_id=%s workflow=%s output_nodes=%s", prompt_id, self.workflow_name, ",".join(self._output_nodes))
        return await self.wait(prompt_id)
