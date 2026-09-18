import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
import server  # noqa: E402


def test_drag_holds_moves_in_steps_and_releases(monkeypatch):
    events = []

    class Touch:
        def down(self, x, y):
            events.append(("down", x, y))

        def move(self, x, y):
            events.append(("move", x, y))

        def up(self, x, y):
            events.append(("up", x, y))

    class Device:
        touch = Touch()

    sleeps = []
    monkeypatch.setattr(server, "_dev", lambda: Device())
    monkeypatch.setattr(server.time, "sleep", sleeps.append)

    result = server.drag(100, 400, 100, 1600, hold_ms=500,
                         duration_ms=1200, steps=3)

    assert events == [
        ("down", 100, 400),
        ("move", 100, 800),
        ("move", 100, 1200),
        ("move", 100, 1600),
        ("up", 100, 1600),
    ]
    assert sleeps == [0.5, 0.4, 0.4, 0.4]
    assert result["action"] == "drag"


def test_continuous_gesture_skill_is_app_agnostic_and_adaptive():
    skill_path = os.path.join(ROOT, "skills", "mobile-operation", "SKILL.md")
    skill = open(skill_path, encoding="utf-8").read()
    section = skill.split("## 持续触屏手势", 1)[1].split("\n## ", 1)[0]

    assert "微信" not in section
    assert "小程序入口" not in section
    assert re.search(r"\d+(?:\.\d+)?\s*(?:ms|%)", section) is None
    assert "当前页面" in section
    assert "可拖内容区" in section
