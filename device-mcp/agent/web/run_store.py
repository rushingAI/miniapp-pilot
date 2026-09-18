"""Companion 本地 Run 事实源：manifest、attempt、事件日志与设备租约。"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

try:
    from telemetry import environment_fingerprint
except ModuleNotFoundError:  # pytest/包导入可能只把 device-mcp 根放进 sys.path
    from agent.telemetry import environment_fingerprint


SCHEMA_VERSION = 1
_SECRET_FIELDS = {"api_key", "authorization", "token", "anthropic_api_key", "anthropic_auth_token"}


def _utc_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _redact(value):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if str(key).lower() in _SECRET_FIELDS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _atomic_json(path: str, payload: dict) -> None:
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    attempt_id: str
    run_dir: str
    attempt_dir: str


@dataclass(frozen=True)
class DeviceLease:
    token: str
    serial: str
    run_id: str
    acquired_at: str


class DeviceLeaseManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._by_serial: dict[str, DeviceLease] = {}
        self._by_token: dict[str, DeviceLease] = {}

    def acquire(self, serial: str, run_id: str) -> DeviceLease:
        with self._lock:
            if serial in self._by_serial:
                raise RuntimeError("设备正被另一个 Run 占用")
            lease = DeviceLease(uuid.uuid4().hex, serial, run_id, _utc_ts())
            self._by_serial[serial] = lease
            self._by_token[lease.token] = lease
            return lease

    def release(self, token: str) -> None:
        with self._lock:
            lease = self._by_token.pop(token, None)
            if lease and self._by_serial.get(lease.serial) == lease:
                self._by_serial.pop(lease.serial, None)

    def active(self, serial: str) -> DeviceLease | None:
        with self._lock:
            return self._by_serial.get(serial)


class RunStore:
    def __init__(self, root: str):
        self.root = os.path.realpath(root)
        os.makedirs(self.root, exist_ok=True)
        self._lock = threading.Lock()
        self._event_seq: dict[str, int] = {}

    def _manifest_path(self, record: RunRecord) -> str:
        return os.path.join(record.run_dir, "manifest.json")

    def _load_manifest(self, record: RunRecord) -> dict:
        with open(self._manifest_path(record), encoding="utf-8") as handle:
            return json.load(handle)

    def create_run(self, device: dict, metadata: dict, environment: dict | None = None) -> RunRecord:
        serial = str(device.get("selected_serial") or "")
        if device.get("status") != "ready" or not serial:
            raise RuntimeError("设备未就绪，不能开始 Run")
        run_id = str(uuid.uuid4())
        attempt_id = str(uuid.uuid4())
        run_dir = os.path.join(self.root, run_id)
        attempt_dir = os.path.join(run_dir, "attempts", attempt_id)
        os.makedirs(attempt_dir, exist_ok=False)
        now = _utc_ts()
        detail = device.get("detail") or {}
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "status": "running",
            "created_at": now,
            "updated_at": now,
            "device": {
                "serial_hash": hashlib.sha256(serial.encode("utf-8")).hexdigest(),
                "manufacturer": detail.get("manufacturer", ""),
                "model": detail.get("model", ""),
                "device": detail.get("device", ""),
                "android": detail.get("android", ""),
                "sdk": detail.get("sdk", ""),
                "width": detail.get("width", 0),
                "height": detail.get("height", 0),
            },
            "metadata": _redact(metadata),
            "environment": _redact(environment or environment_fingerprint()),
            "attempts": [{
                "attempt_id": attempt_id,
                "status": "running",
                "created_at": now,
                "updated_at": now,
            }],
        }
        _atomic_json(os.path.join(run_dir, "manifest.json"), manifest)
        with open(os.path.join(run_dir, "events.jsonl"), "a", encoding="utf-8"):
            pass
        record = RunRecord(run_id, attempt_id, run_dir, attempt_dir)
        self._event_seq[run_id] = 0
        return record

    def append_event(self, record: RunRecord, event: dict) -> dict:
        with self._lock:
            sequence = self._event_seq.get(record.run_id)
            if sequence is None:
                sequence = 0
                path = os.path.join(record.run_dir, "events.jsonl")
                if os.path.isfile(path):
                    with open(path, encoding="utf-8") as handle:
                        sequence = sum(1 for line in handle if line.strip())
            sequence += 1
            self._event_seq[record.run_id] = sequence
            enriched = _redact({
                **event,
                "event_id": str(uuid.uuid4()),
                "run_id": record.run_id,
                "attempt_id": record.attempt_id,
                "event_seq": sequence,
                "ts": _utc_ts(),
            })
            with open(os.path.join(record.run_dir, "events.jsonl"), "a", encoding="utf-8") as handle:
                handle.write(json.dumps(enriched, ensure_ascii=False, default=str) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return enriched

    def read_events(self, record: RunRecord, after_seq: int = 0) -> list[dict]:
        """按序读取一个 Run 的 durable 事件；坏尾行不影响此前已落盘事件恢复。"""
        events: list[dict] = []
        path = os.path.join(record.run_dir, "events.jsonl")
        if not os.path.isfile(path):
            return events
        try:
            cursor = max(0, int(after_seq or 0))
        except (TypeError, ValueError):
            cursor = 0
        with self._lock:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if int(event.get("event_seq") or 0) > cursor:
                        events.append(event)
        return events

    def finish(self, record: RunRecord, status: str, summary: dict | None = None) -> None:
        with self._lock:
            manifest = self._load_manifest(record)
            now = _utc_ts()
            manifest["status"] = status
            manifest["updated_at"] = now
            for attempt in manifest.get("attempts", []):
                if attempt.get("attempt_id") == record.attempt_id:
                    attempt["status"] = status
                    attempt["updated_at"] = now
                    if summary is not None:
                        attempt["summary"] = _redact(summary)
                    break
            _atomic_json(self._manifest_path(record), manifest)

    def recover_incomplete(self) -> int:
        recovered = 0
        for name in os.listdir(self.root):
            path = os.path.join(self.root, name, "manifest.json")
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as handle:
                    manifest = json.load(handle)
                if manifest.get("status") != "running":
                    continue
                now = _utc_ts()
                manifest["status"] = "interrupted"
                manifest["updated_at"] = now
                for attempt in manifest.get("attempts", []):
                    if attempt.get("status") == "running":
                        attempt["status"] = "interrupted"
                        attempt["updated_at"] = now
                _atomic_json(path, manifest)
                recovered += 1
            except (OSError, ValueError, TypeError):
                continue
        return recovered

    def finish_running_children(self, parent_run_id: str, status: str = "interrupted",
                                summary: dict | None = None) -> int:
        """Idempotently converge running nested Runs when their parent is closing."""
        finished = 0
        with self._lock:
            for name in os.listdir(self.root):
                path = os.path.join(self.root, name, "manifest.json")
                if not os.path.isfile(path):
                    continue
                try:
                    with open(path, encoding="utf-8") as handle:
                        manifest = json.load(handle)
                except (OSError, ValueError, TypeError):
                    continue
                metadata = manifest.get("metadata") or {}
                if (metadata.get("parent_run_id") != parent_run_id
                        or manifest.get("status") != "running"):
                    continue
                now = _utc_ts()
                manifest["status"] = status
                manifest["updated_at"] = now
                for attempt in manifest.get("attempts", []):
                    if attempt.get("status") == "running":
                        attempt["status"] = status
                        attempt["updated_at"] = now
                        if summary is not None:
                            attempt["summary"] = _redact(summary)
                _atomic_json(path, manifest)
                finished += 1
        return finished

    def run_dir(self, run_id: str) -> str | None:
        try:
            canonical = str(uuid.UUID(run_id))
        except (ValueError, TypeError, AttributeError):
            return None
        path = os.path.realpath(os.path.join(self.root, canonical))
        return path if path.startswith(self.root + os.sep) and os.path.isdir(path) else None


class RunHandle:
    def __init__(self, coordinator: "RunCoordinator", record: RunRecord,
                 lease: DeviceLease | None, owns_lease: bool = True):
        self.coordinator = coordinator
        self.record = record
        self.lease = lease
        self.run_id = record.run_id
        self.attempt_id = record.attempt_id
        self.run_dir = record.run_dir
        self.attempt_dir = record.attempt_dir
        self._finished = False
        self._owns_lease = owns_lease

    async def emit(self, event: dict):
        enriched = self.coordinator.store.append_event(self.record, event)
        result = self.coordinator.downstream(enriched)
        if inspect.isawaitable(result):
            await result
        return enriched

    def finish(self, status: str, summary: dict | None = None) -> None:
        if self._finished:
            return
        try:
            self.coordinator.store.finish(self.record, status, summary)
        finally:
            if self._owns_lease and self.lease is not None:
                self.coordinator.leases.release(self.lease.token)
            self._finished = True


class RunCoordinator:
    def __init__(self, root: str, downstream: Callable, device_snapshot: Callable[[], dict],
                 recover: bool = True, borrowed_lease: DeviceLease | dict | None = None):
        self.store = RunStore(root)
        self.leases = DeviceLeaseManager()
        self.downstream = downstream
        self.device_snapshot = device_snapshot
        if isinstance(borrowed_lease, dict):
            borrowed_lease = DeviceLease(**{
                key: str(borrowed_lease.get(key) or "")
                for key in ("token", "serial", "run_id", "acquired_at")
            })
        self.borrowed_lease = borrowed_lease
        if recover:
            self.store.recover_incomplete()

    def begin(self, metadata: dict, environment: dict | None = None) -> RunHandle:
        device = self.device_snapshot()
        serial = str(device.get("selected_serial") or "")
        if device.get("status") != "ready" or not serial:
            raise RuntimeError("设备未就绪，不能开始 Run")
        record = self.store.create_run(device, metadata, environment)
        try:
            lease = self.leases.acquire(serial, record.run_id)
        except Exception:
            self.store.finish(record, "rejected")
            raise
        return RunHandle(self, record, lease)

    def begin_child(self, parent_run_id: str, metadata: dict,
                    environment: dict | None = None) -> RunHandle:
        """Create a durable nested Run while borrowing the parent's physical-device lease."""
        parent_dir = self.store.run_dir(parent_run_id)
        if not parent_dir:
            raise RuntimeError("父 Run 不存在，不能创建嵌套套件 Run")
        device = self.device_snapshot()
        serial = str(device.get("selected_serial") or "")
        if device.get("status") != "ready" or not serial:
            raise RuntimeError("设备未就绪，不能开始嵌套 Run")
        parent_lease = self.leases.active(serial)
        # Interactive executor 使用 spawn 子进程。父 Run 的 DeviceLeaseManager 是主进程内存，
        # 子进程不可能查询到；由主进程在 worker config 中显式下发不可落盘的借用凭证。
        if parent_lease is None:
            parent_lease = self.borrowed_lease
        if parent_lease is None or parent_lease.run_id != parent_run_id:
            raise RuntimeError("父 Run 未持有当前设备租约，不能创建嵌套 Run")
        if parent_lease.serial != serial or not parent_lease.token:
            raise RuntimeError("父 Run 设备租约与当前设备不匹配")
        record = self.store.create_run(device, {**metadata, "parent_run_id": parent_run_id}, environment)
        return RunHandle(self, record, parent_lease, owns_lease=False)
