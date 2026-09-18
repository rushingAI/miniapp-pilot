import asyncio
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A  # noqa: E402


class _FakeInteractiveManager:
    def __init__(self, history=None):
        self.history = list(history or [])
        self.busy = False
        self.active = bool(history)
        self.run_id = "interactive-run"
        self.suite_running = False
        self.active_suite = {}
        self.turn_state = "idle"
        self.stop_reason = ""
        self.submitted = None
        self.appended = None
        self.reconfigured = None
        self.controlled = []
        self.device_links = []
        self.session_effort = "high"
        self.selected_effort = "high"

    def events_after(self, after=0):
        return [event for event in self.history if event.get("event_seq", 0) > after]

    async def submit(self, config, text, file=None, effort="high", client_message_id="",
                     received_perf_ns=None, received_utc="", turn_mode=""):
        self.submitted = {
            "config": config, "text": text, "file": file,
            "effort": effort, "client_message_id": client_message_id,
            "received_perf_ns": received_perf_ns, "received_utc": received_utc,
            "turn_mode": turn_mode,
        }
        return asyncio.create_task(asyncio.sleep(0))

    async def append_input(self, text, client_message_id=""):
        self.appended = {"text": text, "client_message_id": client_message_id}
        return True

    async def reconfigure_provider(self, provider_env, model="", effort=""):
        self.reconfigured = {
            "provider_env": dict(provider_env), "model": model, "effort": effort,
        }
        return True

    async def control(self, action, reason="user_stop"):
        self.controlled.append((action, reason))
        return bool(self.busy and action == "stop")

    async def record_device_link(self, previous, current):
        self.device_links.append((previous, current))


def test_changed_device_snapshot_is_persisted_and_broadcast(monkeypatch):
    manager = _FakeInteractiveManager([{"type": "user_message", "event_seq": 1}])
    published = []
    bound = []

    class FakeBus:
        async def put(self, event):
            published.append(event)

    previous = {"status": "ready", "selected_serial": "phone-a", "devices": []}
    current = {"status": "disconnected", "selected_serial": None, "devices": []}
    monkeypatch.setattr(A, "INTERACTIVE_MANAGER", manager)
    monkeypatch.setattr(A, "BUS", FakeBus())
    monkeypatch.setattr(A, "_bind_device", bound.append)

    result = asyncio.run(A._publish_device_snapshot(previous, current))

    assert result == current
    assert bound == [current]
    assert manager.device_links == [(previous, current)]
    assert published == [{"type": "device_status", "device": current}]


def test_repeated_malformed_adb_output_does_not_clear_bound_device(monkeypatch):
    class FlakySupervisor(A.DeviceSupervisor):
        def __init__(self):
            super().__init__(preferred_serial="phone-a")
            self.outputs = iter([
                "List of devices attached\nphone-a\tdevice model:A\n",
                "", "", "",
            ])

        def _run(self, args, timeout=6):
            return subprocess.CompletedProcess(
                args, 0, stdout=next(self.outputs), stderr="",
            )

        def _detail(self, serial):
            return {"model": serial}

    supervisor = FlakySupervisor()
    assert supervisor.refresh(preserve_missing_selection=True)["status"] == "ready"
    malformed = supervisor.refresh(preserve_missing_selection=True)
    cleared = []
    configured = []
    monkeypatch.setattr(A, "ACTIVE_DEVICE", "phone-a")
    monkeypatch.setattr(A.tools.server, "clear_device", lambda: cleared.append(True))
    monkeypatch.setattr(
        A.tools.server, "configure_device", lambda serial: configured.append(serial),
    )

    A._bind_device(malformed)

    assert malformed["status"] == "ready"
    assert malformed["selected_serial"] == "phone-a"
    assert malformed["adb_error_type"] == "malformed_output"
    assert A.ACTIVE_DEVICE == "phone-a"
    assert cleared == []
    assert configured == []


def test_snapshot_returns_durable_history_for_refresh(monkeypatch):
    history = [
        {"type": "user_message", "event_seq": 1, "event_id": "e1", "text": "第一轮"},
        {"type": "chat_delta", "event_seq": 2, "event_id": "e2", "text": "收到"},
        {"type": "tool_event", "event_seq": 3, "event_id": "e3", "name": "tap"},
    ]
    manager = _FakeInteractiveManager(history)
    monkeypatch.setattr(A, "INTERACTIVE_MANAGER", manager)
    monkeypatch.setattr(A.DEVICE_SUPERVISOR, "snapshot", lambda: {"status": "ready"})

    result = asyncio.run(A.snapshot(after=1))

    assert result["events"] == history[1:]
    assert result["last_event_seq"] == 3
    assert result["run_id"] == "interactive-run"


