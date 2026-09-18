"""事件层：事件构造 helper（type=函数名）+ WS 广播总线。前后端协议单一真源。"""
import asyncio
import hashlib
import json
import re
import time


def chat_delta(text, role="assistant"):
    return {"type": "chat_delta", "role": role, "text": text}


def chat_done():
    return {"type": "chat_done"}


def user_message(text="", file=None, client_message_id="", turn_id=""):
    return {
        "type": "user_message", "text": text, "file": file,
        "client_message_id": client_message_id, "turn_id": turn_id,
    }


def thinking(text):
    return {"type": "thinking", "text": text}


def tool_call(name, summary="", status="ok"):
    return {"type": "tool_call", "name": name, "summary": summary, "status": status}


def tool_event(id, name, status="running", args="", cmd="", detail="", seq=None):
    """统一工具事件（Claude Code 式）：前端按 id 原地更新同一行——running→ok/fail。
    status: running(执行中/心跳) | ok | fail | interrupted。args=参数摘要；cmd=等价底层命令(可折叠);
    detail=结果摘要；seq=套件用例号(可选)。心跳=对同一 id 重发 running(带最新 detail 倒计时)。"""
    return {"type": "tool_event", "id": str(id), "name": name, "status": status,
            "args": args, "cmd": cmd, "detail": detail, "seq": seq}


def step_timing(point: str, actor: str, step: dict | None = None, *,
                architecture: str = "", agent_id: str = "", **fields) -> dict:
    """Durable, millisecond step-boundary observation; never changes execution state."""
    identity = {
        key: (step or {}).get(key)
        for key in ("step_id", "kind", "index", "seq")
        if (step or {}).get(key) is not None
    }
    return {
        "type": "step_timing", "point": str(point),
        "observed_at_ms": time.time_ns() // 1_000_000,
        "actor": str(actor or "unknown"), "agent_id": str(agent_id or ""),
        "architecture": str(architecture or ""), **identity, **fields,
    }


def tool_result_outcome(content, is_error: bool) -> tuple[str, str]:
    """Map an SDK ToolResult to UI semantics; user Stop is not a tool failure."""
    raw = str(content or "").lower()
    if is_error and ("user doesn't want to proceed with this tool use" in raw
                     or "tool use was rejected" in raw):
        return "interrupted", "已中断"
    domain_error = False
    result_text = tool_result_text(content)
    if result_text:
        try:
            payload = json.loads(result_text)
        except (TypeError, ValueError):
            payload = None
        domain_error = isinstance(payload, dict) and (
            bool(payload.get("error")) or payload.get("ok") is False
        )
    failed = bool(is_error or domain_error)
    return ("fail" if failed else "ok", safe_result_detail(content, failed))


