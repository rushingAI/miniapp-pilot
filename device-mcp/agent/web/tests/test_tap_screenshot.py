import asyncio
import json
import os
import sys

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import tools  # noqa: E402


def _out(result):
    return json.loads(result["content"][0]["text"])


def _write_png(path, color=(40, 90, 140)):
    Image.new("RGB", (108, 234), color).save(path)
    return {"path": str(path), "bytes": os.path.getsize(path), "secure": False}


def test_tap_digits_failure_returns_current_screen_in_the_same_result(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools.server, "tap_digits",
        lambda *_args, **_kwargs: {"ok": False, "error": "keypad unavailable"},
    )
    monkeypatch.setattr(
        tools.server, "save_screenshot", lambda path: _write_png(path),
    )
    tools.set_trace_dir(str(tmp_path / "trace"))
    try:
        result = asyncio.run(tools.tap_digits.handler({"digits": "123", "tap_confirm": False}))
    finally:
        tools.set_trace_dir(None)

    payload = _out(result)
    assert payload["ok"] is False
    assert payload["observation_source"] == "failure_current_screen"
    assert any(item["type"] == "image" for item in result["content"])


def test_specialized_digit_tool_remains_but_obsolete_tap_tools_are_gone():
    names = {getattr(tool, "name", "") for tool in tools._ALL}
    assert "tap_digits" in names
    assert {"tap", "tap_screenshot", "tap_element"}.isdisjoint(names)
