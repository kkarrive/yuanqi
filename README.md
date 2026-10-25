# 飞书 × Comfy Cloud 九宫格海报生成服务

员工在飞书多维表格上传产品图、选择活动主题和镜头方案，云端工作流生成九宫格商业海报，再自动回填结果图。

```text
飞书多维表格
      ↓ 自动化 HTTP 请求，或服务轮询「待处理」记录
FastAPI（本服务，公网可达）
      ↓ 立即返回 202，后台单 worker 排队
Comfy Cloud（新版元气森林九宫格工作流）
      ↓
飞书：上传结果素材 → 更新记录 → 通知提交人
```

## 新版工作流

新版工作流保存在 `yuanqi-poster-workflow.json`（64 个节点）。服务每次任务会：

- 将飞书产品图上传到 Comfy Cloud，并替换节点 `11`；
- 按「风格」和「方案」替换主题节点 `3761723474387808`、方案节点 `2565934293372055`；
- 按「画面风格」和「字体风格」替换节点 `705857498768221`、`4105141153397020`（留空走默认：写实影棚 / 品牌角标）；
- 若填写「卖点文案」，覆盖节点 `3286967850890428`；留空则沿用工作流内置文案；
- 若填写「产品卡片」，覆盖节点 `2334937224229421`；
- 将「需求描述」追加到固定规则节点 `1705779279442278`，作为补充创意要求；
- 产出三类图并分别回写三个附件字段：
- 产出三类图并分别回写三个附件字段：

| 字段 | 内容 | 节点 |
|---|---|---|
| `结果图` | 一整张 3×3 九宫格 | `68` |
| `九宫格切片` | 按序号 1→9 的九张单格 | `59,60,61,62,64,63,65,66,67` |
| `放大图` | 从九宫格挑出的一格放大 | `81` |

工作流 JSON 必须是 ComfyUI API 格式。节点 ID 可通过 `WORKFLOW_*_NODE` 与 `WORKFLOW_OUTPUT_NODES` 环境变量替换；切片节点 `63`/`64` 的编号与序号错位（`63` 是第 6 张、`64` 是第 5 张），代码里已按切片位置排序。

## 目录

| 文件 | 职责 |
|---|---|
| `app.py` | FastAPI 入口、异步队列、单 worker、幂等和轮询 |
| `comfy_engine.py` | 加载工作流，上传图、提交、轮询和按节点取回多张图 |
| `feishu_client.py` | 飞书 OpenAPI 封装 |
| `prompts.py` | 活动主题、镜头方案和用户补充提示映射 |
| `yuanqi-poster-workflow.json` | ComfyUI API 格式的新版九宫格工作流 |
| `.env.example` | 配置项示例 |
| `Dockerfile` | Sealos / 任意容器平台部署 |

## 本地运行