def test_status_exposes_the_shared_build_version(monkeypatch):
    monkeypatch.setattr(A.DEVICE_SUPERVISOR, "snapshot", lambda: {"status": "ready"})

    result = asyncio.run(A.status())

    assert result["app_version"] == A.APP_VERSION












def test_status_exposes_unified_turn_state_at_top_level(monkeypatch):
    manager = _FakeInteractiveManager()
    manager.turn_state = "stopped"
    manager.stop_reason = "provider_auth_error"
    manager.active_suite = {"suite_id": "suite-1", "current_step": {"step_id": "case-2"}}
    monkeypatch.setattr(A, "INTERACTIVE_MANAGER", manager)
    monkeypatch.setattr(A.DEVICE_SUPERVISOR, "snapshot", lambda: {"status": "ready"})

    result = asyncio.run(A.status())

    assert result["turn_state"] == "stopped"
    assert result["stop_reason"] == "provider_auth_error"
    assert result["active_suite"]["suite_id"] == "suite-1"
    assert result["busy"] is False


def test_control_endpoint_rejects_pause_and_resume(monkeypatch):
    manager = _FakeInteractiveManager()
    manager.busy = True
    monkeypatch.setattr(A, "INTERACTIVE_MANAGER", manager)

    for action in ("pause", "resume"):
        response = asyncio.run(A.control({"action": action}))
        assert response.status_code == 400
    stopped = asyncio.run(A.control({"action": "stop"}))
    assert stopped == {"ok": True, "action": "stop"}


def test_frontend_renders_status_version_in_the_persistent_header():
    web_dir = Path(__file__).resolve().parent.parent
    html = (web_dir / "index.html").read_text(encoding="utf-8")
    javascript = (web_dir / "ws.js").read_text(encoding="utf-8")

    assert 'id="appVersion"' in html
    assert "onAppVersion(s.app_version)" in javascript
    assert "`v${value}`" in javascript
    assert A.APP_VERSION not in html
    assert A.APP_VERSION not in javascript


def test_frontend_has_one_dynamic_action_button_and_choices_before_optimistic_render():
    web_dir = Path(__file__).resolve().parent.parent
    html = (web_dir / "index.html").read_text(encoding="utf-8")
    chat = (web_dir / "chat.js").read_text(encoding="utf-8")

    assert html.count('id="sendBtn"') == 1
    assert 'id="pauseBtn"' not in html
    assert 'id="resumeBtn"' not in html
    assert 'id="stopBtn"' not in html
    choice_guard = chat.index("if (!wasBusy && activeSuite && activeSuite.suite_id)")
    optimistic_render = chat.index("request.optimistic = userMsg", choice_guard)
    assert choice_guard < optimistic_render


def test_message_passes_stable_client_id_to_interactive_executor(monkeypatch):
    manager = _FakeInteractiveManager()
    device = {"status": "ready", "selected_serial": "phone-a", "detail": {"model": "S22"}}
    monkeypatch.setattr(A, "INTERACTIVE_MANAGER", manager)
    monkeypatch.setattr(A, "TURN_TASK", None)
    monkeypatch.setattr(A, "PROVIDER_CONFIGURED", True)
    monkeypatch.setattr(A, "PROVIDER_ENV", {"ANTHROPIC_API_KEY": "in-memory-only"})
    monkeypatch.setattr(A.DEVICE_SUPERVISOR, "snapshot", lambda: device)
    monkeypatch.setattr(A.tools, "RUN_CONTEXT", {})

    result = asyncio.run(A.message({
        "text": "打开设置", "client_message_id": "browser-message-1", "effort": "high",
    }))

    assert result["ok"] is True
    assert manager.submitted["text"] == "打开设置"
    assert manager.submitted["client_message_id"] == "browser-message-1"
    assert manager.submitted["config"]["serial"] == "phone-a"
    assert manager.submitted["config"]["provider_env"]["ANTHROPIC_API_KEY"] == "in-memory-only"
    assert manager.submitted["config"]["key_fingerprint"]
    assert manager.submitted["received_perf_ns"] > 0
    assert manager.submitted["received_utc"].endswith("Z")


