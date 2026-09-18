import asyncio
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_agent_sdk import StreamEvent, SystemMessage  # noqa: E402
from harness_session import HarnessSession  # noqa: E402
from sdk_conversation import (  # noqa: E402
    USER_STOP_GRACE_TIMEOUT_S, sdk_session_id,
)


def test_sdk_session_id_is_extracted_from_system_and_stream_messages():
    assert sdk_session_id(SystemMessage(
        subtype="init", data={"session_id": "system-session"},
    )) == "system-session"
    assert sdk_session_id(StreamEvent(
        uuid="event-1", session_id="stream-session", event={},
    )) == "stream-session"


def test_parent_agent_stop_grace_default_is_five_seconds():
    assert USER_STOP_GRACE_TIMEOUT_S == 5.0
    assert inspect.signature(HarnessSession.stop).parameters["grace_timeout_s"].default == 5.0


def test_missing_session_id_reconnects_fresh_and_warns_without_exposing_id(monkeypatch, tmp_path):
    import harness_session as module

    class OldClient:
        def __init__(self):
            self.disconnected = False

        async def interrupt(self):
            return None

        async def disconnect(self):
            self.disconnected = True

    class NewClient:
        def __init__(self, options=None):
            self.options = options
            self.connected = False

        async def connect(self):
            self.connected = True

    async def scenario():
        emitted = []

        async def emit(event):
            emitted.append(event)

        monkeypatch.setattr(module, "ClaudeSDKClient", NewClient)
        session = HarnessSession(
            emit, attempt_dir=str(tmp_path / "attempt"), uploads_dir=str(tmp_path),
            provider_env={}, effort="low",
        )
        old = OldClient()
        session.client = old
        session._conversation().begin_turn()
        session._conversation().query_submitted()

        await session.stop(grace_timeout_s=0)
        await asyncio.sleep(0)
        await session.ensure_transport_ready()

        assert old.disconnected is True
        assert isinstance(session.client, NewClient)
        assert session.client.options.resume is None
        retired = next(
            event for event in emitted
            if event.get("type") == "sdk_transport"
            and event.get("action") == "transport_retired"
        )
        assert retired["reason"] == "stop_grace_timeout"
        assert retired["submitted_queries"] == 1
        assert retired["received_results"] == 0
        assert retired["drain_elapsed_ms"] >= 0
        assert any(
            event.get("type") == "sdk_transport" and event.get("action") == "context_lost"
            for event in emitted
        )
        assert any("模型会话未能恢复" in event.get("text", "") for event in emitted)
        assert all("session_id" not in event for event in emitted)

    asyncio.run(scenario())