def tool_result_text(content) -> str:
    """Convert every textual ToolResult payload into durable chat text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            item_type = str(
                item.get("type", "") if isinstance(item, dict)
                else getattr(item, "type", "")
            ).lower()
            if item_type in {"image", "audio"} or item_type.endswith((".image", ".audio")):
                continue
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict) and item.get("text") is not None:
                text = str(item.get("text") or "").strip()
            elif hasattr(item, "text"):
                text = str(getattr(item, "text", "") or "").strip()
            else:
                text = json.dumps(item, ensure_ascii=False, default=str).strip()
            if text:
                parts.append(text)
        return "\n".join(parts)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False, default=str).strip()
    return str(content).strip()


_SECRET_RESULT = re.compile(
    r"(?i)(api[_ -]?key|token|cookie|password|passwd|密码)(\s*[:=]\s*)([^\s,;\]}]+)"
)


def safe_result_detail(content, is_error: bool = False, limit: int = 300) -> str:
    """Small, redacted ToolResult summary shared by all execution architectures."""
    if not is_error:
        return "完成"
    if isinstance(content, list):
        raw = " ".join(
            str(item.get("text") or "") for item in content if isinstance(item, dict)
        )
    elif isinstance(content, dict):
        raw = json.dumps(content, ensure_ascii=False, default=str)
    else:
        raw = str(content or "")
    raw = raw.strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            for key in ("error", "reason", "message", "action"):
                value = parsed.get(key)
                if value:
                    raw = str(value)
                    break
    raw = " ".join(raw.split())
    raw = _SECRET_RESULT.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", raw)
    if not raw:
        return "执行失败，工具未返回错误详情" if is_error else "完成"
    return raw[:limit] + ("…" if len(raw) > limit else "")


def screen(b64, mime="image/png"):
    return {"type": "screen", "data": b64, "mime": mime}


def device_status(snapshot: dict):
    """Companion/ADB 设备状态；不包含 API Key 等敏感信息。"""
    return {"type": "device_status", "device": snapshot}


def _fingerprint(value: object, length: int = 64) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:length]


_SAFE_DIAGNOSTIC_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def _safe_diagnostic_name(value: object, fallback: str) -> str:
    name = str(value or "")
    return name if _SAFE_DIAGNOSTIC_NAME.fullmatch(name) else fallback


def device_link(previous: dict | None, current: dict | None) -> dict:
    """Durable ADB transition containing stable classifications and hashed serials only."""
    previous = dict(previous or {})
    current = dict(current or {})
    status = _safe_diagnostic_name(current.get("status"), "unknown")
    diagnostic_reason = _safe_diagnostic_name(current.get("adb_error_type"), "")
    reasons = {
        "checking": "checking",
        "ready": "device_ready",
        "disconnected": "selected_missing",
        "offline": "device_offline",
        "unauthorized": "device_unauthorized",
        "multiple_devices": "multiple_devices",
        "no_device": "no_device",
        "adb_missing": "adb_missing",
        "adb_error": _safe_diagnostic_name(
            current.get("adb_error_type"), "adb_error",
        ),
    }
    selected_serial = current.get("selected_serial") or previous.get("selected_serial")
    device_states = []
    for device in current.get("devices") or []:
        if not isinstance(device, dict):
            continue
        device_states.append({
            "serial_hash": _fingerprint(device.get("serial")),
            "state": _safe_diagnostic_name(device.get("state"), "unknown"),
        })
    device_states.sort(key=lambda item: (item["serial_hash"], item["state"]))
    return {
        "type": "device_link",
        "observed_at_ms": time.time_ns() // 1_000_000,
        "previous_status": _safe_diagnostic_name(previous.get("status"), "unknown"),
        "status": status,
        "reason": diagnostic_reason or reasons.get(status, "unknown"),
        "selected_serial_hash": _fingerprint(selected_serial),
        "devices": device_states,
        "adb_returncode": (
            current.get("adb_returncode")
            if isinstance(current.get("adb_returncode"), int)
            and not isinstance(current.get("adb_returncode"), bool)
            else None
        ),
    }


def executor_failure(exc: BaseException, *, source: str, failure_class: str,
                     child_error_type: str = "", process_alive=None,
                     process_exitcode=None) -> dict:
    """Durable executor failure signature; raw exception text is intentionally omitted."""
    exitcode = (
        process_exitcode
        if isinstance(process_exitcode, int) and not isinstance(process_exitcode, bool)
        else None
    )
    return {
        "type": "executor_failure",
        "observed_at_ms": time.time_ns() // 1_000_000,
        "source": _safe_diagnostic_name(source, "unknown"),
        "failure_class": _safe_diagnostic_name(failure_class, "unknown"),
        "error_type": _safe_diagnostic_name(type(exc).__name__, "Exception"),
        "child_error_type": _safe_diagnostic_name(child_error_type, "")
        if child_error_type else "",
        "process_alive": process_alive if isinstance(process_alive, bool) else None,
        "process_exitcode": exitcode,
        "message_fingerprint": _fingerprint(str(exc), length=16),
    }


def suite_start(run_id, cases):
    return {"type": "suite_start", "run_id": run_id, "cases": cases}


def case_start(seq):
    return {"type": "case_start", "seq": seq}


def case_result(**kw):
    return {"type": "case_result", **kw}


def suite_done(run_id, summary):
    return {"type": "suite_done", "run_id": run_id, "summary": summary}


def download_ready(run_id, filename):
    return {"type": "download_ready", "run_id": run_id, "filename": filename}


def need_human(question):
    return {"type": "need_human", "question": question}


def error(msg):
    return {"type": "error", "msg": msg}


def warn(msg):
    """非致命提醒：本轮继续跑，只是告诉用户"有东西不对，已按兜底处理"。
    刻意不复用 error——前端 error 分支会 unlock() 解锁发送键(语义=本轮结束)，
    一条提醒触发它会让用户在套件仍在跑时误以为能再发，撞上 /message 的 409 并发护栏。"""
    return {"type": "warn", "msg": msg}


class EventBus:
    """JSON 事件广播；快照回放期间阻止可靠事件越过 cursor。"""

    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()
        self.clients: list = []
        self._client_locks: dict = {}
        self._registry_lock = asyncio.Lock()

    @property
    def client_count(self) -> int:
        return len(self.clients)

    async def put(self, ev: dict):
        async with self._registry_lock:
            await self.q.put(ev)

    async def subscribe(self, websocket, replay_loader=None):
        """注册客户端并在同一可靠事件临界区完成 cursor 回放。"""
        async with self._registry_lock:
            if websocket not in self.clients:
                self.clients.append(websocket)
                self._client_locks[websocket] = asyncio.Lock()
            replay = replay_loader() if replay_loader else []
            lock = self._client_locks[websocket]
            async with lock:
                for event in replay:
                    await websocket.send_json(event)

    async def unsubscribe(self, websocket):
        async with self._registry_lock:
            if websocket in self.clients:
                self.clients.remove(websocket)
            self._client_locks.pop(websocket, None)

    async def _send_json(self, websocket, event):
        lock = self._client_locks.get(websocket)
        if lock is None:
            return
        async with lock:
            await websocket.send_json(event)

    async def broadcast_loop(self):
        while True:
            ev = await self.q.get()
            for ws in list(self.clients):
                try:
                    await self._send_json(ws, ev)
                except Exception:
                    await self.unsubscribe(ws)
