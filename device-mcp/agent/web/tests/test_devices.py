import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from devices import DeviceSupervisor, choose_device, parse_adb_devices, parse_wm_size  # noqa: E402


class _ScriptedSupervisor(DeviceSupervisor):
    def __init__(self, outputs, preferred_serial="phone-a"):
        super().__init__(preferred_serial=preferred_serial)
        self.outputs = iter(outputs)
        self.calls = 0

    def _run(self, args, timeout=6):
        self.calls += 1
        return subprocess.CompletedProcess(
            args, 0, stdout=next(self.outputs), stderr="",
        )

    def _detail(self, serial):
        return {"model": serial}


def test_parse_adb_devices_keeps_state_and_metadata():
    output = """List of devices attached
R58M123456\tdevice product:r0qxxx model:SM_S9010 device:r0q transport_id:1
4a1912\tunauthorized usb:1-2 transport_id:2
emulator-5554\toffline transport_id:3
"""

    devices = parse_adb_devices(output)

    assert devices == [
        {
            "serial": "R58M123456",
            "state": "device",
            "product": "r0qxxx",
            "model": "SM_S9010",
            "device": "r0q",
            "transport_id": "1",
        },
        {
            "serial": "4a1912",
            "state": "unauthorized",
            "usb": "1-2",
            "transport_id": "2",
        },
        {
            "serial": "emulator-5554",
            "state": "offline",
            "transport_id": "3",
        },
    ]


def test_choose_single_ready_device_without_hardcoded_serial():
    devices = parse_adb_devices(
        "List of devices attached\nredmi-1\tdevice model:Redmi_K30_Ultra transport_id:1\n"
    )

    state = choose_device(devices)

    assert state["status"] == "ready"
    assert state["selected_serial"] == "redmi-1"


def test_choose_requires_explicit_selection_when_multiple_are_ready():
    devices = parse_adb_devices(
        "List of devices attached\na\tdevice model:SM_S9010\nb\tdevice model:Xiaomi_14\n"
    )

    state = choose_device(devices)

    assert state["status"] == "multiple_devices"
    assert state["selected_serial"] is None


def test_choose_reports_unauthorized_and_offline():
    unauthorized = choose_device(parse_adb_devices(
        "List of devices attached\na\tunauthorized usb:1-1\n"
    ))
    offline = choose_device(parse_adb_devices(
        "List of devices attached\na\toffline transport_id:1\n"
    ))

    assert unauthorized["status"] == "unauthorized"
    assert offline["status"] == "offline"


def test_preferred_serial_must_exist_and_be_ready():
    devices = parse_adb_devices(
        "List of devices attached\na\tdevice model:SM_S9010\nb\tunauthorized\n"
    )

    missing = choose_device(devices, preferred_serial="missing")
    unauthorized = choose_device(devices, preferred_serial="b")

    assert missing["status"] == "disconnected"
    assert missing["selected_serial"] is None
    assert unauthorized["status"] == "unauthorized"
    assert unauthorized["selected_serial"] == "b"


def test_parse_wm_size_prefers_override_then_physical():
    assert parse_wm_size("Physical size: 1080x2340\nOverride size: 720x1560\n") == (720, 1560)
    assert parse_wm_size("Physical size: 1200x2670\n") == (1200, 2670)
    assert parse_wm_size("garbage") == (0, 0)


