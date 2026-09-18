import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from run_store import DeviceLeaseManager, RunCoordinator, RunStore  # noqa: E402


def _device(serial="phone-a"):
    return {
        "status": "ready",
        "selected_serial": serial,
        "detail": {"manufacturer": "Xiaomi", "model": "Xiaomi 14", "android": "15",
                   "width": 1200, "height": 2670},
    }


def test_run_store_creates_manifest_attempt_and_durable_events(tmp_path):
    store = RunStore(str(tmp_path))
    record = store.create_run(_device(), {"filename": "cases.xlsx", "sheet": "S1", "model": "Kimi K3"})

    assert os.path.isdir(record.attempt_dir)
    manifest = json.load(open(os.path.join(record.run_dir, "manifest.json"), encoding="utf-8"))
    assert manifest["run_id"] == record.run_id
    assert manifest["status"] == "running"
    assert manifest["device"]["model"] == "Xiaomi 14"
    assert manifest["environment"]["build_version"]
    assert "api_key" not in json.dumps(manifest["environment"]).lower()
    assert "phone-a" not in json.dumps(manifest)

    first = store.append_event(record, {"type": "suite_start", "api_key": "must-redact"})
    second = store.append_event(record, {"type": "case_start", "seq": 1})

    assert first["event_seq"] == 1
    assert first["event_id"]
    assert second["event_seq"] == 2
    lines = [json.loads(line) for line in open(
        os.path.join(record.run_dir, "events.jsonl"), encoding="utf-8"
    )]
    assert lines[0]["api_key"] == "[REDACTED]"
    assert lines[1]["run_id"] == record.run_id

    assert store.read_events(record, after_seq=1) == [lines[1]]


def test_read_events_skips_incomplete_tail_and_honors_cursor(tmp_path):
    store = RunStore(str(tmp_path))
    record = store.create_run(_device(), {"source": "interactive"})
    store.append_event(record, {"type": "user_message", "text": "第一轮"})
    second = store.append_event(record, {"type": "chat_delta", "text": "收到"})
    with open(os.path.join(record.run_dir, "events.jsonl"), "a", encoding="utf-8") as handle:
        handle.write('{"broken":')

    assert store.read_events(record, 1) == [second]