def test_message_appends_input_to_running_sdk_session(monkeypatch):
    manager = _FakeInteractiveManager()
    manager.busy = True
    monkeypatch.setattr(A, "INTERACTIVE_MANAGER", manager)
    monkeypatch.setattr(A, "TURN_TASK", None)
    monkeypatch.setattr(A, "PROVIDER_CONFIGURED", True)
    monkeypatch.setattr(A, "PROVIDER_ENV", {"ANTHROPIC_API_KEY": "memory"})

    result = asyncio.run(A.message({
        "text": "下一条先检查登录态", "client_message_id": "instruction-1",
    }))

    assert result["appended"] is True
    assert manager.appended == {
        "text": "下一条先检查登录态", "client_message_id": "instruction-1",
    }


def test_idle_active_suite_requires_explicit_turn_mode(monkeypatch):
    manager = _FakeInteractiveManager()
    manager.suite_running = False
    manager.active_suite = {"suite_id": "suite-1", "current_step": {"step_id": "case-2"}}
    device = {"status": "ready", "selected_serial": "phone-a", "detail": {}}
    monkeypatch.setattr(A, "INTERACTIVE_MANAGER", manager)
    monkeypatch.setattr(A, "TURN_TASK", None)
    monkeypatch.setattr(A, "PROVIDER_CONFIGURED", True)
    monkeypatch.setattr(A, "PROVIDER_ENV", {"ANTHROPIC_API_KEY": "memory"})
    monkeypatch.setattr(A.DEVICE_SUPERVISOR, "snapshot", lambda: device)
    monkeypatch.setattr(A.tools, "RUN_CONTEXT", {})

    required = asyncio.run(A.message({"text": "请重试"}))
    body = json.loads(required.body)
    assert required.status_code == 409
    assert body["code"] == "active_suite_choice_required"
    assert body["active_suite"]["suite_id"] == "suite-1"


def test_stopped_active_suite_preserves_start_new_choice_when_child_not_running(monkeypatch):
    manager = _FakeInteractiveManager()
    manager.suite_running = False
    manager.turn_state = "stopped"
    manager.active_suite = {
        "suite_id": "suite-old", "current_step": {"step_id": "setup-0"},
    }
    device = {"status": "ready", "selected_serial": "phone-a", "detail": {}}
    monkeypatch.setattr(A, "INTERACTIVE_MANAGER", manager)
    monkeypatch.setattr(A, "TURN_TASK", None)
    monkeypatch.setattr(A, "PROVIDER_CONFIGURED", True)
    monkeypatch.setattr(A, "PROVIDER_ENV", {"ANTHROPIC_API_KEY": "memory"})
    monkeypatch.setattr(A.DEVICE_SUPERVISOR, "snapshot", lambda: device)
    monkeypatch.setattr(A.tools, "RUN_CONTEXT", {})

    accepted = asyncio.run(A.message({
        "text": "执行 v1.6", "turn_mode": "start_new_suite",
    }))

    assert accepted["ok"] is True
    assert manager.submitted["turn_mode"] == "start_new_suite"


def test_stopped_active_suite_allows_provider_change_without_changing_identity(monkeypatch):
    manager = _FakeInteractiveManager([{"type": "turn_stopped", "event_seq": 1}])
    manager.turn_state = "stopped"
    manager.suite_running = True
    manager.active_suite = {
        "suite_id": "suite-keep", "current_step": {"step_id": "case-4"},
    }
    before = dict(manager.active_suite)
    monkeypatch.setattr(A, "INTERACTIVE_MANAGER", manager)
    monkeypatch.setattr(A, "PROVIDER_ENV", {
        "ANTHROPIC_API_KEY": "old-key-value", "ANTHROPIC_BASE_URL": "https://old.example",
        "ANTHROPIC_MODEL": "old-model",
    })

    result = asyncio.run(A.set_provider_key({
        "api_key": "new-key-value", "anthropic_base_url": "https://new.example",
        "model": "new-model",
    }))

    assert result["ok"] is True
    assert manager.reconfigured["model"] == "new-model"
    assert manager.active_suite == before

    accepted = asyncio.run(A.message({
        "text": "请重试", "turn_mode": "continue_active_suite",
    }))
    assert accepted["ok"] is True
    assert manager.submitted["turn_mode"] == "continue_active_suite"
