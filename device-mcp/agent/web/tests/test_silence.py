import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from silence import SilenceObserver  # noqa: E402


def test_default_silence_threshold_is_240_seconds():
    observer = SilenceObserver(lambda _event: None)
    assert observer.warn_after == 240.0


def test_silence_observer_warns_once_without_retrying():
    async def scenario():
        events = []

        async def emit(event):
            events.append(event)

        observer = SilenceObserver(emit, warn_after=0.02)
        task = asyncio.create_task(observer.watch())
        await asyncio.sleep(0.07)
        observer.close()
        await task
        return events

    events = asyncio.run(scenario())
    assert [event["level"] for event in events] == [1]
    assert all(event["type"] == "silence_warning" for event in events)


def test_known_user_wait_does_not_emit_silence_warning():
    async def scenario():
        events = []
        observer = SilenceObserver(events.append, warn_after=0.01,
                                   waiting=lambda: True)

        async def emit(event):
            events.append(event)
        observer.emit = emit
        task = asyncio.create_task(observer.watch())
        await asyncio.sleep(0.04)
        observer.close()
        await task
        return events

    assert asyncio.run(scenario()) == []
