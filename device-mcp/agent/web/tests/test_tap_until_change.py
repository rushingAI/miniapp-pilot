import os, sys, asyncio, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import tools  # noqa: E402


def _stub_device(monkeypatch, taps, diff_seq):
    monkeypatch.setattr(tools.server, "save_screenshot", lambda p: {"path": p, "bytes": 1})
    monkeypatch.setattr(tools.server, "tap", lambda x, y: taps.append((x, y)))
    it = iter(diff_seq)
    monkeypatch.setattr(tools.server, "_postcondition_screen_diff", lambda a, b: next(it))
    tools.CONTROL = None


def test_registered_in_all():
    assert any(getattr(t, "name", "") == "tap_until_change" for t in tools._ALL)


def test_finds_button_by_stepping_up(monkeypatch):
    taps = []
    _stub_device(monkeypatch, taps, [0.0, 0.0, 50.0])   # 前两次屏幕没变,第三次变
    res = asyncio.run(tools.tap_until_change.handler(
        {"x": 500, "y": 2188, "step": 50, "settle_ms": 0}))
    out = json.loads(res["content"][0]["text"])
    assert out["changed"] is True
    assert out["tries"] == 3
    assert out["hit_xy"] == [500, 2088]                 # 原点→-50→-100,第3次在 2088 命中
    assert taps == [(500, 2188), (500, 2138), (500, 2088)]   # 逐级上移(治视觉偏低)


def test_reports_no_change_when_nothing_moves(monkeypatch):
    taps = []
    _stub_device(monkeypatch, taps, [0.0] * 8)          # 全程没变
    res = asyncio.run(tools.tap_until_change.handler(
        {"x": 500, "y": 2000, "max_tries": 4, "settle_ms": 0}))
    out = json.loads(res["content"][0]["text"])
    assert out["changed"] is False and out["tries"] == 4   # 如实报没命中,别谎报


def test_stopped_short_circuits_without_tapping(monkeypatch):
    taps = []
    _stub_device(monkeypatch, taps, [50.0])
    class C:
        stopped = True
    tools.CONTROL = C()
    res = asyncio.run(tools.tap_until_change.handler({"x": 1, "y": 2, "settle_ms": 0}))
    tools.CONTROL = None
    out = json.loads(res["content"][0]["text"])
    assert out.get("stopped") is True and taps == []       # stop 下不碰设备