def test_supervisor_adb_uses_utf8_with_replacement(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    DeviceSupervisor()._run(["devices", "-l"])

    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"


def test_supervisor_does_not_repeat_static_getprops_every_poll():
    class FakeSupervisor(DeviceSupervisor):
        def __init__(self):
            super().__init__()
            self.detail_calls = 0

        def _run(self, args, timeout=6):
            return subprocess.CompletedProcess(
                args, 0,
                stdout="List of devices attached\nxiaomi\tdevice model:Xiaomi_14\n",
                stderr="",
            )

        def _detail(self, serial):
            self.detail_calls += 1
            return {"model": "Xiaomi 14", "width": 1200, "height": 2670}

    supervisor = FakeSupervisor()
    supervisor.refresh()
    supervisor.refresh()

    assert supervisor.detail_calls == 1


def test_idle_supervisor_reselects_the_only_new_ready_device():
    class SwitchingSupervisor(DeviceSupervisor):
        def __init__(self):
            super().__init__()
            self.outputs = iter([
                "List of devices attached\nphone-a\tdevice model:A\n",
                "List of devices attached\nphone-b\tdevice model:B\n",
            ])

        def _run(self, args, timeout=6):
            return subprocess.CompletedProcess(args, 0, stdout=next(self.outputs), stderr="")

        def _detail(self, serial):
            return {"model": serial}

    supervisor = SwitchingSupervisor()

    first = supervisor.refresh()
    second = supervisor.refresh()

    assert first["selected_serial"] == "phone-a"
    assert second["status"] == "ready"
    assert second["selected_serial"] == "phone-b"


def test_active_run_preserves_missing_selection_until_manual_switch():
    class SwitchingSupervisor(DeviceSupervisor):
        def __init__(self):
            super().__init__(preferred_serial="phone-a")

        def _run(self, args, timeout=6):
            return subprocess.CompletedProcess(
                args, 0,
                stdout="List of devices attached\nphone-b\tdevice model:B\n",
                stderr="",
            )

    state = SwitchingSupervisor().refresh(preserve_missing_selection=True)

    assert state["status"] == "disconnected"
    assert state["selected_serial"] is None
    assert state["devices"][0]["serial"] == "phone-b"
    assert state["adb_returncode"] == 0
    assert state["adb_error_type"] == ""


def test_supervisor_retries_empty_success_instead_of_publishing_disconnect():
    supervisor = _ScriptedSupervisor([
        "List of devices attached\nphone-a\tdevice model:A\n",
        "",
        "List of devices attached\nphone-a\tdevice model:A\n",
    ])

    assert supervisor.refresh(preserve_missing_selection=True)["status"] == "ready"
    recovered = supervisor.refresh(preserve_missing_selection=True)

    assert recovered["status"] == "ready"
    assert recovered["selected_serial"] == "phone-a"
    assert recovered["adb_error_type"] == "malformed_output_recovered"
    assert supervisor.calls == 3


def test_supervisor_preserves_last_valid_snapshot_after_repeated_malformed_output():
    supervisor = _ScriptedSupervisor([
        "List of devices attached\nphone-a\tdevice model:A\n",
        "", "", "",
    ])

    ready = supervisor.refresh(preserve_missing_selection=True)
    state = supervisor.refresh(preserve_missing_selection=True)

    assert ready["status"] == "ready"
    assert state["status"] == "ready"
    assert state["selected_serial"] == "phone-a"
    assert state["devices"] == ready["devices"]
    assert state["detail"] == ready["detail"]
    assert state["adb_returncode"] == 0
    assert state["adb_error_type"] == "malformed_output"
    assert supervisor.calls == 4


def test_supervisor_does_not_retry_valid_empty_device_list():
    supervisor = _ScriptedSupervisor(["List of devices attached\n\n"])

    state = supervisor.refresh(preserve_missing_selection=True)

    assert state["status"] == "disconnected"
    assert state["adb_returncode"] == 0
    assert state["adb_error_type"] == ""
    assert supervisor.calls == 1


def test_supervisor_records_adb_command_failure_class_and_returncode():
    class FailingSupervisor(DeviceSupervisor):
        def _run(self, args, timeout=6):
            return subprocess.CompletedProcess(
                args, 17, stdout="", stderr="USB backend secret detail",
            )

    state = FailingSupervisor().refresh()

    assert state["status"] == "adb_error"
    assert state["adb_returncode"] == 17
    assert state["adb_error_type"] == "command_failed"
