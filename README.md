# 飞书 × Comfy Cloud 海报生成服务

员工在多维表格里上传产品图、写一句创意 → 云端模型出商业海报 → 自动回填到表格。

```
飞书多维表格（员工传图 + 创意）
      ↓ 自动化「发送 HTTP 请求」
FastAPI（本服务，公网可达）
      ↓ 立刻 202，后台排队
Comfy Cloud（Nano Banana 2，云端推理，本地零依赖）
      ↓
回填飞书：上传素材 → 更新记录 → 发消息
```

## 目录

| 文件 | 职责 |
|---|---|
| `app.py` | FastAPI 入口、内存队列、单 worker、幂等 |
| `comfy_engine.py` | Comfy Cloud 四步调用（上传图→提交→轮询→取图） |
| `feishu_client.py` | 飞书 OpenAPI 封装 |
| `prompts.py` | 三套风格提示词模板 |
| `.env.example` | 全部配置项 |
| `Dockerfile` | Sealos / 任意容器平台部署 |

## 本地运行

```bash
cp .env.example .env      # 填好值
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

自检：

```bash
curl http://127.0.0.1:8000/healthz
# {"ok":true,"queued":0,"jobs":0}
```

## Sealos 部署

1. 把本目录推到 Git 仓库（或在 Sealos DevBox 里直接上传）
2. Sealos → 应用管理 → 新建应用 → 选择「Dockerfile 构建」或填镜像
3. 环境变量：把 `.env` 里所有键逐个填进「环境变量」面板
4. 端口填 `8000`，开启外网访问
5. 部署完成后拿到公网域名，例如 `https://xxxxx.sealos.run`

验证公网可达：

```bash
curl https://xxxxx.sealos.run/healthz
```

## 多维表格字段

列名必须和 `.env` 里的 `FIELD_*` 完全一致：

| 列名 | 字段类型 | 说明 |
|---|---|---|
| 产品图 | 附件 | 员工上传，建议白底图 |
| 需求描述 | 多行文本 | 可留空，留空用默认模板 |
| 风格 | 单选 | 夏日清爽 / 健身能量 / 简约电商 |
| 状态 | 单选 | 待处理 / 生成中 / 已完成 / 失败 |
| 结果图 | 附件 | 自动回填 |
| 错误信息 | 多行文本 | 失败原因 |
| 提交人 | 创建人 | 自动字段，用于发通知 |

`FEISHU_APP_TOKEN` 取多维表格 URL 里 `/base/<这一段>`；`FEISHU_TABLE_ID` 取 `?table=<这一段>`。

## 飞书自动化配置

触发：**添加新记录时**

动作：**发送 HTTP 请求**

| 项 | 值 |
|---|---|
| 方法 | POST |
| URL | `https://<你的域名>/api/jobs` |
| Header | `Authorization: Bearer <API_TOKEN>` |
| Header | `Content-Type: application/json` |
| Body(raw) | 见下 |

```json
{"record_id": "<记录ID>"}
```

只传 `record_id` 就够了——服务端会自己回读记录里的产品图、需求、风格。这样不用在飞书里拼字段引用，也不怕字段改名。

如果飞书的记录 ID 变量不好取，也可以显式传：

```json
{
  "record_id": "<记录ID>",
  "requirement": "<需求描述>",
  "style": "<风格>",
  "image_token": "<产品图的 file_token>"
}
```

## 排查手册

| 现象 | 原因 | 处理 |
|---|---|---|
| 飞书里报请求超时 | 服务同步等出图 | 确认返回 202 而非 200；本服务已按异步实现 |
| 记录状态一直是待处理 | 服务没收到请求 | 看服务日志；确认公网域名、token、字段名 |
| 状态变「失败」，错误含 `Unauthorized: Please login first` | Partner Node 缺凭证 | 确认 `COMFY_API_KEY` 同时进了请求头和 `extra_data.api_key_comfy_org`（`comfy_engine.py` 已处理） |
| 错误含 `Payment Required` / `credits_locked` | Comfy 订阅或积分问题 | 去 comfy.org 查订阅状态 |
| 取回的图 0 字节 | `/api/view` 的 302 没跟随 | `httpx` 用 `follow_redirects=True`（已设置） |
| 附件字段空着 | 上传素材失败 | 确认应用有 `drive:drive` 权限且已发布 |

## 已踩过并修复的坑

1. **裸提交 Partner Node 必然失败**。只加 `X-API-Key` 请求头不够，认证值必须放进 body 的
   `extra_data.api_key_comfy_org`，因为节点读的是 `node_cls.hidden.api_key_comfy_org`。
2. **`/api/history` 在 Cloud 已废弃**，用 `/api/jobs/{prompt_id}`。
3. **`/api/view` 返回 302 预签名地址**，不跟随重定向会拿到空文件。
4. **本机跑不了 Partner Node**：RTX 5060 Laptop + 8G 显存加载 `comfy_api_nodes` 时进程硬崩，
   所以出图必须走云端。
