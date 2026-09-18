"""对话式通用手机 agent 前端后端。首版 Companion：单会话、单次选择一台设备。"""
import asyncio
import base64
import glob
import hashlib
import io
import logging
import os
import subprocess
import sys
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

_HERE = os.path.dirname(os.path.abspath(__file__))      # device-mcp/agent/web
_AGENT = os.path.dirname(_HERE)                          # device-mcp/agent
_DEVICE_MCP = os.path.dirname(_AGENT)                    # device-mcp
_REPO = os.path.dirname(os.path.dirname(_AGENT))         # miniapp-pilot
for _p in (_DEVICE_MCP, _AGENT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from runtime_paths import data_root
DATA_ROOT = str(data_root())
UPLOADS = os.path.join(DATA_ROOT, "uploads") if DATA_ROOT else os.path.join(_HERE, "uploads")
os.makedirs(UPLOADS, exist_ok=True)
OUT_BASE = DATA_ROOT
os.makedirs(OUT_BASE, exist_ok=True)
ALLOWED_EXT = {".xlsx", ".png", ".jpg", ".jpeg", ".txt", ".csv"}

import tools    # noqa: E402
import events   # noqa: E402
from build_version import APP_VERSION  # noqa: E402
from devices import DeviceSupervisor  # noqa: E402
from auth import claude_sdk_env, DEFAULT_KIMI_MODEL  # noqa: E402
from run_store import RunCoordinator  # noqa: E402
from executor import InteractiveExecutorManager  # noqa: E402
from telemetry import provider_key_fingerprint, utc_ts_ms  # noqa: E402
from plugin_settings import (  # noqa: E402
    SettingsContext, discover_settings_plugins, plugin_descriptors,
)
from provider_store import ProviderStore, ProviderStoreError  # noqa: E402

app = FastAPI()
LOG = logging.getLogger(__name__)
BUS = events.EventBus()
CONTROL = None
SCREEN_ON = False
TURN_TASK = None   # 当前对话轮的 asyncio.Task；回合制并发护栏用
DEVICE_SUPERVISOR = DeviceSupervisor(preferred_serial=os.environ.get("MINIAPP_PILOT_DEVICE_SERIAL", ""))
ACTIVE_DEVICE = ""
DEVICE_LOOP_ON = False
RUN_COORDINATOR = None
INTERACTIVE_MANAGER = None
PROVIDER_CONFIGURED = bool(claude_sdk_env().get("ANTHROPIC_API_KEY") or
                           claude_sdk_env().get("ANTHROPIC_AUTH_TOKEN"))
PROVIDER_KEY_FINGERPRINT = "environment" if PROVIDER_CONFIGURED else ""
PROVIDER_ENV = claude_sdk_env() if PROVIDER_CONFIGURED else None
PROVIDER_MODEL = ((PROVIDER_ENV or {}).get("ANTHROPIC_MODEL") or DEFAULT_KIMI_MODEL)
PROVIDER_STORAGE = "developer_env" if PROVIDER_CONFIGURED else "memory"
_SETTINGS_ROOT = DATA_ROOT
PROVIDER_STORE = ProviderStore(os.path.join(_SETTINGS_ROOT, "provider.json"))


def _restore_provider_config() -> bool:
    """Restore an opted-in provider without ever exposing its key to the browser."""
    global PROVIDER_CONFIGURED, PROVIDER_KEY_FINGERPRINT, PROVIDER_ENV, PROVIDER_MODEL, PROVIDER_STORAGE
    if PROVIDER_CONFIGURED:
        return False
    try:
        saved = PROVIDER_STORE.load()
    except ProviderStoreError as exc:
        LOG.warning("Unable to restore saved provider credential: %s", exc)
        return False
    if not saved:
        return False
    provider_env = claude_sdk_env(
        api_key=saved["api_key"], base_url=saved["base_url"], model=saved["model"],
    )
    PROVIDER_ENV = provider_env
    PROVIDER_MODEL = provider_env["ANTHROPIC_MODEL"]
    PROVIDER_KEY_FINGERPRINT = provider_key_fingerprint(provider_env)
    PROVIDER_CONFIGURED = True
    PROVIDER_STORAGE = "system_credential"
    return True


def _settings_busy() -> bool:
    return _mutation_locked()


def _mutation_locked() -> bool:
    manager = INTERACTIVE_MANAGER
    if TURN_TASK and not TURN_TASK.done():
        return True
    if not manager:
        return False
    return bool(getattr(manager, "mutation_locked", manager.busy))


SETTINGS_PLUGINS = discover_settings_plugins(SettingsContext(
    data_root=DATA_ROOT,
    uploads_dir=UPLOADS,
    is_busy=_settings_busy,
))
for _plugin in SETTINGS_PLUGINS:
    app.include_router(
        _plugin.router,
        prefix=f"/api/settings/plugins/{_plugin.plugin_id}",
        tags=["settings-plugin"],
    )


# ---------- 安全工具（纯函数，可单测） ----------
def safe_filename(name):
    base = os.path.basename(name or "")
    if not base or base.startswith("."):
        return None
    ext = os.path.splitext(base)[1].lower()
    return base if ext in ALLOWED_EXT else None


def evidence_path(out_dir, name):
    if not out_dir or not name or name != os.path.basename(name):
        return None   # 只接受裸文件名；含路径分隔/../ 一律拒
    rp = os.path.realpath(os.path.join(out_dir, name))
    root = os.path.realpath(out_dir)
    return rp if rp.startswith(root + os.sep) and os.path.isfile(rp) else None


PREVIEW_W = 540      # 前端 #screen 只显示 257x557 CSS px @dpr2 ≈ 514 物理像素宽，540 已够；
PREVIEW_Q = 75       # 推 1080 宽是 4.4 倍像素纯浪费(缩放时全丢弃)
# ⚠️ 这里降的是**给人看的预览**。AI 看的截图走完全独立的另一条路(tools.py:save_screenshot →
# server.py 自己一次 adb screencap → PIL 缩到长边 1568 → JPEG → MCP tool result，带 scale/coord_hint
# 让 AI 换算设备坐标)。改这里对 AI 判断零影响；反过来 1568 那个数字是实测调出来的(768 太糊会点偏)，别动。


def shrink_preview(raw: bytes):
    """全屏 PNG 字节 → (JPEG 字节, mime)。纯函数，可单测。坏数据退回原字节，绝不炸预览循环。"""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        if im.width > PREVIEW_W:
            im = im.resize((PREVIEW_W, round(im.height * PREVIEW_W / im.width)), Image.BILINEAR)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=PREVIEW_Q)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return raw, "image/png"


