"""Windows Companion 的 ADB 设备发现与首版单设备选择。

本模块不依赖 FastAPI，解析和选择逻辑保持纯函数，便于在无真机环境中回归。
"""

from __future__ import annotations

import re
import subprocess
import threading


_SIZE_RE = re.compile(r"(\d+)x(\d+)")
_ADB_DEVICES_HEADER = "List of devices attached"
_ADB_DEVICES_ATTEMPTS = 3


def parse_adb_devices(output: str) -> list[dict[str, str]]:
    """解析 ``adb devices -l``，保留 serial、状态和 ``key:value`` 元数据。"""
    devices: list[dict[str, str]] = []
    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("List of devices attached") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        item = {"serial": parts[0], "state": parts[1]}
        for token in parts[2:]:
            if ":" in token:
                key, value = token.split(":", 1)
                if key and value:
                    item[key] = value
        devices.append(item)
    return devices


def choose_device(devices: list[dict[str, str]], preferred_serial: str = "") -> dict:
    """选择首版唯一设备；多台 ready 时强制人工选择，不偷偷取第一台。"""
    preferred = (preferred_serial or "").strip()
    if preferred:
        match = next((d for d in devices if d.get("serial") == preferred), None)
        if match is None:
            return {"status": "disconnected", "selected_serial": None, "devices": devices}
        state = match.get("state") or "unknown"
        return {
            "status": "ready" if state == "device" else state,
            "selected_serial": preferred,
            "devices": devices,
        }

    ready = [d for d in devices if d.get("state") == "device"]
    if len(ready) == 1:
        return {"status": "ready", "selected_serial": ready[0]["serial"], "devices": devices}
    if len(ready) > 1:
        return {"status": "multiple_devices", "selected_serial": None, "devices": devices}
    if any(d.get("state") == "unauthorized" for d in devices):
        return {"status": "unauthorized", "selected_serial": None, "devices": devices}
    if any(d.get("state") == "offline" for d in devices):
        return {"status": "offline", "selected_serial": None, "devices": devices}
    return {"status": "no_device", "selected_serial": None, "devices": devices}


def parse_wm_size(output: str) -> tuple[int, int]:
    """解析 ``wm size``；有 Override 时必须使用当前逻辑尺寸。"""
    values: dict[str, tuple[int, int]] = {}
    fallback: tuple[int, int] = (0, 0)
    for raw in (output or "").splitlines():
        match = _SIZE_RE.search(raw)
        if not match:
            continue
        size = (int(match.group(1)), int(match.group(2)))
        fallback = size
        label = raw.split(":", 1)[0].strip().lower()
        if "override" in label:
            values["override"] = size
        elif "physical" in label:
            values["physical"] = size
    return values.get("override") or values.get("physical") or fallback


