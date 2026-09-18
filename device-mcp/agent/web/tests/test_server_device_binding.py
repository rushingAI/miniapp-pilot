import os
import subprocess
import sys

DEVICE_MCP = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, DEVICE_MCP)

import server  # noqa: E402


def test_text_adb_uses_utf8_with_replacement_on_windows(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(server, "_SERIAL", "device-a")
    monkeypatch.setattr(server.subprocess, "run", fake_run)

    result = server._adb("shell", "dumpsys", "window")

    assert result.stdout == "ok"
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"


def test_parse_wm_size_prefers_override():
    assert server._parse_wm_size(
        "Physical size: 1080x2340\nOverride size: 720x1560\n"
    ) == (720, 1560)


def test_configure_device_resets_cached_connection(monkeypatch):
    monkeypatch.delenv("MINIAPP_PILOT_DEVICE_SERIAL", raising=False)
    server._SERIAL = "old"
    server._DEV = object()

    server.configure_device("new-serial")

    assert os.environ["MINIAPP_PILOT_DEVICE_SERIAL"] == "new-serial"
    assert server._SERIAL == "new-serial"
    assert server._DEV is None

    server.clear_device()
    assert "MINIAPP_PILOT_DEVICE_SERIAL" not in os.environ
    assert server._SERIAL is None
    assert server._DEV is None


def test_invalidated_device_session_restarts_uiautomator_on_next_use(monkeypatch):
    class FakeDevice:
        def __init__(self):
            self.reset_calls = 0

        def reset_uiautomator(self):
            self.reset_calls += 1

    device = FakeDevice()
    monkeypatch.setattr(server.u2, "connect", lambda serial: device)
    monkeypatch.setattr(server, "_SERIAL", "phone-a")
    monkeypatch.setattr(server, "_DEV", object())

    server.invalidate_device_session("phone-a")

    assert server._DEV is None
    assert device.reset_calls == 0
    assert server._dev() is device
    assert device.reset_calls == 1
    assert server._dev() is device
    assert device.reset_calls == 1


def test_auto_select_refuses_multiple_ready_devices(monkeypatch):
    monkeypatch.delenv("MINIAPP_PILOT_DEVICE_SERIAL", raising=False)

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0], 0,
            stdout="List of devices attached\na\tdevice model:A\nb\tdevice model:B\n",
            stderr="",
        )

    monkeypatch.setattr(server.subprocess, "run", fake_run)

    try:
        server._serial(retries=1)
    except RuntimeError as exc:
        assert "多台" in str(exc)
    else:
        raise AssertionError("多台设备时不应偷偷选择第一台")


def test_screenshot_adb_failure_is_not_reported_as_secure(monkeypatch, tmp_path):
    server._SERIAL = "device-a"

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, stdout=b"", stderr=b"device offline")

    monkeypatch.setattr(server.subprocess, "run", fake_run)

    try:
        server.save_screenshot(str(tmp_path / "screen.png"))
    except RuntimeError as exc:
        assert "device offline" in str(exc)
    else:
        raise AssertionError("ADB 失败不能伪装成 FLAG_SECURE")


def test_ui_step_swipe_uses_current_viewport(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_wm_size", lambda: (1200, 2670))
    monkeypatch.setattr(server, "swipe", lambda *args: calls.append(args) or {"ok": True})
    monkeypatch.setattr(server, "get_focused_app", lambda: "pkg/Activity")

    result = server.run_ui_steps(
        [{"action": "swipe_up"}, {"action": "swipe_down"}],
        settle_ms=0,
    )

    assert result["ok"] is True
    assert calls == [
        (600, 2056, 600, 694, 400),
        (600, 694, 600, 2056, 400),
    ]
