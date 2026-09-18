"""安全窗口不暴露数字键时，只允许精确命中真机标定后输入。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import server  # noqa: E402


def test_uses_exact_calibration_when_a11y_hides_all_keys(monkeypatch):
    taps = []
    monkeypatch.setattr(server, "_screen_elements", lambda **kwargs: (1080, 2340, []))
    monkeypatch.setattr(server, "get_focused_app", lambda: "pkg/Pwd")
    monkeypatch.setattr(
        server, "_calibrated_digit_keymap",
        lambda focused, w, h: ({str(i): [i * 10, 1000 + i] for i in range(10)},
                               f"{focused}|{w}x{h}"))
    monkeypatch.setattr(server, "_adb", lambda *args: taps.append(args))
    monkeypatch.setattr(server.time, "sleep", lambda _: None)

    out = server.tap_digits("135790")

    assert out["ok"] is True and out["via"] == "calibrated"
    assert [call[-2:] for call in taps] == [
        ("10", "1001"), ("30", "1003"), ("50", "1005"),
        ("70", "1007"), ("90", "1009"), ("0", "1000")]


def test_refuses_blind_input_without_exact_calibration(monkeypatch):
    taps = []
    monkeypatch.setattr(server, "_screen_elements", lambda **kwargs: (1080, 2340, []))
    monkeypatch.setattr(server, "get_focused_app", lambda: "pkg/Other")
    monkeypatch.setattr(
        server, "_calibrated_digit_keymap",
        lambda focused, w, h: ({}, f"{focused}|{w}x{h}"))
    monkeypatch.setattr(server, "_adb", lambda *args: taps.append(args))

    out = server.tap_digits("135790")

    assert out["ok"] is False
    assert out["available_keys"] == [] and out["calibration_key"] == "pkg/Other|1080x2340"
    assert taps == []


def test_partial_a11y_keyboard_never_uses_calibration(monkeypatch):
    elements = [{"text": "1", "clickable": True, "bounds_unreliable": False, "center": [1, 1]}]
    monkeypatch.setattr(server, "_screen_elements", lambda **kwargs: (1080, 2340, elements))
    monkeypatch.setattr(server, "get_focused_app", lambda: (_ for _ in ()).throw(AssertionError("不应读标定")))

    out = server.tap_digits("12")

    assert out["ok"] is False and out["available_keys"] == ["1"]
