"""模型截图必须能安全穿过 Claude Agent SDK 的 1 MiB JSON 帧。"""

import base64
import asyncio
import io
import json
import os
import sys

from PIL import Image


sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)
import tools  # noqa: E402


def test_high_entropy_screenshot_fits_duplicated_sdk_json_frame():
    """stream-json 会在消息和 tool_use_result 中各携带一份图片。"""
    image = Image.effect_noise((705, 1568), 100).convert("RGB")

    encoded, _quality = tools._encode_image_under(image)
    image_b64 = base64.b64encode(encoded).decode("ascii")
    image_item = {
        "type": "image",
        "data": image_b64,
        "mimeType": "image/jpeg",
    }
    frame = {
        "type": "user",
        "message": {
            "content": [{"type": "tool_result", "content": [image_item]}],
        },
        "tool_use_result": {"content": [image_item]},
    }

    frame_bytes = json.dumps(frame, separators=(",", ":")).encode("utf-8")
    assert len(encoded) <= 320_000
    assert tools.MODEL_IMAGE_MAX_BYTES == 320_000
    assert len(frame_bytes) < tools.SDK_JSON_MESSAGE_MAX_BYTES


def test_screenshot_allocates_current_trace_evidence_and_ignores_agent_path(
        tmp_path, monkeypatch):
    assert tools.screenshot.input_schema == {}
    trace_dir = tmp_path / "trace" / "case-1"
    tools.set_trace_dir(str(trace_dir))
    original_image = Image.effect_noise((705, 1568), 100).convert("RGB")
    saved_paths = []

    def save_screenshot(path):
        saved_paths.append(path)
        original_image.save(path, "PNG")
        return {
            "ok": True, "path": path, "bytes": os.path.getsize(path), "secure": False,
        }

    monkeypatch.setattr(tools.server, "save_screenshot", save_screenshot)
    try:
        result = asyncio.run(tools.screenshot.handler({
            "path": "work/case-1-home.png",
        }))
    finally:
        tools.set_trace_dir(None)

    metadata = json.loads(result["content"][0]["text"])
    encoded = base64.b64decode(result["content"][1]["data"])
    with Image.open(io.BytesIO(encoded)) as sent_image:
        sent_size = list(sent_image.size)

    assert len(saved_paths) == 1
    evidence_path = saved_paths[0]
    assert os.path.dirname(evidence_path) == str(trace_dir)
    assert evidence_path != "work/case-1-home.png"
    assert os.path.isfile(evidence_path)
    assert metadata["evidence_path"] == evidence_path
    assert len(encoded) <= tools.MODEL_IMAGE_MAX_BYTES
    assert metadata["image_wh"] == sent_size
    assert tools.LAST_SCREENSHOT_META["image_wh"] == sent_size
    assert sent_size[0] < 705 or sent_size[1] < 1568


def test_screenshot_without_evidence_context_fails_before_device_capture(monkeypatch):
    captured = []
    monkeypatch.setattr(
        tools.server, "save_screenshot", lambda path: captured.append(path),
    )
    tools.set_trace_dir(None)

    result = asyncio.run(tools.screenshot.handler({}))
    payload = json.loads(result["content"][0]["text"])

    assert payload["ok"] is False
    assert payload["error"].startswith("evidence_context_missing:")
    assert captured == []
