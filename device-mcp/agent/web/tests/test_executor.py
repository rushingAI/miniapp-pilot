import asyncio
import json
import os
import queue
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from executor import (  # noqa: E402
    InteractiveExecutorManager, _interactive_child,
)
from events import EventBus  # noqa: E402
from run_store import RunCoordinator  # noqa: E402


def test_interactive_executor_keeps_one_child_across_turns_and_persists_events(tmp_path):
    async def scenario():
        delivered = []

        async def downstream(event):
            delivered.append(event)

        device = {
            "status": "ready", "selected_serial": "phone-a",
            "detail": {"manufacturer": "Xiaomi", "model": "Xiaomi 14"},
        }
        coordinator = RunCoordinator(str(tmp_path), downstream, lambda: device)
        manager = InteractiveExecutorManager(coordinator)
        config = {"probe": True, "serial": "phone-a", "device": device}

        first = await manager.submit(config, "第一轮", client_message_id="m1")
        await first
        first_pid = manager.pid
        second = await manager.submit(config, "第二轮", client_message_id="m2")
        await second

        assert first_pid and manager.pid == first_pid
        assert [e["text"] for e in delivered if e["type"] == "user_message"] == ["第一轮", "第二轮"]
        assert [e["text"] for e in delivered if e["type"] == "chat_delta"] == [
            "echo:第一轮", "echo:第二轮",
        ]
        assert all(e.get("event_id") for e in delivered)
        assert manager.events_after(0) == delivered
        phase_path = os.path.join(
            manager.handle.attempt_dir, "trace", "interactive", "phase_timing.jsonl",
        )
        phases = [__import__("json").loads(line) for line in open(phase_path, encoding="utf-8")]
        assert {record["phase"] for record in phases} >= {
            "executor_received", "server_received", "queued", "first_sdk_message", "result_received",
        }
        await manager.close()

    asyncio.run(scenario())


def test_browser_replays_prompt_reply_and_steps_after_it_was_closed(tmp_path):
    class FakeSocket:
        def __init__(self):
            self.events = []

        async def send_json(self, event):
            self.events.append(event)

    async def scenario():
        bus = EventBus()
        device = {"status": "ready", "selected_serial": "phone-a", "detail": {"model": "S22"}}
        coordinator = RunCoordinator(str(tmp_path), bus.put, lambda: device)
        manager = InteractiveExecutorManager(coordinator)

        # 执行期间没有任何浏览器客户端；回复只能依靠 durable Run 保存。
        turn = await manager.submit(
            {"probe": True, "serial": "phone-a", "device": device},
            "关闭浏览器后继续", client_message_id="closed-browser-message",
        )
        await turn
        assert bus.client_count == 0

        reopened = FakeSocket()
        await bus.subscribe(reopened, lambda: manager.events_after(0))
        assert [event["type"] for event in reopened.events] == [
            "user_message", "chat_delta", "chat_done", "interactive_status",
        ]
        assert reopened.events[0]["client_message_id"] == "closed-browser-message"
        assert reopened.events[1]["text"] == "echo:关闭浏览器后继续"
        await manager.close()

    asyncio.run(scenario())


def test_running_input_is_durable_before_forwarding_to_active_owner(tmp_path):
    async def scenario():
        delivered = []
        device = {"status": "ready", "selected_serial": "phone-a", "detail": {}}
        coordinator = RunCoordinator(str(tmp_path), delivered.append, lambda: device)
        manager = InteractiveExecutorManager(coordinator)
        manager.handle = coordinator.begin({"source": "interactive"})
        manager.commands = queue.Queue()
        manager._turn_state = "running"
        manager._suite_running = True
        manager._current_turn_id = "turn-1"
        manager._active_suite = {
            "suite_id": "suite-1", "current_step": {"step_id": "case-2"},
        }

        accepted = await manager.append_input("当前行先检查登录态", "instruction-1")

        assert accepted is True
        assert delivered[-1]["type"] == "turn_input"
        assert delivered[-1]["status"] == "accepted"
        assert delivered[-1]["client_message_id"] == "instruction-1"
        assert manager.commands.get_nowait() == {
            "action": "append_input", "text": "当前行先检查登录态",
            "client_message_id": "instruction-1", "input_seq": 1,
            "turn_id": "turn-1", "suite_id": "suite-1", "step_id": "case-2",
        }
        manager.handle.finish("completed")
        manager.handle = None

    asyncio.run(scenario())