def run_dir(run_id):
    if RUN_COORDINATOR:
        current = RUN_COORDINATOR.store.run_dir(run_id)
        if current:
            return current
    rid = os.path.basename(run_id or "")
    if not rid:
        return None
    d = os.path.realpath(os.path.join(OUT_BASE, f"run-web-{rid}"))
    return d if d.startswith(os.path.realpath(OUT_BASE) + os.sep) and os.path.isdir(d) else None


class Control:
    def __init__(self):
        self.stopped = False
        self.stop_reason = ""
        self.control_seq = 0


def _grab_preview():
    """采一帧并降载。跑在线程里：adb screencap 实测 370ms、PIL 编码 ~8ms，都不能占事件循环。"""
    snap = DEVICE_SUPERVISOR.snapshot()
    serial = snap.get("selected_serial") if snap.get("status") == "ready" else ""
    if not serial:
        return None
    result = subprocess.run(
        ["adb", "-s", serial, "exec-out", "screencap", "-p"], capture_output=True)
    if result.returncode != 0:
        return None
    raw = result.stdout
    if not raw:
        return None            # 0 字节 = FLAG_SECURE 安全窗口，没图可推
    return shrink_preview(raw)


async def _screen_loop():
    while SCREEN_ON:
        # 没人连着就别采：一次 adb screencap 要 370ms，无客户端时纯空转。
        # 代价只是关掉浏览器再打开时首帧要等 ≤1.6s。
        if BUS.client_count:
            try:
                got = await asyncio.to_thread(_grab_preview)
                if got:
                    data, mime = got
                    await BUS.put(events.screen(base64.b64encode(data).decode(), mime))
            except Exception:
                pass
        await asyncio.sleep(1.6)


def _bind_device(snapshot: dict) -> None:
    """把 supervisor 选择结果同步给 in-process device MCP；断开时必须清旧缓存。"""
    global ACTIVE_DEVICE
    serial = snapshot.get("selected_serial") if snapshot.get("status") == "ready" else ""
    if serial and serial != ACTIVE_DEVICE:
        tools.server.configure_device(serial)
        ACTIVE_DEVICE = serial
    elif not serial and ACTIVE_DEVICE:
        tools.server.clear_device()
        ACTIVE_DEVICE = ""


