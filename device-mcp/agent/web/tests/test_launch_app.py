import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "agent"))

import server  # noqa: E402
import tools  # noqa: E402


def _cp(args, stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def test_launch_app_rejects_package_without_launcher_activity(monkeypatch):
    calls = []

    def fake_adb(*args, **kwargs):
        calls.append(args)
        if args[:3] == ("shell", "pm", "path"):
            return _cp(args, stdout="")
        raise AssertionError(f"unexpected adb call: {args}")

    monkeypatch.setattr(server, "_adb", fake_adb)
    result = server.launch_app(package="com.missing")

    assert result["ok"] is False
    assert result["reason"] == "not_installed"
    assert not any("start" in call for call in calls)


def test_launch_app_verifies_target_reached_foreground(monkeypatch):
    calls = []

    def fake_adb(*args, **kwargs):
        calls.append(args)
        if args[:3] == ("shell", "pm", "path"):
            return _cp(args, stdout="package:/data/app/com.example/base.apk\n")
        if args[:4] == ("shell", "cmd", "package", "resolve-activity"):
            return _cp(args, stdout="com.example/.MainActivity\n")
        if args[:3] == ("shell", "am", "start"):
            return _cp(args, stdout="Status: ok\n")
        raise AssertionError(f"unexpected adb call: {args}")

    monkeypatch.setattr(server, "_adb", fake_adb)
    monkeypatch.setattr(server, "get_focused_app", lambda: "com.example/.MainActivity")

    result = server.launch_app(package="com.example", timeout_s=0)

    assert result["ok"] is True
    assert result["package"] == "com.example"
    assert result["focused_app"] == "com.example/.MainActivity"
    assert ("shell", "am", "start", "-W", "-n", "com.example/.MainActivity") in calls


def test_launch_app_reports_failure_when_foreground_does_not_change(monkeypatch):
    def fake_adb(*args, **kwargs):
        if args[:3] == ("shell", "pm", "path"):
            return _cp(args, stdout="package:/data/app/com.example/base.apk\n")
        if args[:4] == ("shell", "cmd", "package", "resolve-activity"):
            return _cp(args, stdout="com.example/.MainActivity\n")
        if args[:3] == ("shell", "am", "start"):
            return _cp(args, stdout="Status: ok\n")
        raise AssertionError(f"unexpected adb call: {args}")

    monkeypatch.setattr(server, "_adb", fake_adb)
    monkeypatch.setattr(server, "get_focused_app", lambda: "com.android.launcher/.Launcher")

    result = server.launch_app(package="com.example", timeout_s=0)

    assert result["ok"] is False
    assert result["reason"] == "foreground_mismatch"


def test_launch_app_by_visible_name_uses_exact_a11y_not_visual_coordinates(monkeypatch):
    taps = []
    focuses = iter([
        "com.android.launcher/.Launcher",
        "com.example/.MainActivity",
    ])
    monkeypatch.setattr(server, "press_key", lambda key: {"ok": True, "key": key})
    monkeypatch.setattr(
        server,
        "tap_element",
        lambda **kwargs: taps.append(kwargs) or {"ok": True},
    )
    monkeypatch.setattr(server, "get_focused_app", lambda: next(focuses))

    result = server.launch_app(app_name="Example App", timeout_s=0)

    assert result["ok"] is True
    assert result["package"] == "com.example"
    assert taps == [{"text": "Example App", "contains": False}]


def test_launch_app_by_visible_name_falls_back_to_exact_content_description(monkeypatch):
    taps = []
    focuses = iter([
        "com.android.launcher/.Launcher",
        "com.example/.MainActivity",
    ])

    def tap_element(**kwargs):
        taps.append(kwargs)
        return {"ok": bool(kwargs.get("content_desc"))}

    monkeypatch.setattr(server, "press_key", lambda key: {"ok": True, "key": key})
    monkeypatch.setattr(server, "tap_element", tap_element)
    monkeypatch.setattr(server, "get_focused_app", lambda: next(focuses))

    result = server.launch_app(app_name="Example App", timeout_s=0)

    assert result["ok"] is True
    assert result["package"] == "com.example"
    assert taps == [
        {"text": "Example App", "contains": False},
        {"content_desc": "Example App"},
    ]


def test_launch_app_by_name_searches_the_generic_app_drawer_once_when_home_misses(monkeypatch):
    taps = []
    swipes = []
    attempts = iter([
        {"ok": False, "error": "missing text"},
        {"ok": False, "error": "missing desc"},
        {"ok": True},
    ])
    focuses = iter([
        "com.android.launcher/.Launcher",
        "com.example/.MainActivity",
    ])
    monkeypatch.setattr(server, "press_key", lambda key: {"ok": True, "key": key})
    monkeypatch.setattr(
        server, "tap_element",
        lambda **kwargs: taps.append(kwargs) or next(attempts),
    )
    monkeypatch.setattr(server, "_wm_size", lambda: (1080, 2340))
    monkeypatch.setattr(server, "swipe", lambda *args: swipes.append(args) or {"ok": True})
    monkeypatch.setattr(server.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(server, "get_focused_app", lambda: next(focuses))

    result = server.launch_app(app_name="Example App", timeout_s=0)

    assert result["ok"] is True
    assert result["package"] == "com.example"
    assert result["via"] == "launcher_app_drawer"
    assert taps == [
        {"text": "Example App", "contains": False},
        {"content_desc": "Example App"},
        {"text": "Example App", "contains": False},
    ]
    assert swipes == [(540, 1872, 540, 468, 300)]


def test_launch_app_prefers_unambiguous_package_when_model_sends_both(monkeypatch):
    calls = []

    def fake_adb(*args, **kwargs):
        calls.append(args)
        if args[:3] == ("shell", "pm", "path"):
            return _cp(args, stdout="package:/data/app/com.example/base.apk\n")
        if args[:4] == ("shell", "cmd", "package", "resolve-activity"):
            return _cp(args, stdout="com.example/.MainActivity\n")
        if args[:3] == ("shell", "am", "start"):
            return _cp(args, stdout="Status: ok\n")
        raise AssertionError(f"unexpected adb call: {args}")

    monkeypatch.setattr(server, "_adb", fake_adb)
    monkeypatch.setattr(server, "get_focused_app", lambda: "com.example/.MainActivity")
    monkeypatch.setattr(server, "press_key", lambda key: (_ for _ in ()).throw(
        AssertionError("不应退到桌面走 app_name 分支")))

    result = server.launch_app(package="com.example", app_name="Example App", timeout_s=0)

    assert result["ok"] is True and result["package"] == "com.example"


def test_mobile_skill_never_guesses_package_or_visually_taps_app_icons():
    skill_path = os.path.join(ROOT, "skills", "mobile-operation", "SKILL.md")
    skill = open(skill_path, encoding="utf-8").read()

    assert "只知道桌面显示名" in skill
    assert "禁止把显示名猜成包名" in skill
    assert "禁止用截图坐标盲点应用图标" in skill