def test_finish_updates_manifest_without_overwriting_attempt(tmp_path):
    store = RunStore(str(tmp_path))
    record = store.create_run(_device(), {"sheet": "S1"})

    store.finish(record, "completed", {"pass": 2})

    manifest = json.load(open(os.path.join(record.run_dir, "manifest.json"), encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert manifest["attempts"][0]["status"] == "completed"
    assert manifest["attempts"][0]["summary"] == {"pass": 2}


def test_startup_recovery_marks_incomplete_run_interrupted(tmp_path):
    store = RunStore(str(tmp_path))
    record = store.create_run(_device(), {"sheet": "S1"})

    RunStore(str(tmp_path)).recover_incomplete()

    manifest = json.load(open(os.path.join(record.run_dir, "manifest.json"), encoding="utf-8"))
    assert manifest["status"] == "interrupted"
    assert manifest["attempts"][0]["status"] == "interrupted"


def test_device_lease_rejects_second_run_until_release():
    leases = DeviceLeaseManager()
    first = leases.acquire("phone-a", "run-1")

    try:
        leases.acquire("phone-a", "run-2")
    except RuntimeError as exc:
        assert "占用" in str(exc)
    else:
        raise AssertionError("同一设备不应同时获得两个 active lease")

    leases.release(first.token)
    second = leases.acquire("phone-a", "run-2")
    assert second.run_id == "run-2"


def test_coordinator_persists_before_broadcast_and_releases_lease(tmp_path):
    delivered = []

    async def emit(event):
        delivered.append(event)

    coordinator = RunCoordinator(str(tmp_path), emit, lambda: _device())
    handle = coordinator.begin({"sheet": "S1"})
    asyncio.run(handle.emit({"type": "case_start", "seq": 1}))
    handle.finish("completed", {"pass": 1})

    assert delivered[0]["event_seq"] == 1
    assert coordinator.leases.active("phone-a") is None


def test_coordinator_refuses_run_without_ready_device(tmp_path):
    coordinator = RunCoordinator(
        str(tmp_path), lambda event: None,
        lambda: {"status": "unauthorized", "selected_serial": None},
    )

    try:
        coordinator.begin({"sheet": "S1"})
    except RuntimeError as exc:
        assert "未就绪" in str(exc)
    else:
        raise AssertionError("设备未就绪时不应创建 Run")


def test_child_run_borrows_parent_lease_and_keeps_separate_manifest(tmp_path):
    coordinator = RunCoordinator(str(tmp_path), lambda event: None, lambda: _device())
    parent = coordinator.begin({"source": "interactive"})

    child = coordinator.begin_child(parent.run_id, {"source": "test_excel", "sheet": "S1"})
    manifest = json.load(open(os.path.join(child.run_dir, "manifest.json"), encoding="utf-8"))

    assert manifest["metadata"]["parent_run_id"] == parent.run_id
    assert child.run_id != parent.run_id
    child.finish("completed")
    assert coordinator.leases.active("phone-a") is not None
    parent.finish("completed")
    assert coordinator.leases.active("phone-a") is None


def test_parent_close_converges_only_running_children(tmp_path):
    coordinator = RunCoordinator(str(tmp_path), lambda event: None, lambda: _device())
    parent = coordinator.begin({"source": "interactive"})
    running = coordinator.begin_child(parent.run_id, {"source": "test_excel", "sheet": "S1"})
    completed = coordinator.begin_child(parent.run_id, {"source": "test_excel", "sheet": "S2"})
    completed.finish("completed")

    count = coordinator.store.finish_running_children(
        parent.run_id, "interrupted", {"reason": "parent_session_closed"}
    )

    assert count == 1
    running_manifest = json.load(open(os.path.join(running.run_dir, "manifest.json"), encoding="utf-8"))
    completed_manifest = json.load(open(os.path.join(completed.run_dir, "manifest.json"), encoding="utf-8"))
    assert running_manifest["status"] == "interrupted"
    assert running_manifest["attempts"][0]["summary"]["reason"] == "parent_session_closed"
    assert completed_manifest["status"] == "completed"
    assert coordinator.store.finish_running_children(parent.run_id) == 0
    parent.finish("interrupted")


def test_child_run_requires_parent_to_own_device_lease(tmp_path):
    coordinator = RunCoordinator(str(tmp_path), lambda event: None, lambda: _device())
    parent = coordinator.begin({"source": "interactive"})
    parent.finish("completed")

    try:
        coordinator.begin_child(parent.run_id, {"source": "test_excel"})
    except RuntimeError as exc:
        assert "父 Run" in str(exc)
    else:
        raise AssertionError("已释放租约的父 Run 不能再派生套件 Run")


def test_spawned_coordinator_can_use_explicit_borrowed_parent_lease(tmp_path):
    parent_coordinator = RunCoordinator(str(tmp_path), lambda event: None, lambda: _device())
    parent = parent_coordinator.begin({"source": "interactive"})
    capability = {
        "token": parent.lease.token,
        "serial": parent.lease.serial,
        "run_id": parent.lease.run_id,
        "acquired_at": parent.lease.acquired_at,
    }
    # 模拟 spawn 子进程：新的 coordinator 没有主进程内存中的 lease manager。
    child_coordinator = RunCoordinator(
        str(tmp_path), lambda event: None, lambda: _device(),
        recover=False, borrowed_lease=capability,
    )

    child = child_coordinator.begin_child(parent.run_id, {"source": "test_excel"})

    assert child.lease.run_id == parent.run_id
    child.finish("completed")
    assert parent_coordinator.leases.active("phone-a") is not None
    parent.finish("completed")


def test_borrowed_parent_lease_rejects_wrong_device_or_run(tmp_path):
    coordinator = RunCoordinator(str(tmp_path), lambda event: None, lambda: _device())
    parent = coordinator.begin({"source": "interactive"})
    wrong = {
        "token": parent.lease.token, "serial": "phone-b",
        "run_id": parent.run_id, "acquired_at": parent.lease.acquired_at,
    }
    child_coordinator = RunCoordinator(
        str(tmp_path), lambda event: None, lambda: _device(),
        recover=False, borrowed_lease=wrong,
    )

    try:
        child_coordinator.begin_child(parent.run_id, {"source": "test_excel"})
    except RuntimeError as exc:
        assert "不匹配" in str(exc)
    else:
        raise AssertionError("跨进程借用凭证必须绑定同一设备和父 Run")
    parent.finish("completed")