async def _publish_device_snapshot(previous: dict | None, snapshot: dict) -> dict:
    """Persist and broadcast a changed device snapshot without altering control flow."""
    _bind_device(snapshot)
    if snapshot == previous:
        return snapshot
    if INTERACTIVE_MANAGER:
        await INTERACTIVE_MANAGER.record_device_link(previous, snapshot)
    await BUS.put(events.device_status(snapshot))
    return snapshot


async def _device_loop():
    """周期探测 USB/ADB 状态；只在状态变化时广播，避免刷屏。"""
    last = None
    while DEVICE_LOOP_ON:
        preserve = bool(INTERACTIVE_MANAGER and INTERACTIVE_MANAGER.active)
        snapshot = await asyncio.to_thread(
            DEVICE_SUPERVISOR.refresh, preserve_missing_selection=preserve,
        )
        last = await _publish_device_snapshot(last, snapshot)
        await asyncio.sleep(2.0)


@app.on_event("startup")
async def _startup():
    global CONTROL, SCREEN_ON, DEVICE_LOOP_ON, RUN_COORDINATOR, INTERACTIVE_MANAGER
    await asyncio.to_thread(_restore_provider_config)
    asyncio.create_task(BUS.broadcast_loop())
    CONTROL = Control()
    RUN_COORDINATOR = RunCoordinator(
        os.path.join(OUT_BASE, "runs"), BUS.put, DEVICE_SUPERVISOR.snapshot
    )
    INTERACTIVE_MANAGER = InteractiveExecutorManager(RUN_COORDINATOR)
    tools.CONTROL = CONTROL   # 同进程回退路径的统一停止屏障；子进程各自注入同契约 Control
    initial_provider_env = PROVIDER_ENV
    tools.RUN_CONTEXT = {"uploads_dir": UPLOADS, "out_base": OUT_BASE,
                         "emit": BUS.put, "control": CONTROL,
                         "provider_env": initial_provider_env,
                         "key_fingerprint": provider_key_fingerprint(initial_provider_env),
                         "run_coordinator": RUN_COORDINATOR}
    snapshot = await asyncio.to_thread(DEVICE_SUPERVISOR.refresh)
    _bind_device(snapshot)
    SCREEN_ON = True
    DEVICE_LOOP_ON = True
    asyncio.create_task(_screen_loop())
    asyncio.create_task(_device_loop())


@app.on_event("shutdown")
async def _shutdown():
    global SCREEN_ON, DEVICE_LOOP_ON
    SCREEN_ON = False
    DEVICE_LOOP_ON = False
    if INTERACTIVE_MANAGER:
        await INTERACTIVE_MANAGER.close()


@app.get("/api/status")
async def status():
    busy = bool(INTERACTIVE_MANAGER and INTERACTIVE_MANAGER.busy)
    turn_state = getattr(INTERACTIVE_MANAGER, "turn_state", "idle") if INTERACTIVE_MANAGER else "idle"
    stop_reason = getattr(INTERACTIVE_MANAGER, "stop_reason", "") if INTERACTIVE_MANAGER else ""
    active_suite = getattr(INTERACTIVE_MANAGER, "active_suite", {}) if INTERACTIVE_MANAGER else {}
    return {
        "app_version": APP_VERSION,
        "busy": busy,
        "turn_state": turn_state,
        "stop_reason": stop_reason,
        "active_suite": active_suite,
        "device": DEVICE_SUPERVISOR.snapshot(),
        "provider": {
            "configured": PROVIDER_CONFIGURED,
            "model": PROVIDER_MODEL,
            "fingerprint": PROVIDER_KEY_FINGERPRINT,
            "storage": PROVIDER_STORAGE,
        },
        "interactive": {
            "active": bool(INTERACTIVE_MANAGER and INTERACTIVE_MANAGER.active),
            "busy": busy,
            "turn_state": turn_state,
            "stop_reason": stop_reason,
            "mutation_locked": bool(INTERACTIVE_MANAGER and getattr(
                INTERACTIVE_MANAGER, "mutation_locked", INTERACTIVE_MANAGER.busy)),
            "run_id": INTERACTIVE_MANAGER.run_id if INTERACTIVE_MANAGER else "",
            "selected_effort": getattr(INTERACTIVE_MANAGER, "selected_effort", "") if INTERACTIVE_MANAGER else "",
            "session_effort": getattr(INTERACTIVE_MANAGER, "session_effort", "") if INTERACTIVE_MANAGER else "",
            "active_suite": active_suite,
        },
    }


