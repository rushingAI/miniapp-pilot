"""设备预览帧的降载：缩到 540 宽 JPEG。
实测依据：#screen 只显示 257x557 CSS px @dpr2 = 514 物理像素宽，推 1080 宽是 4.4 倍纯浪费；
降载后 224KB→51KB(base64)、浏览器解码 9.2ms→1.1ms。与 AI 看的截图是两条独立路径，不受影响。"""
import io
import os
import sys

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402


def _png(w, h):
    im = Image.new("RGB", (w, h))
    for x in range(0, w, 7):           # 造点花纹，免得纯色图压缩得看不出差别
        for y in range(0, h, 7):
            im.putpixel((x, y), (x % 256, y % 256, (x + y) % 256))
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def test_shrink_to_preview_width_and_jpeg():
    raw = _png(1080, 2340)
    data, mime = app.shrink_preview(raw)
    assert mime == "image/jpeg"
    im = Image.open(io.BytesIO(data))
    assert im.size == (app.PREVIEW_W, 2340 * app.PREVIEW_W // 1080)
    assert len(data) < len(raw) / 2       # 显著变小才有意义


def test_shrink_does_not_upscale_small_frames():
    raw = _png(320, 640)
    data, mime = app.shrink_preview(raw)
    assert mime == "image/jpeg"
    assert Image.open(io.BytesIO(data)).size == (320, 640)


def test_shrink_falls_back_to_original_on_bad_data():
    # 坏字节绝不能把预览循环炸掉——退回原始 PNG 让前端自己处理
    data, mime = app.shrink_preview(b"not-a-png")
    assert (data, mime) == (b"not-a-png", "image/png")


def test_screen_event_carries_mime():
    import events
    assert events.screen("AAA", "image/jpeg")["mime"] == "image/jpeg"


def test_preview_skips_adb_when_device_is_not_ready(monkeypatch):
    monkeypatch.setattr(app.DEVICE_SUPERVISOR, "snapshot", lambda: {
        "status": "unauthorized", "selected_serial": None,
    })
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("未就绪时不应截图")
    ))
    assert app._grab_preview() is None


def test_bind_device_clears_stale_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(app.tools.server, "configure_device", lambda serial: calls.append(("set", serial)))
    monkeypatch.setattr(app.tools.server, "clear_device", lambda: calls.append(("clear", None)))
    monkeypatch.setattr(app, "ACTIVE_DEVICE", "")

    app._bind_device({"status": "ready", "selected_serial": "xiaomi-14"})
    app._bind_device({"status": "offline", "selected_serial": None})

    assert calls == [("set", "xiaomi-14"), ("clear", None)]


# ---------- ①：没人连着就别采（一次 adb screencap 实测 370ms，无客户端时纯空转） ----------
def _run_loop(monkeypatch, clients, rounds=4):
    """跑 _screen_loop 若干轮后停，返回 (采集次数, 推送的事件)。sleep 换成即时，测试才跑得快。"""
    import asyncio
    calls, pushed = [], []

    def fake_grab():
        calls.append(1)
        return (b"\xff\xd8fake", "image/jpeg")

    async def fake_put(ev):
        pushed.append(ev)

    n = {"i": 0}

    async def fake_sleep(_):
        n["i"] += 1
        if n["i"] >= rounds:
            app.SCREEN_ON = False        # 跑够轮数就让 while 退出

    monkeypatch.setattr(app, "_grab_preview", fake_grab)
    monkeypatch.setattr(app.BUS, "clients", clients)
    monkeypatch.setattr(app.BUS, "put", fake_put)
    monkeypatch.setattr(app.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(app, "SCREEN_ON", True)
    asyncio.run(app._screen_loop())
    return len(calls), pushed


def test_no_capture_without_ws_clients(monkeypatch):
    calls, pushed = _run_loop(monkeypatch, clients=[])
    assert calls == 0 and pushed == []


def test_captures_and_pushes_when_client_connected(monkeypatch):
    calls, pushed = _run_loop(monkeypatch, clients=[object()], rounds=3)
    assert calls == 3
    assert all(p["type"] == "screen" and p["mime"] == "image/jpeg" for p in pushed)


def test_secure_window_frame_is_skipped(monkeypatch):
    """FLAG_SECURE 窗口 screencap 返 0 字节 → _grab_preview 给 None → 不推空帧。"""
    import asyncio
    pushed = []

    async def fake_put(ev):
        pushed.append(ev)

    n = {"i": 0}

    async def fake_sleep(_):
        n["i"] += 1
        if n["i"] >= 3:
            app.SCREEN_ON = False

    monkeypatch.setattr(app, "_grab_preview", lambda: None)
    monkeypatch.setattr(app.BUS, "clients", [object()])
    monkeypatch.setattr(app.BUS, "put", fake_put)
    monkeypatch.setattr(app.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(app, "SCREEN_ON", True)
    asyncio.run(app._screen_loop())
    assert pushed == []