def test_finished_parent_turn_with_active_suite_is_idle_and_resumable(tmp_path):
    device = {"status": "ready", "selected_serial": "phone-a", "detail": {}}
    manager = InteractiveExecutorManager(
        RunCoordinator(str(tmp_path), lambda event: None, lambda: device),
    )
    manager._suite_running = True
    manager._active_suite = {
        "suite_id": "suite-1", "current_step": {"step_id": "case-3"},
    }
    manager._turn_state = "running"
    manager._parent_turn_active = False
    manager._current_turn_id = ""

    assert manager.busy is False
    assert manager.turn_state == "idle"


def test_interactive_worker_config_carries_parent_device_lease_capability(tmp_path):
    async def scenario():
        device = {"status": "ready", "selected_serial": "phone-a", "detail": {}}
        coordinator = RunCoordinator(str(tmp_path), lambda event: None, lambda: device)
        manager = InteractiveExecutorManager(coordinator)
        captured = {}

        async def fake_execute(config, command, future):
            captured.update(config)
            future.set_result(True)
            manager._turn_state = "idle"

        manager._execute_turn = fake_execute
        task = await manager.submit(
            {"probe": True, "serial": "phone-a", "device": device}, "执行 v1",
        )
        await task

        capability = captured["parent_device_lease"]
        assert capability["run_id"] == manager.handle.run_id
        assert capability["serial"] == "phone-a"
        assert capability["token"] == manager.handle.lease.token
        manager.handle.finish("completed")
        manager.handle = None

    asyncio.run(scenario())


def test_interactive_manager_only_accepts_response_while_human_is_waiting(tmp_path):
    async def scenario():
        device = {"status": "ready", "selected_serial": "phone-a", "detail": {}}
        manager = InteractiveExecutorManager(
            RunCoordinator(str(tmp_path), lambda event: None, lambda: device),
        )
        persisted = []

        async def persist(event):
            persisted.append(event)

        manager._persist = persist
        manager.commands = queue.Queue()
        manager._turn_state = "running"
        manager._current_turn_id = "turn-1"

        assert await manager.respond("过早回答") is False
        manager._human_waiting = True
        assert await manager.respond("继续") is True
        assert manager.commands.get_nowait() == {"action": "respond", "answer": "继续"}
        assert manager._human_waiting is False
        assert persisted == [{
            "type": "human_resolved", "reason": "answered", "turn_id": "turn-1",
        }]
        assert "继续" not in str(persisted)

    asyncio.run(scenario())


def test_idle_native_shutdown_does_not_interrupt_the_already_stopped_turn(monkeypatch, tmp_path):
    import harness_session
    import server
    import tools

    instances = []

    class FakeExcel:
        active_suite_id = "suite-still-active"

    class FakeHarnessSession:
        def __init__(self, *args, **kwargs):
            self.test_excel = FakeExcel()
            self.stop_calls = 0
            self.close_calls = 0
            instances.append(self)

        async def connect(self):
            return None

        async def stop(self):
            self.stop_calls += 1

        async def close(self):
            self.close_calls += 1

    monkeypatch.setattr(harness_session, "HarnessSession", FakeHarnessSession)
    monkeypatch.setattr(server, "configure_device", lambda _serial: None)
    monkeypatch.setattr(server, "clear_device", lambda: None)
    # _interactive_child normally exits with its process. Preserve its module globals
    # when exercising the worker inline in this test process.
    monkeypatch.setattr(tools, "CONTROL", tools.CONTROL)
    monkeypatch.setattr(tools, "RUN_CONTEXT", tools.RUN_CONTEXT)
    monkeypatch.setattr(tools, "TRACE_DIR", tools.TRACE_DIR)

    async def scenario():
        channel = queue.Queue()
        commands = queue.Queue()
        commands.put({"action": "shutdown"})
        attempt_dir = tmp_path / "attempt"
        attempt_dir.mkdir()

        await _interactive_child({
            "attempt_dir": str(attempt_dir),
            "uploads_dir": str(tmp_path / "uploads"),
            "runs_root": str(tmp_path / "runs"),
            "architecture": "native_harness",
            "serial": "phone-a",
            "device": {"status": "ready", "selected_serial": "phone-a"},
            "parent_run_id": "parent-run",
            "provider_env": {},
            "effort": "low",
        }, channel, commands)

        assert len(instances) == 1
        assert instances[0].stop_calls == 0
        assert instances[0].close_calls == 1

    asyncio.run(scenario())


