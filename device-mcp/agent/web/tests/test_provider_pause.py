import asyncio
import os
import queue
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from executor import InteractiveExecutorManager  # noqa: E402


class _Process:
    @staticmethod
    def is_alive():
        return True


def test_supervisor_maps_runtime_failure_to_stopped_without_resume_state():
    async def scenario():
        manager = InteractiveExecutorManager(object())
        manager.events = queue.Queue()
        manager.commands = queue.Queue()
        manager.process = _Process()
        manager.events.put({"kind": "event", "event": {
            "type": "turn_stopped", "request_id": "request-1",
            "architecture": "legacy_row", "reason": "provider_unavailable",
        }})
        manager.events.put({"kind": "closed"})
        await manager._pump()
        assert manager.turn_state == "stopped"
        assert manager.stop_reason == "provider_unavailable"
        assert await manager.control("resume") is False
        assert manager.commands.empty()

    asyncio.run(scenario())