@app.get("/api/settings/plugins")
async def settings_plugins():
    return {"plugins": plugin_descriptors(SETTINGS_PLUGINS)}


@app.post("/api/provider-key")
async def set_provider_key(payload: dict):
    """替换 Provider；只有显式“记住此电脑”才写入系统凭据库。"""
    global PROVIDER_CONFIGURED, PROVIDER_KEY_FINGERPRINT, PROVIDER_ENV, PROVIDER_MODEL, PROVIDER_STORAGE
    if _mutation_locked():
        return JSONResponse({"error": "当前测试尚未结束，结束后才能更换模型配置"}, status_code=409)
    current_env = PROVIDER_ENV or {}
    key = str(payload.get("api_key") or current_env.get("ANTHROPIC_API_KEY")
              or current_env.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
    if len(key) < 8 or any(ch.isspace() for ch in key):
        return JSONResponse({"error": "请输入有效的 Kimi API Key"}, status_code=400)
    base_url = str(payload.get("anthropic_base_url") or current_env.get("ANTHROPIC_BASE_URL") or "").strip()
    if base_url:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or any(ch.isspace() for ch in base_url):
            return JSONResponse({"error": "ANTHROPIC_BASE_URL 必须是有效的 HTTP/HTTPS 地址"}, status_code=400)
    model = str(payload.get("model") or current_env.get("ANTHROPIC_MODEL") or "").strip()
    if len(model) > 128 or any(ch.isspace() for ch in model):
        return JSONResponse({"error": "模型名称不能包含空白，且不能超过 128 个字符"}, status_code=400)
    provider_env = claude_sdk_env(api_key=key, base_url=base_url, model=model)
    remember = payload.get("remember")
    try:
        if remember is True:
            await asyncio.to_thread(
                PROVIDER_STORE.save, key, provider_env["ANTHROPIC_BASE_URL"], provider_env["ANTHROPIC_MODEL"],
            )
            next_storage = "system_credential"
        elif remember is False:
            await asyncio.to_thread(PROVIDER_STORE.delete)
            next_storage = "memory"
        else:
            next_storage = "memory"
    except (ProviderStoreError, OSError) as exc:
        return JSONResponse({"error": str(exc) or "无法安全保存模型凭据"}, status_code=503)
    if INTERACTIVE_MANAGER and INTERACTIVE_MANAGER.active:
        try:
            await INTERACTIVE_MANAGER.reconfigure_provider(
                provider_env, model=provider_env["ANTHROPIC_MODEL"],
                effort=INTERACTIVE_MANAGER.session_effort or INTERACTIVE_MANAGER.selected_effort,
            )
        except Exception as exc:
            return JSONResponse({"error": f"模型服务重连失败：{str(exc)[:240]}"}, status_code=502)
    PROVIDER_ENV = provider_env
    PROVIDER_MODEL = provider_env["ANTHROPIC_MODEL"]
    PROVIDER_KEY_FINGERPRINT = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    PROVIDER_STORAGE = next_storage
    tools.RUN_CONTEXT["provider_env"] = provider_env
    tools.RUN_CONTEXT["key_fingerprint"] = PROVIDER_KEY_FINGERPRINT
    PROVIDER_CONFIGURED = True
    return {
        "ok": True,
        "provider": {
            "configured": True, "model": PROVIDER_MODEL,
            "fingerprint": PROVIDER_KEY_FINGERPRINT, "storage": PROVIDER_STORAGE,
        },
    }


@app.post("/api/device/select")
async def select_device(payload: dict):
    if _mutation_locked():
        return JSONResponse({"error": "当前任务运行中，请停止或等待完成后再切换设备"}, status_code=409)
    if INTERACTIVE_MANAGER and INTERACTIVE_MANAGER.active:
        await INTERACTIVE_MANAGER.close()
    try:
        snapshot = await asyncio.to_thread(DEVICE_SUPERVISOR.select, payload.get("serial", ""))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    _bind_device(snapshot)
    await BUS.put(events.device_status(snapshot))
    return {"ok": True, "device": snapshot}


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    fn = safe_filename(file.filename)
    if not fn:
        return JSONResponse({"error": "文件名或类型不允许"}, status_code=400)
    path = os.path.join(UPLOADS, fn)   # 统一写 uploads 根，TestExcel 按 basename 导入
    with open(path, "wb") as f:
        f.write(await file.read())
    sheets = []
    if fn.lower().endswith(".xlsx"):
        import openpyxl
        sheets = openpyxl.load_workbook(path, read_only=True).sheetnames
    return {"file_id": fn, "filename": fn, "sheets": sheets}


@app.post("/upload/tencent-sheet")
async def upload_tencent_sheet(payload: dict):
    """Create an upload-compatible xlsx snapshot from a Tencent Docs link."""
    from tencent_sheets import TencentDocsError, fetch_workbook_bytes

    url = str(payload.get("url") or "").strip()
    token = str(payload.get("token") or "").strip()
    try:
        raw, source = await fetch_workbook_bytes(url, token)
        import openpyxl
        workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        sheets = workbook.sheetnames
        if not sheets:
            raise TencentDocsError("腾讯表格中没有可读取的工作表")
        filename = f"腾讯用例_{source['file_id']}.xlsx"
        path = os.path.join(UPLOADS, filename)
        tmp_path = path + ".tmp"
        with open(tmp_path, "wb") as output:
            output.write(raw)
        os.replace(tmp_path, path)
    except TencentDocsError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": f"腾讯表格转换失败：{str(exc)[:240]}"}, status_code=400)
    return {"file_id": filename, "filename": filename, "sheets": sheets,
            "source": {"type": "tencent_docs", "url": source["url"]}}


EFFORTS = ("none", "low", "high", "max")


async def _pick_effort(raw) -> str:
    """前端传来的 Kimi 推理模式 → 合法值。none 表示关闭 Thinking。

    非法值回退 low 并推 warn 到对话。
    空串/缺字段=旧客户端或非前端直接 POST,静默回退,不刷警告(否则 warn 就成噪音了)。"""
    val = str(raw or "").strip().lower()
    if val in EFFORTS:
        return val
    if val:   # 传了东西但不认识 → 是 bug,别吞
        await BUS.put(events.warn(f"推理深度值非法({val})，已回退 low"))
    return "low"


@app.post("/message")
async def message(payload: dict):
    global TURN_TASK
    received_perf_ns = time.perf_counter_ns()
    received_utc = utc_ts_ms()
    text = (payload.get("text") or "").strip()
    file = payload.get("file")   # {filename, sheets} 或 None（随消息附带的已上传文件）
    if not text and not file:
        return JSONResponse({"error": "空消息"}, status_code=400)
    if not PROVIDER_CONFIGURED or not PROVIDER_ENV:
        return JSONResponse({"error": "请先在顶栏输入本人的 Kimi API Key"}, status_code=401)
    busy = ((TURN_TASK and not TURN_TASK.done()) or
            (INTERACTIVE_MANAGER and INTERACTIVE_MANAGER.busy))
    client_message_id = str(payload.get("client_message_id") or "").strip()[:100]
    if busy:
        if text and not file and INTERACTIVE_MANAGER and await INTERACTIVE_MANAGER.append_input(
                text, client_message_id=client_message_id):
            return {"ok": True, "appended": True,
                    "run_id": INTERACTIVE_MANAGER.run_id}
        return JSONResponse({"error": "当前仍在执行；只允许追加纯文本，附件请在停止后发送"}, status_code=409)

    turn_mode = str(payload.get("turn_mode") or "").strip()
    allowed_turn_modes = {"continue_active_suite", "start_new_suite", "unrelated_task"}
    active_suite = (
        getattr(INTERACTIVE_MANAGER, "active_suite", {}) if INTERACTIVE_MANAGER else {}
    )
    has_active_suite = bool((active_suite or {}).get("suite_id"))
    if has_active_suite and turn_mode not in allowed_turn_modes:
        return JSONResponse({
            "error": "当前有一项可恢复的测试，请先选择如何处理",
            "code": "active_suite_choice_required",
            "active_suite": active_suite,
        }, status_code=409)
    if not has_active_suite:
        turn_mode = ""

    # Key 设置中的模型名已映射到主 agent 和子 agent；Run 同步记录实际选择。
    tools.RUN_CONTEXT["model"] = PROVIDER_MODEL
    # 前端选的推理设置→套件子agent。只放行 low/high/max，防直接 POST 注入错值。
    # (EffortLevel 只是 type hint,transport 直接拼成 --effort 交给 CLI),错值不会在 Python 层报错。
    tools.RUN_CONTEXT["effort"] = await _pick_effort(payload.get("effort"))

    snapshot = DEVICE_SUPERVISOR.snapshot()
    effort = tools.RUN_CONTEXT["effort"]
    config = {
        "serial": snapshot.get("selected_serial") or "",
        "device": snapshot,
        "provider_env": PROVIDER_ENV,
        "model": PROVIDER_MODEL,
        "key_fingerprint": provider_key_fingerprint(PROVIDER_ENV),
        "uploads_dir": UPLOADS,
        "effort": effort,
        "architecture": "native_harness",
    }
    try:
        TURN_TASK = await INTERACTIVE_MANAGER.submit(
            config, text, file=file, effort=effort, client_message_id=client_message_id,
            received_perf_ns=received_perf_ns, received_utc=received_utc,
            turn_mode=turn_mode,
        )
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    # HTTP 已在 user_message 落盘后返回；后台任务异常已由 manager 写入 durable error。
    TURN_TASK.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
    return {"ok": True, "run_id": INTERACTIVE_MANAGER.run_id}


@app.post("/respond")
async def respond(payload: dict):
    if INTERACTIVE_MANAGER and await INTERACTIVE_MANAGER.respond(payload.get("answer", "")):
        return {"ok": True}
    return JSONResponse({"error": "无等待中的人工请求"}, status_code=400)


@app.post("/permission/respond")
async def respond_permission(payload: dict):
    if not isinstance(payload.get("allow"), bool):
        return JSONResponse({"error": "allow 必须是布尔值"}, status_code=400)
    if INTERACTIVE_MANAGER and await INTERACTIVE_MANAGER.respond_permission(payload["allow"]):
        return {"ok": True}
    return JSONResponse({"error": "无等待中的工具权限请求"}, status_code=400)


@app.post("/control")
async def control(payload: dict):
    act = payload.get("action")
    if act != "stop":
        return JSONResponse({"error": "未知控制动作"}, status_code=400)
    accepted = bool(INTERACTIVE_MANAGER and await INTERACTIVE_MANAGER.control("stop"))
    if not accepted:
        return JSONResponse({"error": "当前没有可停止的执行"}, status_code=409)
    return {"ok": True, "action": act}


@app.get("/api/snapshot")
async def snapshot(after: int = 0):
    history = INTERACTIVE_MANAGER.events_after(after) if INTERACTIVE_MANAGER else []
    return {
        "run_id": INTERACTIVE_MANAGER.run_id if INTERACTIVE_MANAGER else "",
        "busy": bool(INTERACTIVE_MANAGER and INTERACTIVE_MANAGER.busy),
        "turn_state": getattr(INTERACTIVE_MANAGER, "turn_state", "idle") if INTERACTIVE_MANAGER else "idle",
        "stop_reason": getattr(INTERACTIVE_MANAGER, "stop_reason", "") if INTERACTIVE_MANAGER else "",
        "active_suite": getattr(INTERACTIVE_MANAGER, "active_suite", {}) if INTERACTIVE_MANAGER else {},
        "events": history,
        "last_event_seq": max([int(e.get("event_seq") or 0) for e in history], default=int(after or 0)),
        "device": DEVICE_SUPERVISOR.snapshot(),
    }


@app.get("/download")
async def download(run_id: str = "", name: str = ""):
    d = run_dir(run_id)
    if d:
        if name and name == os.path.basename(name):
            candidates = glob.glob(os.path.join(d, "attempts", "*", "workspace", "output", name))
            candidates += glob.glob(os.path.join(d, "attempts", "*", name))
            for candidate in candidates:
                real = os.path.realpath(candidate)
                if real.startswith(os.path.realpath(d) + os.sep) and os.path.isfile(real):
                    return FileResponse(real, filename=name)
        cands = sorted(glob.glob(os.path.join(d, "*.xlsx")))
        cands += sorted(glob.glob(os.path.join(d, "attempts", "*", "*.xlsx")))
        if cands:
            return FileResponse(cands[0], filename=os.path.basename(cands[0]))
    return JSONResponse({"error": "暂无回填结果"}, status_code=404)


@app.get("/evidence")
async def evidence(run_id: str = "", name: str = ""):
    d = run_dir(run_id)
    p = evidence_path(d, name) if d else None
    if p:
        return FileResponse(p)
    return JSONResponse({"error": "禁止"}, status_code=403)


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    try:
        after = max(0, int(websocket.query_params.get("after", "0")))
    except (TypeError, ValueError):
        after = 0
    await BUS.subscribe(
        websocket,
        lambda: INTERACTIVE_MANAGER.events_after(after) if INTERACTIVE_MANAGER else [],
    )
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await BUS.unsubscribe(websocket)


app.mount("/", StaticFiles(directory=_HERE, html=True), name="static")