def test_interactive_worker_rebuilds_device_session_after_reconnect(monkeypatch, tmp_path):
    import harness_session
    import server
    import tools

    calls = []

    class FakeExcel:
        active_suite_id = ""

    class FakeHarnessSession:
        def __init__(self, *args, **kwargs):
            self.test_excel = FakeExcel()

        async def connect(self):
            return None

        async def close(self):
            return None

    monkeypatch.setattr(harness_session, "HarnessSession", FakeHarnessSession)
    monkeypatch.setattr(
        server, "configure_device", lambda serial: calls.append(("configure", serial)),
    )
    monkeypatch.setattr(server, "clear_device", lambda: calls.append(("clear",)))
    monkeypatch.setattr(
        server, "invalidate_device_session",
        lambda serial: calls.append(("invalidate", serial)),
        raising=False,
    )
    monkeypatch.setattr(tools, "CONTROL", tools.CONTROL)
    monkeypatch.setattr(tools, "RUN_CONTEXT", tools.RUN_CONTEXT)
    monkeypatch.setattr(tools, "TRACE_DIR", tools.TRACE_DIR)

    async def scenario():
        channel = queue.Queue()
        commands = queue.Queue()
        commands.put({"action": "device_binding", "serial": "phone-a"})
        commands.put({"action": "device_binding", "serial": "phone-a"})
        commands.put({"action": "shutdown"})
        attempt_dir = tmp_path / "attempt"
        attempt_dir.mkdir()

        await _interactive_child({
            "attempt_dir": str(attempt_dir),
            "uploads_dir": str(tmp_path / "uploads"),
            "runs_root": str(tmp_path / "runs"),
            "architecture": "native_harness",
            "serial": "phone-a",
            "device": {"status": "ready", "selected_serial": "phone-a"},
            "parent_run_id": "parent-run",
            "provider_env": {},
            "effort": "low",
        }, channel, commands)

        assert calls == [
            ("configure", "phone-a"),
            ("invalidate", "phone-a"),
            ("invalidate", "phone-a"),
            ("clear",),
        ]

    asyncio.run(scenario())


def test_running_native_shutdown_still_interrupts_the_active_turn(monkeypatch, tmp_path):
    import harness_session
    import server
    import tools

    instances = []

    class FakeExcel:
        active_suite_id = "suite-active"

    class FakeHarnessSession:
        def __init__(self, *args, **kwargs):
            self.test_excel = FakeExcel()
            self.stop_calls = 0
            self.release_send = asyncio.Event()
            instances.append(self)

        async def connect(self):
            return None

        async def change_effort(self, _effort):
            return None

        async def prepare_turn(self, _mode):
            return ""

        async def send(self, _text, file=None, phase_timer=None):
            await self.release_send.wait()

        async def stop(self):
            self.stop_calls += 1
            self.release_send.set()

        async def close(self):
            return None

    monkeypatch.setattr(harness_session, "HarnessSession", FakeHarnessSession)
    monkeypatch.setattr(server, "configure_device", lambda _serial: None)
    monkeypatch.setattr(server, "clear_device", lambda: None)
    monkeypatch.setattr(tools, "CONTROL", tools.CONTROL)
    monkeypatch.setattr(tools, "RUN_CONTEXT", tools.RUN_CONTEXT)
    monkeypatch.setattr(tools, "TRACE_DIR", tools.TRACE_DIR)

    async def scenario():
        channel = queue.Queue()
        commands = queue.Queue()
        commands.put({
            "action": "send", "turn_id": "turn-running", "text": "执行测试",
            "turn_mode": "", "effort": "low",
        })
        commands.put({"action": "shutdown"})
        attempt_dir = tmp_path / "attempt"
        attempt_dir.mkdir()

        await _interactive_child({
            "attempt_dir": str(attempt_dir),
            "uploads_dir": str(tmp_path / "uploads"),
            "runs_root": str(tmp_path / "runs"),
            "architecture": "native_harness",
            "serial": "phone-a",
            "device": {"status": "ready", "selected_serial": "phone-a"},
            "parent_run_id": "parent-run",
            "provider_env": {},
            "effort": "low",
        }, channel, commands)

        assert len(instances) == 1
        assert instances[0].stop_calls == 1

    asyncio.run(scenario())


