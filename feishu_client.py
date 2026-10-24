"""飞书 OpenAPI 客户端：拿 tenant_access_token、下载产品图、上传海报素材、
更新多维表格记录、给提交人发消息。

权限要求：
- bitable:app       读写多维表格记录
- drive:drive       上传/下载素材
- im:message:send_as_bot  机器人发消息
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

import httpx

BASE = "https://open.feishu.cn/open-apis"
TOKEN_SAFETY_MARGIN_S = 120


class FeishuError(RuntimeError):
    pass


@dataclass
class _Token:
    value: str
    expires_at: float


class FeishuClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        app_token: str,
        table_id: str,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._app_token = app_token
        self._table_id = table_id
        self._token: _Token | None = None
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(base_url=BASE, timeout=httpx.Timeout(60.0, connect=10.0))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _tenant_token(self) -> str:
        async with self._lock:
            now = time.time()
            if self._token and self._token.expires_at > now:
                return self._token.value
            resp = await self._client.post(
                "/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
            body = resp.json()
            if body.get("code") != 0 or not body.get("tenant_access_token"):
                raise FeishuError(f"token failed: {body}")
            expire = int(body.get("expire", 7200))
            self._token = _Token(
                value=str(body["tenant_access_token"]),
                expires_at=now + max(expire - TOKEN_SAFETY_MARGIN_S, 60),
            )
            return self._token.value

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._tenant_token()}"}

    async def download_media(self, file_token: str) -> bytes:
        resp = await self._client.get(
            f"/drive/v1/medias/{file_token}/download",
            headers=await self._headers(),
        )
        if resp.status_code != 200 or not resp.content:
            raise FeishuError(f"download media failed {resp.status_code}: {resp.text[:200]}")
        return resp.content

    async def upload_media(self, data: bytes, filename: str) -> str:
        form = {
            "file_name": filename,
            "parent_type": "bitable_image",
            "parent_node": self._app_token,
            "size": str(len(data)),
        }
        files = {"file": (filename, data, "image/png")}
        resp = await self._client.post(
            "/drive/v1/medias/upload_all",
            headers=await self._headers(),
            data=form,
            files=files,
        )
        body = resp.json()
        if body.get("code") != 0:
            raise FeishuError(f"upload media failed: {body}")
        return str(body["data"]["file_token"])

    async def get_record(self, record_id: str) -> dict:
        """返回记录的 fields 字典（飞书把字段嵌在 data.record.fields 里）。"""
        resp = await self._client.get(
            f"/bitable/v1/apps/{self._app_token}/tables/{self._table_id}/records/{record_id}",
            headers=await self._headers(),
        )
        body = resp.json()
        if body.get("code") != 0:
            raise FeishuError(f"get record failed: {body}")
        fields = body["data"]["record"].get("fields")
        if not isinstance(fields, dict):
            raise FeishuError(f"unexpected record shape: {body['data']['record']}")
        return dict(fields)

    async def update_record(self, record_id: str, fields: dict) -> None:
        resp = await self._client.put(
            f"/bitable/v1/apps/{self._app_token}/tables/{self._table_id}/records/{record_id}",
            headers=await self._headers(),
            json={"fields": fields},
        )
        body = resp.json()
        if body.get("code") != 0:
            raise FeishuError(f"update record failed: {body}")

    async def send_text(self, open_id: str, text: str) -> None:
        payload = {
            "receive_id": open_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        resp = await self._client.post(
            "/im/v1/messages",
            params={"receive_id_type": "open_id"},
            headers=await self._headers(),
            json=payload,
        )
        body = resp.json()
        if body.get("code") != 0:
            raise FeishuError(f"send message failed: {body}")