```bash
cp .env.example .env  # 填入真实配置，不要提交 .env
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

自检：

```bash
curl http://127.0.0.1:8000/healthz
```

健康接口会返回 `workflow` 和 `output_node`，用于确认实际加载的工作流版本和结果节点。

## Sealos 部署

1. 将本目录推送到 Git 仓库。
2. Sealos → 应用管理 → 新建应用 → 选择 Dockerfile 构建。
3. 配置 `.env.example` 中的环境变量；真实 API key、app secret 和入站 token 只放在平台密钥/环境变量中。
4. 容器端口填 `8000`，开启外网访问。
5. 部署后验证：

```bash
curl https://<你的域名>/healthz
```

期望看到类似结果：

```json
{"ok":true,"queued":0,"jobs":0,"workflow":"yuanqi-poster-workflow.json","output_node":"81"}
```

## 多维表格字段

字段名必须和 `FIELD_*` 环境变量一致：

| 列名 | 字段类型 | 说明 |
|---|---|---|
| 产品图 | 附件 | 员工上传，建议白底正面图 |
| 需求描述 | 多行文本 | 可留空，作为固定工作流之外的补充要求 |
| 产品卡片 | 多行文本 | 可选；留空则用工作流内置卡片。换产品时填瓶型、标签、卖点 |
| 风格 | 单选 | 夏日冰爽 / 运动燃脂 / 户外露营 / 节日礼赠 / 办公提神 |
| 方案 | 单选 | 电商主图 / TVC动态分镜 |
| 状态 | 单选 | 待处理 / 生成中 / 已完成 / 失败 |
| 结果图 | 附件 | 自动回填九宫格整图（节点 `68`） |
| 九宫格切片 | 附件 | 自动回填按序九张单格（文件名带 1-9 序号） |
| 放大图 | 附件 | 自动回填单格放大图（节点 `81`） |
| 错误信息 | 多行文本 | 失败原因 |
| 提交人 | 创建人 | 用于发送完成通知 |

旧选项 `夏日清爽`、`健身能量`、`简约电商` 仍由服务兼容，其中 `简约电商` 默认映射到「电商主图」方案。

`FEISHU_APP_TOKEN` 取多维表格 URL 里 `/base/<这一段>`；`FEISHU_TABLE_ID` 取 `?table=<这一段>`。

## 触发方式一：飞书自动化

触发：**添加新记录时**。

动作：**发送 HTTP 请求**。

| 项 | 值 |
|---|---|
| 方法 | POST |
| URL | `https://<你的域名>/api/jobs` |
| Header | `Authorization: Bearer <API_TOKEN>` |
| Header | `Content-Type: application/json` |
| Body | 见下 |

最小 Body：

```json
{"record_id":"<记录ID>"}
```

服务端会按记录 ID 回读产品图、需求描述、风格、方案和提交人。也可以显式传入字段：

```json
{
  "record_id": "<记录ID>",
  "requirement": "<需求描述>",
  "style": "<风格>",
  "scheme": "<方案>",
  "image_token": "<产品图的 file_token>"
}
```

## 触发方式二：服务轮询

设置 `POLL_ENABLED=true` 后，服务会按 `POLL_INTERVAL_S` 秒扫描状态为 `STATUS_PENDING` 的记录。扫描到有产品图的记录后自动排队，适合不配置飞书自动化的部署方式。

同一 `record_id` 在 `DEDUP_TTL_S` 时间内不会重复生成。服务重启后内存状态会清空，因此生产环境不要同时启用重复触发源，或保持飞书状态字段及时更新。

## 接口

### `GET /healthz`

返回服务健康状态、队列长度、内存任务数、工作流名称和结果节点。

### `POST /api/jobs`

接收任务并立即返回 `202`：

```json
{"job_id":"<任务ID>"}
```

### `GET /api/jobs/{record_id}`

查询内存中的任务状态：`queued`、`running`、`done` 或 `failed`。

## 排查手册

| 现象 | 原因 | 处理 |
|---|---|---|
| 飞书里报请求超时 | 服务同步等待出图 | 确认接口返回 `202`，本服务按异步实现 |
| 状态一直是待处理 | 服务没收到请求或轮询未开启 | 看服务日志，确认公网域名、入站 token、字段名和 `POLL_ENABLED` |
| `Unauthorized: Please login first` | Partner Node 缺少凭证 | 确认 `COMFY_API_KEY` 同时位于请求头和 `extra_data.api_key_comfy_org` |
| `Payment Required` / `credits_locked` | Comfy 订阅或积分问题 | 检查 Comfy Cloud 账户状态 |
| 结果图 0 字节 | `/api/view` 的 302 未跟随 | 当前客户端已启用 `follow_redirects=True` |
| 结果图字段为空 | 飞书素材上传失败 | 确认应用有 `drive:drive` 权限且已发布 |
| `output node 81 is missing` | 工作流与节点配置不匹配 | 检查 `WORKFLOW_PATH` 和 `WORKFLOW_OUTPUT_NODE` |

## 已知平台约束

- Comfy Cloud 的 Partner Node 认证值必须放进请求体的 `extra_data.api_key_comfy_org`。
- Comfy Cloud 使用 `/api/jobs/{prompt_id}` 查询任务，不使用已废弃的 `/api/history`。
- `/api/view` 可能返回预签名地址，需要跟随重定向。
- 本地不执行 Partner Node，推理统一走 Comfy Cloud。