def test_native_stop_has_a_hard_limit_and_emits_one_terminal_contract(monkeypatch, tmp_path):
    import executor as executor_module
    import harness_session
    import server
    import tools

    instances = []

    class FakeExcel:
        active_suite_id = "suite-active"

    class FakeHarnessSession:
        def __init__(self, *args, **kwargs):
            self.test_excel = FakeExcel()
            self._operator_client = None
            instances.append(self)

        async def connect(self):
            return None

        async def change_effort(self, _effort):
            return None

        async def prepare_turn(self, _mode):
            return ""

        async def send(self, _text, file=None, phase_timer=None):
            await asyncio.Event().wait()

        async def stop(self):
            await asyncio.Event().wait()

        async def close(self):
            return None

    monkeypatch.setattr(executor_module, "_SDK_STOP_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(executor_module, "_DEVICE_ATOMIC_STOP_GRACE_SECONDS", 0.02)
    monkeypatch.setattr(executor_module, "_STOP_HARD_LIMIT_SECONDS", 0.1)
    monkeypatch.setattr(harness_session, "HarnessSession", FakeHarnessSession)
    monkeypatch.setattr(server, "configure_device", lambda _serial: None)
    monkeypatch.setattr(server, "clear_device", lambda: None)
    monkeypatch.setattr(tools, "CONTROL", tools.CONTROL)
    monkeypatch.setattr(tools, "RUN_CONTEXT", tools.RUN_CONTEXT)
    monkeypatch.setattr(tools, "TRACE_DIR", tools.TRACE_DIR)

    async def scenario():
        channel = queue.Queue()
        commands = queue.Queue()
        commands.put({
            "action": "send", "turn_id": "turn-stuck", "text": "执行测试",
            "turn_mode": "", "effort": "low",
        })
        commands.put({"action": "stop", "reason": "user_stop"})
        commands.put({"action": "shutdown"})
        attempt_dir = tmp_path / "attempt"
        attempt_dir.mkdir()

        started = __import__("time").perf_counter()
        await asyncio.wait_for(_interactive_child({
            "attempt_dir": str(attempt_dir),
            "uploads_dir": str(tmp_path / "uploads"),
            "runs_root": str(tmp_path / "runs"),
            "architecture": "native_harness",
            "serial": "phone-a",
            "device": {"status": "ready", "selected_serial": "phone-a"},
            "parent_run_id": "parent-run",
            "provider_env": {},
            "effort": "low",
        }, channel, commands), timeout=0.5)

        assert __import__("time").perf_counter() - started < 0.5
        messages = []
        while not channel.empty():
            messages.append(channel.get_nowait())
        event_types = [
            item["event"].get("type") for item in messages
            if item.get("kind") == "event"
        ]
        assert event_types.count("turn_stopped") == 1
        assert event_types.count("chat_done") == 1
        assert sum(item.get("kind") == "turn_done" for item in messages) == 1

    asyncio.run(scenario())


def test_interactive_manager_stop_resolves_waiting_human_without_answer(tmp_path):
    async def scenario():
        device = {"status": "ready", "selected_serial": "phone-a", "detail": {}}
        manager = InteractiveExecutorManager(
            RunCoordinator(str(tmp_path), lambda event: None, lambda: device),
        )
        persisted = []

        async def persist(event):
            persisted.append(event)

        manager._persist = persist
        manager.commands = queue.Queue()
        manager._human_waiting = True
        manager._current_turn_id = "turn-2"
        manager._turn_state = "running"

        await manager.control("stop")

        assert manager.commands.get_nowait() == {"action": "stop", "reason": "user_stop"}
        assert manager._human_waiting is False
        assert persisted == [
            {"type": "control_requested", "action": "stop", "scope": "turn",
             "turn_id": "turn-2", "suite_running": False, "reason": "user_stop"},
            {"type": "human_resolved", "reason": "stopped", "turn_id": "turn-2"},
        ]

    asyncio.run(scenario())


def test_interactive_manager_repeated_stop_is_idempotent(tmp_path):
    async def scenario():
        device = {"status": "ready", "selected_serial": "phone-a", "detail": {}}
        manager = InteractiveExecutorManager(
            RunCoordinator(str(tmp_path), lambda event: None, lambda: device),
        )
        persisted = []

        async def persist(event):
            persisted.append(event)

        manager._persist = persist
        manager.commands = queue.Queue()
        manager._current_turn_id = "turn-3"
        manager._turn_state = "running"

        assert await manager.control("stop") is True
        assert await manager.control("stop") is True
        assert manager.commands.qsize() == 1
        assert [event["type"] for event in persisted] == ["control_requested"]

    asyncio.run(scenario())


def test_interactive_controlled_close_finishes_completed(tmp_path):
    async def scenario():
        device = {"status": "ready", "selected_serial": "phone-a", "detail": {}}
        coordinator = RunCoordinator(str(tmp_path), lambda event: None, lambda: device)
        manager = InteractiveExecutorManager(coordinator)
        manager.handle = coordinator.begin({"source": "interactive"})
        manifest_path = os.path.join(manager.handle.run_dir, "manifest.json")

        await manager.close()

        manifest = json.load(open(manifest_path, encoding="utf-8"))
        assert manifest["status"] == "completed"
        assert manifest["attempts"][0]["status"] == "completed"

    asyncio.run(scenario())


def test_controlled_close_completes_parent_and_interrupts_active_child(tmp_path):
    async def scenario():
        device = {"status": "ready", "selected_serial": "phone-a", "detail": {}}
        coordinator = RunCoordinator(str(tmp_path), lambda event: None, lambda: device)
        manager = InteractiveExecutorManager(coordinator)
        manager.handle = coordinator.begin({"source": "interactive"})
        parent_manifest_path = os.path.join(manager.handle.run_dir, "manifest.json")
        child = coordinator.begin_child(manager.handle.run_id, {"source": "test_excel"})
        child_manifest_path = os.path.join(child.run_dir, "manifest.json")
        manager._suite_running = True

        await manager.close()

        parent_manifest = json.load(open(parent_manifest_path, encoding="utf-8"))
        child_manifest = json.load(open(child_manifest_path, encoding="utf-8"))
        assert parent_manifest["status"] == "completed"
        assert parent_manifest["attempts"][0]["status"] == "completed"
        assert child_manifest["status"] == "interrupted"
        assert child_manifest["attempts"][0]["status"] == "interrupted"

    asyncio.run(scenario())


def test_cancelled_interactive_supervisor_does_not_mark_run_failed(tmp_path):
    class AliveProcess:
        def is_alive(self):
            return True

    async def scenario():
        device = {"status": "ready", "selected_serial": "phone-a", "detail": {}}
        coordinator = RunCoordinator(str(tmp_path), lambda event: None, lambda: device)
        manager = InteractiveExecutorManager(coordinator)
        manager.handle = coordinator.begin({"source": "interactive"})
        manifest_path = os.path.join(manager.handle.run_dir, "manifest.json")
        manager.process = AliveProcess()
        manager.commands = queue.Queue()
        future = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(manager._execute_turn(
            {}, {"turn_id": "turn-cancelled"}, future,
        ))
        await asyncio.sleep(0)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("受控取消应保留 CancelledError 语义")

        manifest = json.load(open(manifest_path, encoding="utf-8"))
        assert manifest["status"] == "running"
        manager.handle.finish("completed")

    asyncio.run(scenario())


def test_shutdown_transport_error_does_not_mark_run_failed(tmp_path):
    class AliveProcess:
        def is_alive(self):
            return True

    async def scenario():
        device = {"status": "ready", "selected_serial": "phone-a", "detail": {}}
        coordinator = RunCoordinator(str(tmp_path), lambda event: None, lambda: device)
        manager = InteractiveExecutorManager(coordinator)
        manager.handle = coordinator.begin({"source": "interactive"})
        manifest_path = os.path.join(manager.handle.run_dir, "manifest.json")
        manager.process = AliveProcess()
        manager.commands = queue.Queue()
        manager._closing = True
        future = asyncio.get_running_loop().create_future()
        future.set_exception(RuntimeError("transport disconnected during shutdown"))

        try:
            await manager._execute_turn({}, {"turn_id": "turn-shutdown"}, future)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("关闭期间的 transport 错误应按受控取消处理")

        manifest = json.load(open(manifest_path, encoding="utf-8"))
        assert manifest["status"] == "running"
        manager.handle.finish("completed")

    asyncio.run(scenario())


def test_interactive_executor_failure_marks_run_failed(tmp_path):
    class AliveProcess:
        exitcode = 23

        def is_alive(self):
            return True

    async def scenario():
        device = {"status": "ready", "selected_serial": "phone-a", "detail": {}}
        coordinator = RunCoordinator(str(tmp_path), lambda event: None, lambda: device)
        manager = InteractiveExecutorManager(coordinator)
        manager.handle = coordinator.begin({"source": "interactive"})
        record = manager.handle.record
        manifest_path = os.path.join(manager.handle.run_dir, "manifest.json")
        manager.process = AliveProcess()
        manager.commands = queue.Queue()
        future = asyncio.get_running_loop().create_future()
        future.set_exception(RuntimeError("executor crashed"))

        try:
            await manager._execute_turn({}, {"turn_id": "turn-failed"}, future)
        except RuntimeError:
            pass
        else:
            raise AssertionError("executor 崩溃必须传递失败")

        manifest = json.load(open(manifest_path, encoding="utf-8"))
        assert manifest["status"] == "failed"
        assert manifest["attempts"][0]["status"] == "failed"
        events = coordinator.store.read_events(record, 0)
        failure = next(event for event in events if event["type"] == "executor_failure")
        assert failure["source"] == "turn_execution"
        assert failure["failure_class"] == "turn_exception"
        assert failure["process_exitcode"] == 23
        assert "executor crashed" not in json.dumps(failure, ensure_ascii=False)

    asyncio.run(scenario())


def test_fatal_child_message_persists_safe_failure_signature(tmp_path):
    class AliveProcess:
        exitcode = None

        def is_alive(self):
            return True

    async def scenario():
        device = {"status": "ready", "selected_serial": "phone-a", "detail": {}}
        coordinator = RunCoordinator(str(tmp_path), lambda event: None, lambda: device)
        manager = InteractiveExecutorManager(coordinator)
        manager.handle = coordinator.begin({"source": "interactive"})
        record = manager.handle.record
        manager.process = AliveProcess()
        manager.events = queue.Queue()
        manager.events.put({
            "kind": "fatal",
            "error_type": "OSError",
            "message": "token=do-not-store C:/private/customer.txt",
        })

        await manager._pump()

        persisted = coordinator.store.read_events(record, 0)
        failure = next(event for event in persisted if event["type"] == "executor_failure")
        serialized = json.dumps(failure, ensure_ascii=False)
        assert failure["source"] == "event_pump"
        assert failure["failure_class"] == "worker_fatal"
        assert failure["child_error_type"] == "OSError"
        assert failure["process_alive"] is True
        assert failure["process_exitcode"] is None
        assert "do-not-store" not in serialized
        assert "customer.txt" not in serialized

    asyncio.run(scenario())


def test_dead_child_process_persists_exitcode(tmp_path):
    class DeadProcess:
        exitcode = -9

        def is_alive(self):
            return False

    async def scenario():
        device = {"status": "ready", "selected_serial": "phone-a", "detail": {}}
        coordinator = RunCoordinator(str(tmp_path), lambda event: None, lambda: device)
        manager = InteractiveExecutorManager(coordinator)
        manager.handle = coordinator.begin({"source": "interactive"})
        record = manager.handle.record
        manager.process = DeadProcess()
        manager.events = queue.Queue()

        await manager._pump()

        persisted = coordinator.store.read_events(record, 0)
        failure = next(event for event in persisted if event["type"] == "executor_failure")
        assert failure["failure_class"] == "child_process_exit"
        assert failure["process_alive"] is False
        assert failure["process_exitcode"] == -9

    asyncio.run(scenario())


def test_device_link_transition_is_durable_and_redacted(tmp_path):
    async def scenario():
        device = {"status": "ready", "selected_serial": "phone-a", "detail": {}}
        coordinator = RunCoordinator(str(tmp_path), lambda event: None, lambda: device)
        manager = InteractiveExecutorManager(coordinator)
        manager.handle = coordinator.begin({"source": "interactive"})
        record = manager.handle.record

        await manager.record_device_link(
            {"status": "ready", "selected_serial": "phone-a", "devices": []},
            {
                "status": "disconnected", "selected_serial": None,
                "devices": [{"serial": "phone-b", "state": "device"}],
                "adb_returncode": 0,
            },
        )

        persisted = coordinator.store.read_events(record, 0)
        link = next(event for event in persisted if event["type"] == "device_link")
        serialized = json.dumps(link, ensure_ascii=False)
        assert link["reason"] == "selected_missing"
        assert link["adb_returncode"] == 0
        assert "phone-a" not in serialized
        assert "phone-b" not in serialized
        manager.handle.finish("completed")

    asyncio.run(scenario())


def test_device_link_transition_rebinds_the_running_executor(tmp_path):
    async def scenario():
        device = {"status": "ready", "selected_serial": "phone-a", "detail": {}}
        coordinator = RunCoordinator(str(tmp_path), lambda event: None, lambda: device)
        manager = InteractiveExecutorManager(coordinator)
        manager.handle = coordinator.begin({"source": "interactive"})
        manager.commands = queue.Queue()

        await manager.record_device_link(
            {"status": "ready", "selected_serial": "phone-a", "devices": []},
            {"status": "disconnected", "selected_serial": None, "devices": []},
        )
        await manager.record_device_link(
            {"status": "disconnected", "selected_serial": None, "devices": []},
            {
                "status": "ready", "selected_serial": "phone-a",
                "devices": [{"serial": "phone-a", "state": "device"}],
            },
        )

        assert manager.commands.get_nowait() == {
            "action": "device_binding", "serial": "phone-a",
        }
        assert manager.commands.get_nowait() == {
            "action": "device_binding", "serial": "phone-a",
        }
        manager.handle.finish("completed")
        manager.handle = None

    asyncio.run(scenario())


def test_ready_snapshot_metadata_change_does_not_rebind_executor(tmp_path):
    async def scenario():
        device = {"status": "ready", "selected_serial": "phone-a", "detail": {}}
        coordinator = RunCoordinator(str(tmp_path), lambda event: None, lambda: device)
        manager = InteractiveExecutorManager(coordinator)
        manager.handle = coordinator.begin({"source": "interactive"})
        manager.commands = queue.Queue()

        await manager.record_device_link(
            {
                "status": "ready", "selected_serial": "phone-a", "devices": [],
                "adb_error_type": "malformed_output_recovered",
            },
            {
                "status": "ready", "selected_serial": "phone-a", "devices": [],
                "adb_error_type": "",
            },
        )

        assert manager.commands.empty()
        manager.handle.finish("completed")
        manager.handle = None

    asyncio.run(scenario())
