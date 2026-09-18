import asyncio
import os
import queue
import sys

_WEB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENT = os.path.dirname(_WEB)
sys.path.insert(0, _WEB)
sys.path.insert(0, _AGENT)

from executor import InteractiveExecutorManager  # noqa: E402
from model_request import classify_runtime_failure, turn_stopped_event  # noqa: E402
from run_store import RunCoordinator  # noqa: E402


def test_runtime_failure_classifier_covers_auth_request_model_and_nested_groups():
    assert classify_runtime_failure(RuntimeError("HTTP 401 unauthorized"))["reason"] == (
        "provider_auth_error"
    )
    assert classify_runtime_failure(RuntimeError("HTTP 400 invalid model: missing-model"))[
        "reason"
    ] == "invalid_model"
    assert classify_runtime_failure(RuntimeError("HTTP 422 invalid request"))["reason"] == (
        "provider_request_error"
    )
    assert classify_runtime_failure(RuntimeError("adb: no devices/emulators found"))["reason"] == (
        "device_unavailable"
    )
    nested = ExceptionGroup(
        "task group", [RuntimeError("wrapper"), RuntimeError("HTTP 503 unavailable")]
    )
    assert classify_runtime_failure(nested)["reason"] == "provider_unavailable"
    assert classify_runtime_failure("There are no healthy deployments") is None


def test_running_append_is_durable_scoped_and_forwarded(tmp_path):
    async def scenario():
        delivered = []
        device = {"status": "ready", "selected_serial": "phone-a", "detail": {}}
        manager = InteractiveExecutorManager(
            RunCoordinator(str(tmp_path), delivered.append, lambda: device)
        )
        manager.handle = manager.coordinator.begin({"source": "interactive"})
        manager.commands = queue.Queue()
        manager._turn_state = "running"
        manager._current_turn_id = "turn-1"
        manager._suite_running = True
        manager._active_suite = {
            "suite_id": "suite-1",
            "current_step": {"step_id": "case-2"},
        }

        accepted = await manager.append_input("本行改用备用入口", "message-1")

        assert accepted is True
        event = delivered[-1]
        assert event["type"] == "turn_input"
        assert event["status"] == "accepted"
        assert event["client_message_id"] == "message-1"
        assert event["turn_id"] == "turn-1"
        assert event["suite_id"] == "suite-1"
        assert event["step_id"] == "case-2"
        assert manager.commands.get_nowait() == {
            "action": "append_input",
            "text": "本行改用备用入口",
            "client_message_id": "message-1",
            "input_seq": 1,
            "turn_id": "turn-1",
            "suite_id": "suite-1",
            "step_id": "case-2",
        }
        manager.handle.finish("completed")
        manager.handle = None

    asyncio.run(scenario())


def test_stop_is_the_only_control_and_sets_stop_reason(tmp_path):
    async def scenario():
        device = {"status": "ready", "selected_serial": "phone-a", "detail": {}}
        manager = InteractiveExecutorManager(
            RunCoordinator(str(tmp_path), lambda event: None, lambda: device)
        )
        manager.commands = queue.Queue()
        manager._turn_state = "running"
        manager._current_turn_id = "turn-1"

        assert await manager.control("pause") is False
        assert await manager.control("resume") is False
        assert await manager.control("stop") is True
        assert manager.stop_reason == "user_stop"
        assert manager.commands.get_nowait() == {
            "action": "stop",
            "reason": "user_stop",
        }
        assert await manager.append_input("停止之后不得再进入当前 Turn", "late-message") is False

    asyncio.run(scenario())


def test_active_suite_is_resumable_after_parent_turn_finished(tmp_path):
    manager = InteractiveExecutorManager(
        RunCoordinator(str(tmp_path), lambda event: None, lambda: {})
    )
    manager._turn_state = "idle"
    manager._suite_running = True

    assert manager.busy is False
    assert manager.turn_state == "idle"
    assert manager.mutation_locked is True


def test_user_stop_event_is_not_mislabeled_as_provider_failure():
    event = turn_stopped_event("request-1", "legacy_row", {"reason": "user_stop"})
    assert event["signal"] == "control"
    assert "用户已停止" in event["msg"]
    assert "模型服务不可用" not in event["msg"]
