"""脱敏性能观测：阶段时间线、Result 汇总与 Run 环境指纹。"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from urllib.parse import urlparse

try:
    from .build_version import APP_VERSION
except ImportError:  # Packaged entry points may import agent modules as top-level modules.
    from build_version import APP_VERSION


BUILD_VERSION = APP_VERSION
_KEY_FIELDS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
_PROXY_FIELDS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")
_SAFE_METRIC_LABEL = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def utc_ts_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def provider_key_fingerprint(provider_env: dict | None) -> str:
    env = provider_env or {}
    value = next((str(env.get(name) or "") for name in _KEY_FIELDS if env.get(name)), "")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8] if value else ""


def environment_fingerprint(provider_env: dict | None = None,
                            key_fingerprint: str = "") -> dict:
    """只返回可安全落盘的运行环境事实；不返回 Key、代理地址或完整 URL。"""
    env = provider_env or {}
    base_url = str(env.get("ANTHROPIC_BASE_URL") or "")
    hostname = (urlparse(base_url).hostname or "").lower()
    return {
        "build_version": BUILD_VERSION,
        "python_version": platform.python_version(),
        "claude_agent_sdk_version": _package_version("claude-agent-sdk"),
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "api_hostname": hostname,
        "proxy_present": any(bool(os.environ.get(name)) for name in _PROXY_FIELDS),
        "key_fingerprint": key_fingerprint or provider_key_fingerprint(provider_env),
    }


def _windows_process_snapshot(pid: int) -> dict:
    """Best-effort cumulative CPU and working-set facts for one Windows process."""
    try:
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("page_fault_count", wintypes.DWORD),
                ("peak_working_set_size", ctypes.c_size_t),
                ("working_set_size", ctypes.c_size_t),
                ("quota_peak_paged_pool_usage", ctypes.c_size_t),
                ("quota_paged_pool_usage", ctypes.c_size_t),
                ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
                ("quota_non_paged_pool_usage", ctypes.c_size_t),
                ("pagefile_usage", ctypes.c_size_t),
                ("peak_pagefile_usage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

        # Same-user SDK/CLI processes normally permit this narrow read-only access.
        handle = kernel32.OpenProcess(0x1000 | 0x0010, False, int(pid))
        if not handle:
            return {}
        try:
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            metrics = {}
            if kernel32.GetProcessTimes(
                    handle, ctypes.byref(created), ctypes.byref(exited),
                    ctypes.byref(kernel), ctypes.byref(user)):
                def milliseconds(value):
                    ticks = (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)
                    return round(ticks / 10_000, 3)

                metrics["process_cpu_ms"] = round(
                    milliseconds(kernel) + milliseconds(user), 3,
                )
            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(
                    handle, ctypes.byref(counters), counters.cb):
                metrics["rss_mb"] = round(
                    int(counters.working_set_size) / (1024 * 1024), 3,
                )
            return metrics
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return {}


def _windows_system_snapshot() -> dict:
    """Cumulative CPU clocks and current memory pressure; deltas are derived offline."""
    try:
        import ctypes
        from ctypes import wintypes

        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        idle = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        metrics = {"system_cpu_count": int(os.cpu_count() or 0)}
        if kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            def milliseconds(value):
                ticks = (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)
                return round(ticks / 10_000, 3)

            metrics.update({
                "system_idle_ms": milliseconds(idle),
                "system_kernel_ms": milliseconds(kernel),
                "system_user_ms": milliseconds(user),
            })
        memory = MemoryStatusEx()
        memory.dwLength = ctypes.sizeof(memory)
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            metrics.update({
                "system_memory_load_pct": int(memory.dwMemoryLoad),
                "system_available_memory_mb": round(
                    int(memory.ullAvailPhys) / (1024 * 1024), 3,
                ),
            })
        return metrics
    except Exception:
        return {}


def runtime_resource_snapshot(processes: dict[str, int] | None = None) -> dict:
    """Small redacted runtime snapshot for timing boundaries, never a control signal."""
    snapshot = {
        "observer_cpu_ms": round(time.process_time_ns() / 1_000_000, 3),
        "system_cpu_count": int(os.cpu_count() or 0),
    }
    if platform.system() == "Windows":
        snapshot.update(_windows_system_snapshot())
        requested = {"observer": os.getpid(), **dict(processes or {})}
        for label, raw_pid in requested.items():
            if not _SAFE_METRIC_LABEL.fullmatch(str(label or "")):
                continue
            try:
                pid = int(raw_pid)
            except (TypeError, ValueError):
                continue
            if pid <= 0:
                continue
            for key, value in _windows_process_snapshot(pid).items():
                snapshot[f"{label}_{key}"] = value
    else:
        try:
            import resource
            maximum = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            divisor = 1024 * 1024 if platform.system() == "Darwin" else 1024
            snapshot["observer_max_rss_mb"] = round(maximum / divisor, 3)
            load_1m, load_5m, load_15m = os.getloadavg()
            snapshot.update({
                "system_load_1m": round(load_1m, 3),
                "system_load_5m": round(load_5m, 3),
                "system_load_15m": round(load_15m, 3),
            })
        except (AttributeError, OSError, ImportError):
            pass
    return snapshot


def append_jsonl(path: str, record: dict) -> None:
    """一次追加一条小记录；调用方只传脱敏字段。写失败不得影响真实执行。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


class PhaseTimer:
    """按同一 monotonic 原点记录阶段；可跨 Windows spawn 传递 perf_counter_ns 原点。"""

    def __init__(self, path: str, identifiers: dict | None = None,
                 origin_ns: int | None = None, origin_utc: str = ""):
        self.path = path
        self.identifiers = dict(identifiers or {})
        self.origin_ns = int(origin_ns or time.perf_counter_ns())
        self.origin_utc = origin_utc or utc_ts_ms()

    def mark(self, phase: str, **fields) -> dict:
        return self.mark_at(phase, time.perf_counter_ns(), utc_ts_ms(), **fields)

    def mark_at(self, phase: str, at_ns: int, at_utc: str = "", **fields) -> dict:
        record = {
            "type": "phase_timing",
            **self.identifiers,
            "phase": str(phase),
            "ts": at_utc or utc_ts_ms(),
            "origin_ts": self.origin_utc,
            "elapsed_ms": round((int(at_ns) - self.origin_ns) / 1_000_000, 3),
            **fields,
        }
        append_jsonl(self.path, record)
        return record
