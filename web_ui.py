"""网页端：账号登录 + 直接在页面里生图（不经过飞书）。

设计要点：
1. 凭据用环境变量覆盖，默认 demo/demo1234 仅供演示。
2. 会话用 HMAC 签名 Cookie，不引入额外依赖。
3. 网页任务与飞书任务相互独立：各自队列、各自 worker，互不影响。
4. 结果图落盘到 WEB_DATA_DIR，按 job 分目录，超过 WEB_MAX_JOBS 自动清理。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from comfy_engine import ComfyEngine, ComfyImage
from prompts import build_prompt, resolve_font, resolve_visual

log = logging.getLogger("web")

WEB_USER = os.getenv("WEB_USER", "demo")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "demo1234")
WEB_SECRET = os.getenv("WEB_SECRET", "yuanqi-web-demo-secret-change-me")
WEB_MAX_JOBS = int(os.getenv("WEB_MAX_JOBS", "30"))
SESSION_TTL_S = float(os.getenv("WEB_SESSION_TTL_S", "86400"))
MAX_UPLOAD_BYTES = int(os.getenv("WEB_MAX_UPLOAD_MB", "15")) * 1024 * 1024
COOKIE_NAME = "yuanqi_session"

# 切片节点按位置排成 1..9：63 是第 6 张、64 是第 5 张
TILE_NODES = ("59", "60", "61", "62", "64", "63", "65", "66", "67")
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("full", ("68",)),
    ("tiles", TILE_NODES),
    ("up", ("81",)),
)


def data_dir() -> Path:
    """优先用挂载卷，不可写时退回临时目录。"""
    preferred = Path(os.getenv("WEB_DATA_DIR", "/data/web"))
    for candidate in (preferred, Path(tempfile.gettempdir()) / "yuanqi-web"):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError:
            continue
    raise RuntimeError("没有可写的数据目录")


# ---------- 会话 ----------

def _sign(raw: str) -> str:
    return hmac.new(WEB_SECRET.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def issue_session(user: str) -> str:
    raw = f"{user}:{int(time.time() + SESSION_TTL_S)}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode() + "." + _sign(raw)


def read_session(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    encoded, _, signature = token.rpartition(".")
    try:
        raw = base64.urlsafe_b64decode(encoded.encode()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(signature, _sign(raw)):
        return None
    user, _, expiry = raw.partition(":")
    try:
        if int(expiry) < time.time():
            return None
    except ValueError:
        return None
    return user or None


def check_credentials(user: str, password: str) -> bool:
    return hmac.compare_digest(user.strip(), WEB_USER) and hmac.compare_digest(password, WEB_PASSWORD)


# ---------- 任务 ----------

@dataclass
class WebJob:
    job_id: str
    requirement: str
    style: str
    scheme: str
    visual: str
    font: str
    product_card: str
    copy_text: str
    image_name: str
    image_bytes: bytes
    status: str = "queued"
    error: str = ""
    prompt_id: str = ""
    images: dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0


jobs: dict[str, WebJob] = {}
queue: asyncio.Queue[WebJob] = asyncio.Queue()


def _seed(job_id: str) -> int:
    return int.from_bytes(hashlib.sha256(job_id.encode()).digest()[:4], "big") % 2_147_483_647


def _prune() -> None:
    """只保留最近 N 个已结束任务，删除其余目录。"""
    finished = sorted(
        (j for j in jobs.values() if j.status in {"done", "failed"}),
        key=lambda j: j.finished_at,
        reverse=True,
    )
    for stale in finished[WEB_MAX_JOBS:]:
        jobs.pop(stale.job_id, None)
        shutil.rmtree(data_dir() / stale.job_id, ignore_errors=True)


def create_job(
    *,
    requirement: str,
    style: str,
    scheme: str,
    visual: str,
    font: str,
    product_card: str,
    copy_text: str,
    image_name: str,
    image_bytes: bytes,
) -> WebJob:
    job = WebJob(
        job_id=uuid.uuid4().hex[:12],
        requirement=requirement,
        style=style,
        scheme=scheme,
        visual=visual,
        font=font,
        product_card=product_card,
        copy_text=copy_text,
        image_name=image_name,
        image_bytes=image_bytes,
    )
    jobs[job.job_id] = job
    _prune()
    return job


async def run_job(job: WebJob, comfy: ComfyEngine) -> None:
    job.status = "running"
    try:
        prompt_text, template = build_prompt(job.style, job.requirement, job.scheme)
        result = await comfy.generate(
            image_bytes=job.image_bytes,
            filename=f"web-{job.job_id}.jpg",
            prompt_text=prompt_text,
            template=template,
            product_card=job.product_card,
            visual_index=resolve_visual(job.visual),
            font_index=resolve_font(job.font),
            copy_text=job.copy_text,
            seed=_seed(job.job_id),
        )
        job.prompt_id = result.prompt_id

        folder = data_dir() / job.job_id
        folder.mkdir(parents=True, exist_ok=True)
        for key, nodes in GROUPS:
            index = 0
            for node in nodes:
                image: ComfyImage | None = result.images.get(node)
                if image is None:
                    continue
                index += 1
                filename = f"{key}-{index}.png"
                (folder / filename).write_bytes(image.image_bytes)
                job.images[f"{key}-{index}"] = filename
        if not job.images:
            raise RuntimeError("生成完成但没有任何图片产出")
        job.status = "done"
        log.info("web job %s done (%d images)", job.job_id, len(job.images))
    except Exception as exc:  # noqa: BLE001 - 单任务失败不能拖垮 worker
        job.status = "failed"
        job.error = str(exc)[:400]
        log.error("web job %s failed: %s", job.job_id, exc)
    finally:
        job.finished_at = time.time()
        job.image_bytes = b""  # 及时释放内存


async def worker(comfy_getter) -> None:
    while True:
        job = await queue.get()
        try:
            comfy = comfy_getter()
            if comfy is None:
                job.status = "failed"
                job.error = "服务未就绪"
                job.finished_at = time.time()
                continue
            await run_job(job, comfy)
        except Exception:  # noqa: BLE001
            log.exception("web worker crashed")
        finally:
            queue.task_done()


def snapshot(job: WebJob) -> dict:
    """给前端的状态快照。"""
    elapsed = (job.finished_at or time.time()) - job.created_at
    return {
        "job_id": job.job_id,
        "status": job.status,
        "error": job.error,
        "elapsed_s": round(elapsed, 1),
        "prompt_id": job.prompt_id,
        "counts": {
            "full": sum(1 for k in job.images if k.startswith("full-")),
            "tiles": sum(1 for k in job.images if k.startswith("tiles-")),
            "up": sum(1 for k in job.images if k.startswith("up-")),
        },
        "images": [
            {"key": key, "url": f"/api/web/jobs/{job.job_id}/img/{name}"}
            for key, name in sorted(job.images.items())
        ],
    }


# ---------- 页面 ----------

STYLE = """
*{box-sizing:border-box}
body{margin:0;min-height:100vh;color:#e9eeff;
background:radial-gradient(1100px 520px at 12% -8%,#16223f 0%,#0a0f1e 58%) fixed;
font:15px/1.65 "Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif}
a{color:#5ad1b0}
.card{background:linear-gradient(180deg,#111a33,#0d1530);border:1px solid #25304f;
border-radius:14px;box-shadow:0 14px 36px rgba(0,0,0,.32)}
label{display:block;font-size:12px;color:#8b98bd;margin-bottom:4px}
select,textarea,input{width:100%;font:inherit;color:#e9eeff;background:#0c1428;
border:1px solid #25304f;border-radius:9px;padding:8px 10px}
textarea{min-height:72px;resize:vertical}
button{font:inherit;cursor:pointer;font-weight:600;padding:9px 18px;border-radius:9px;
border:1px solid #2f7d64;background:linear-gradient(180deg,#1d5b49,#153f33);color:#e9eeff}
button:hover:not(:disabled){transform:translateY(-1px)}
button:disabled{opacity:.45;cursor:not-allowed}
"""

LOGIN_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · 元气森林海报生成</title><style>__STYLE__
.login{max-width:380px;margin:12vh auto;padding:28px}
h1{font-size:20px;margin:0 0 6px}
p.sub{color:#8b98bd;font-size:13px;margin:0 0 20px}
.row{margin-bottom:14px}
.err{color:#ff7a7a;font-size:13px;margin:0 0 14px;min-height:20px}
</style></head><body>
<form class="card login" method="post" action="/login">
  <h1>元气森林海报生成</h1>
  <p class="sub">请使用分配的账号登录</p>
  <p class="err">__ERROR__</p>
  <div class="row"><label>账号</label><input name="username" autocomplete="username" autofocus></div>
  <div class="row"><label>密码</label><input name="password" type="password" autocomplete="current-password"></div>
  <button type="submit" style="width:100%">登录</button>
</form></body></html>"""

APP_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>元气森林海报生成</title><style>__STYLE__
header{display:flex;justify-content:space-between;align-items:center;max-width:1280px;
margin:0 auto;padding:18px 20px 6px}
header h1{font-size:19px;margin:0}
header .who{font-size:13px;color:#8b98bd}
.wrap{max-width:1280px;margin:0 auto;padding:10px 20px 60px}
.grid{display:grid;grid-template-columns:360px 1fr;gap:18px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.panel{padding:18px}
.panel h2{font-size:13px;color:#8b98bd;margin:0 0 14px;letter-spacing:.5px}
.field{margin-bottom:12px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.drop{border:1px dashed #2b3a60;border-radius:10px;padding:14px;text-align:center;
color:#8b98bd;font-size:13px;cursor:pointer}
.drop.has{border-color:#3ddbb0;color:#c9d8ee}
.drop img{max-width:100%;max-height:140px;border-radius:8px;display:block;margin:8px auto 0}
.status{font-size:13px;color:#8b98bd;margin:14px 0 0}
.status.done{color:#6ee7a8}
.status.failed{color:#ff7a7a}
.results{display:grid;gap:18px}
.group h3{font-size:12px;color:#8b98bd;margin:0 0 8px}
.thumbs{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.thumbs.one{grid-template-columns:1fr}
.thumbs img{width:100%;border-radius:8px;display:block;background:#0c1428}
.ph{border:1px dashed #25304f;border-radius:10px;min-height:120px;display:grid;
place-items:center;color:#5d6a90;font-size:13px}
</style></head><body>
<header>
  <h1>元气森林海报生成</h1>
  <span class="who">__USER__ · <a href="/logout">退出</a></span>
</header>
<div class="wrap">
  <div class="grid">
    <section class="card panel">
      <h2>生成参数</h2>
      <div class="field">
        <label>产品图（必填）</label>
        <div class="drop" id="drop">点击选择或拖入图片<input type="file" id="file" accept="image/*" hidden></div>
      </div>
      <div class="field two">
        <div><label>风格</label><select id="style">
          <option>夏日冰爽</option><option>运动燃脂</option><option>户外露营</option>
          <option>节日礼赠</option><option>办公提神</option></select></div>
        <div><label>方案</label><select id="scheme">
          <option>TVC动态分镜</option><option>电商主图</option><option>卖点海报矩阵</option></select></div>
      </div>
      <div class="field two">
        <div><label>画面风格</label><select id="visual">
          <option>写实影棚</option><option>几何品牌大片</option></select></div>
        <div><label>字体风格</label><select id="font">
          <option>品牌角标</option><option>活力气泡</option><option>书法质感</option></select></div>
      </div>
      <div class="field"><label>需求描述（可选）</label>
        <textarea id="req" placeholder="补充构图、场景或氛围要求"></textarea></div>
      <div class="field"><label>产品卡片（可选，留空用工作流内置）</label>
        <textarea id="card" placeholder="瓶型、标签、卖点等"></textarea></div>
      <div class="field"><label>卖点文案（可选，留空用工作流内置）</label>
        <textarea id="copy" placeholder="画面允许出现的广告文字"></textarea></div>
      <button id="go" style="width:100%">开始生成</button>
      <p class="status" id="status">等待提交</p>
    </section>

    <section class="card panel">
      <h2>生成结果</h2>
      <div class="results">
        <div class="group"><h3>九宫格整图</h3><div class="thumbs one" id="gFull"><div class="ph">尚未生成</div></div></div>
        <div class="group"><h3>九宫格切片（1→9）</h3><div class="thumbs" id="gTiles"><div class="ph" style="grid-column:1/-1">尚未生成</div></div></div>
        <div class="group"><h3>放大图</h3><div class="thumbs one" id="gUp"><div class="ph">尚未生成</div></div></div>
      </div>
      <p class="status" id="meta"></p>
    </section>
  </div>
</div>
<script>
const $ = (id) => document.getElementById(id);
let picked = null, timer = null;

$('drop').addEventListener('click', () => $('file').click());
$('file').addEventListener('change', (e) => setFile(e.target.files[0]));
$('drop').addEventListener('dragover', (e) => e.preventDefault());
$('drop').addEventListener('drop', (e) => {
  e.preventDefault();
  if (e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
});

function setFile(f) {
  if (!f) return;
  picked = f;
  const drop = $('drop');
  drop.classList.add('has');
  drop.innerHTML = '<div>' + f.name + ' · ' + Math.round(f.size / 1024) + ' KB</div>';
  const img = document.createElement('img');
  img.src = URL.createObjectURL(f);
  drop.appendChild(img);
  const input = document.createElement('input');
  input.type = 'file'; input.id = 'file'; input.accept = 'image/*'; input.hidden = true;
  input.addEventListener('change', (e) => setFile(e.target.files[0]));
  drop.appendChild(input);
}

function setStatus(text, cls) {
  const el = $('status');
  el.className = 'status' + (cls ? ' ' + cls : '');
  el.textContent = text;
}

function renderImages(items) {
  const map = { gFull: 'full', gTiles: 'tiles', gUp: 'up' };
  for (const [elId, key] of Object.entries(map)) {
    const box = $(elId);
    const subset = items.filter((i) => i.key.startsWith(key + '-'));
    if (!subset.length) { box.innerHTML = '<div class="ph">无</div>'; continue; }
    box.innerHTML = '';
    subset.forEach((item) => {
      const img = document.createElement('img');
      img.src = item.url;
      img.alt = item.key;
      box.appendChild(img);
    });
  }
}

async function poll(jobId) {
  const res = await fetch('/api/web/jobs/' + jobId);
  if (!res.ok) { setStatus('查询失败', 'failed'); $('go').disabled = false; return; }
  const data = await res.json();
  $('meta').textContent = 'job ' + data.job_id
    + (data.prompt_id ? ' · prompt ' + data.prompt_id.slice(0, 8) : '')
    + ' · ' + data.elapsed_s + 's';
  if (data.status === 'queued') setStatus('排队中…');
  else if (data.status === 'running') setStatus('生成中…（约 2 分钟）');
  else if (data.status === 'done') {
    setStatus('完成：整图 ' + data.counts.full + ' · 切片 ' + data.counts.tiles + ' · 放大 ' + data.counts.up, 'done');
    renderImages(data.images);
    $('go').disabled = false;
    return;
  } else {
    setStatus('失败：' + (data.error || '未知错误'), 'failed');
    $('go').disabled = false;
    return;
  }
  timer = setTimeout(() => poll(jobId), 3000);
}

$('go').addEventListener('click', async () => {
  if (!picked) { setStatus('请先选择产品图', 'failed'); return; }
  const form = new FormData();
  form.append('image', picked);
  form.append('style', $('style').value);
  form.append('scheme', $('scheme').value);
  form.append('visual', $('visual').value);
  form.append('font', $('font').value);
  form.append('requirement', $('req').value);
  form.append('product_card', $('card').value);
  form.append('copy_text', $('copy').value);

  $('go').disabled = true;
  if (timer) clearTimeout(timer);
  setStatus('提交中…');
  try {
    const res = await fetch('/api/web/jobs', { method: 'POST', body: form });
    const data = await res.json();
    if (!res.ok) { setStatus('提交失败：' + (data.detail || res.status), 'failed'); $('go').disabled = false; return; }
    poll(data.job_id);
  } catch (e) {
    setStatus('提交失败：' + e.message, 'failed');
    $('go').disabled = false;
  }
});
</script></body></html>"""


def login_page(error: str = "") -> str:
    return LOGIN_HTML.replace("__STYLE__", STYLE).replace("__ERROR__", error)


def app_page(user: str) -> str:
    return APP_HTML.replace("__STYLE__", STYLE).replace("__USER__", user)