class DeviceSupervisor:
    """轮询 ADB 并维护一个可序列化的设备快照。"""

    def __init__(self, adb: str = "adb", preferred_serial: str = ""):
        self.adb = adb
        self.preferred_serial = (preferred_serial or "").strip()
        self._lock = threading.Lock()
        self._snapshot: dict = {
            "status": "checking",
            "selected_serial": None,
            "devices": [],
            "detail": {},
            "error": "",
            "adb_returncode": None,
            "adb_error_type": "",
        }

    def snapshot(self) -> dict:
        with self._lock:
            snap = dict(self._snapshot)
            snap["devices"] = [dict(d) for d in self._snapshot.get("devices", [])]
            snap["detail"] = dict(self._snapshot.get("detail", {}))
            return snap

    def select(self, serial: str) -> dict:
        serial = (serial or "").strip()
        current = self.snapshot()
        if not any(d.get("serial") == serial and d.get("state") == "device"
                   for d in current.get("devices", [])):
            raise ValueError("只能选择当前已就绪的设备")
        self.preferred_serial = serial
        return self.refresh()

    def _run(self, args: list[str], timeout: int = 6) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.adb, *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )

    def _query_devices(self) -> tuple[subprocess.CompletedProcess, bool, int]:
        """Retry successful-but-malformed ADB replies before treating them as state."""
        result = None
        malformed_attempts = 0
        for _ in range(_ADB_DEVICES_ATTEMPTS):
            result = self._run(["devices", "-l"])
            if result.returncode != 0:
                return result, False, malformed_attempts
            if any(
                line.strip() == _ADB_DEVICES_HEADER
                for line in (result.stdout or "").splitlines()
            ):
                return result, True, malformed_attempts
            malformed_attempts += 1
        assert result is not None
        return result, False, malformed_attempts

    def _prop(self, serial: str, name: str) -> str:
        result = self._run(["-s", serial, "shell", "getprop", name])
        return (result.stdout or "").strip() if result.returncode == 0 else ""

    def _detail(self, serial: str) -> dict:
        wm = self._run(["-s", serial, "shell", "wm", "size"])
        width, height = parse_wm_size(wm.stdout if wm.returncode == 0 else "")
        return {
            "manufacturer": self._prop(serial, "ro.product.manufacturer"),
            "model": self._prop(serial, "ro.product.model"),
            "device": self._prop(serial, "ro.product.device"),
            "android": self._prop(serial, "ro.build.version.release"),
            "sdk": self._prop(serial, "ro.build.version.sdk"),
            "width": width,
            "height": height,
        }

    def refresh(self, preserve_missing_selection: bool = False) -> dict:
        previous = self.snapshot()
        try:
            result, valid_output, malformed_attempts = self._query_devices()
            if result.returncode != 0:
                snap = {
                    "status": "adb_error",
                    "selected_serial": None,
                    "devices": [],
                    "detail": {},
                    "error": (result.stderr or result.stdout or "ADB 执行失败").strip()[:300],
                    "adb_returncode": int(result.returncode),
                    "adb_error_type": "command_failed",
                }
            elif not valid_output:
                snap = {
                    **previous,
                    "error": "ADB 设备查询返回无效响应，请稍后重试",
                    "adb_returncode": 0,
                    "adb_error_type": "malformed_output",
                }
            else:
                devices = parse_adb_devices(result.stdout)
                preferred_ready = any(
                    d.get("serial") == self.preferred_serial and d.get("state") == "device"
                    for d in devices
                )
                if self.preferred_serial and not preferred_ready and not preserve_missing_selection:
                    # 没有 active Run 时不跨 Run 粘住已拔掉的旧机。
                    self.preferred_serial = ""
                selected = choose_device(devices, self.preferred_serial)
                serial = selected.get("selected_serial")
                if selected["status"] == "ready" and serial and not self.preferred_serial:
                    # 记住当前选择；active Run 期间由调用方要求保留，空闲时可重选。
                    self.preferred_serial = serial
                same_ready_device = (
                    selected["status"] == "ready"
                    and previous.get("status") == "ready"
                    and previous.get("selected_serial") == serial
                )
                detail = {}
                if same_ready_device:
                    detail = previous.get("detail", {})
                elif selected["status"] == "ready" and serial:
                    detail = self._detail(serial)
                snap = {
                    **selected,
                    "detail": detail,
                    "error": "",
                    "adb_returncode": 0,
                    "adb_error_type": (
                        "malformed_output_recovered" if malformed_attempts else ""
                    ),
                }
        except FileNotFoundError:
            snap = {
                "status": "adb_missing", "selected_serial": None, "devices": [],
                "detail": {}, "error": "未找到 ADB，请重新安装或修复 Companion",
                "adb_returncode": None, "adb_error_type": "missing",
            }
        except subprocess.TimeoutExpired:
            snap = {
                "status": "adb_error", "selected_serial": None, "devices": [],
                "detail": {}, "error": "ADB 响应超时",
                "adb_returncode": None, "adb_error_type": "timeout",
            }
        with self._lock:
            self._snapshot = snap
        return self.snapshot()
