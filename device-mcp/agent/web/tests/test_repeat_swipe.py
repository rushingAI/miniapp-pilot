import asyncio
import json
import os
import sys

from PIL import Image


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "agent"))

import server  # noqa: E402
import tools  # noqa: E402


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def test_repeat_swipe_executes_requested_count_with_viewport_defaults(monkeypatch):
    calls = []
    sleeps = []
    monkeypatch.setattr(server, "_wm_size", lambda: (1000, 2000))
    monkeypatch.setattr(server, "_adb", lambda *args: calls.append(args))
    monkeypatch.setattr(server.time, "sleep", sleeps.append)

    result = server.repeat_swipe(count=3, duration_ms=250, interval_ms=80)

    assert calls == [
        ("shell", "input", "swipe", "500", "1400", "500", "600", "250"),
        ("shell", "input", "swipe", "500", "1400", "500", "600", "250"),
        ("shell", "input", "swipe", "500", "1400", "500", "600", "250"),
    ]
    assert sleeps == [0.08, 0.08]
    assert result == {
        "ok": True,
        "action": "repeat_swipe",
        "requested_count": 3,
        "completed_count": 3,
        "from": [500, 1400],
        "to": [500, 600],
        "duration_ms": 250,
        "interval_ms": 80,
        "coordinates": "viewport_default",
    }


def test_repeat_swipe_accepts_observed_explicit_coordinates(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_adb", lambda *args: calls.append(args))
    monkeypatch.setattr(server.time, "sleep", lambda _seconds: None)

    result = server.repeat_swipe(
        count=2, x1=400, y1=1500, x2=400, y2=600,
        duration_ms=300, interval_ms=0,
    )

    assert result["ok"] is True
    assert result["coordinates"] == "explicit"
    assert result["completed_count"] == 2
    assert len(calls) == 2


def test_repeat_swipe_rejects_invalid_count_without_touching_device(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_adb", lambda *args: calls.append(args))

    result = server.repeat_swipe(count=0)

    assert result["ok"] is False
    assert result["reason"] == "invalid_count"
    assert calls == []


def test_agent_repeat_swipe_schema_and_forwarding(monkeypatch):
    captured = []
    tools.CONTROL = None
    tools.set_trace_dir(None)
    monkeypatch.setattr(
        tools.server,
        "repeat_swipe",
        lambda **kwargs: captured.append(kwargs) or {
            "ok": True,
            "requested_count": kwargs["count"],
            "completed_count": kwargs["count"],
        },
    )

    result = _payload(asyncio.run(tools.repeat_swipe.handler({"count": 3})))

    assert result["completed_count"] == 3
    assert captured == [{
        "count": 3,
        "x1": None,
        "y1": None,
        "x2": None,
        "y2": None,
        "duration_ms": 250,
        "interval_ms": 80,
    }]
    schema = tools.repeat_swipe.input_schema
    assert schema["required"] == ["count"]
    assert set(schema["properties"]) == {
        "count", "x1", "y1", "x2", "y2", "duration_ms", "interval_ms",
    }
    assert any(item.name == "repeat_swipe" for item in tools._ALL)


def test_agent_repeat_swipe_uses_one_trace_envelope_for_the_whole_batch(tmp_path, monkeypatch):
    shots = []

    def fake_shot(name):
        shots.append(name)
        path = tmp_path / name
        Image.new("RGB", (108, 234), (20, 40, 60)).save(path)
        return str(path), False

    async def no_sleep(_seconds):
        return None

    tools.CONTROL = None
    tools.set_trace_dir(str(tmp_path))
    monkeypatch.setattr(tools, "_shot", fake_shot)
    monkeypatch.setattr(tools.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(tools.server, "get_focused_app", lambda: "pkg/Activity")
    monkeypatch.setattr(tools.server, "repeat_swipe", lambda **_kwargs: {
        "ok": True,
        "action": "repeat_swipe",
        "requested_count": 3,
        "completed_count": 3,
    })
    try:
        tool_result = asyncio.run(tools.repeat_swipe.handler({"count": 3}))
        result = _payload(tool_result)
        trace = [json.loads(line) for line in open(tmp_path / "trace.jsonl", encoding="utf-8")]
        calls = [json.loads(line) for line in open(tmp_path / "toolcalls.jsonl", encoding="utf-8")]
    finally:
        tools.set_trace_dir(None)

    assert result["completed_count"] == 3
    assert result["observation_source"] == "repeat_swipe_after"
    assert any(item["type"] == "image" for item in tool_result["content"])
    assert shots == ["00_repeat_swipe_before.png", "00_repeat_swipe_after.png"]
    assert len(trace) == 1 and trace[0]["action"] == "repeat_swipe"
    assert calls[-1]["sig"]["requested_count"] == 3
    assert calls[-1]["sig"]["completed_count"] == 3


def test_repeat_swipe_skill_routes_explicit_fast_repetition_without_drag_fallback():
    skill_path = os.path.join(ROOT, "skills", "mobile-operation", "SKILL.md")
    skill = open(skill_path, encoding="utf-8").read()

    assert "repeat_swipe(count=N)" in skill
    assert "repeat_swipe(count=3)" in skill
    assert "禁止拆成多个 `swipe`" in skill
    assert "禁止改用 `drag`" in skill
    assert "滑块" in skill and "持续按住" in skill
    assert "三次结束后使用工具返回的动作后画面核验" in skill
    assert "才补充一次 `screenshot`" in skill
