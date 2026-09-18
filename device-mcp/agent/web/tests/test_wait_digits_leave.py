"""wait_digits_leave：数字输入页原子化（等页→输→等离开）。
纯逻辑单测——stub 掉读屏/读窗口/点数字，不碰真机。
重点守三条硬纪律：没到页不点、键盘读不到不点、没走掉不重输。"""
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import server  # noqa: E402
import tools  # noqa: E402

FAST = {"poll_ms": 1, "timeout_s": 0.05, "leave_timeout_s": 0.05}


def _stub(monkeypatch, state, digits_result=None, calls=None):
    """state = {"focus": 窗口名, "texts": [屏上文本…]}；tap_digits 被调用时可改 state 模拟页面跳走。"""
    monkeypatch.setattr(server, "get_focused_app", lambda: state["focus"])
    monkeypatch.setattr(
        server, "_screen_elements",
        lambda filter_useful=True, max_elements=300: (
            1080, 2340,
            [{"text": t, "content_desc": ""} for t in state["texts"]]))

    def _td(digits, tap_confirm=False, confirm_text="确定"):
        (calls if calls is not None else []).append(digits)
        if callable(digits_result):
            return digits_result(digits)
        return digits_result if digits_result is not None else {"ok": True, "digits": digits}

    monkeypatch.setattr(server, "tap_digits", _td)


# ---------- 纪律①：没确认到页，一位数字都不点 ----------

def test_no_page_match_types_nothing(monkeypatch):
    calls = []
    _stub(monkeypatch, {"focus": "pkg/OtherPage", "texts": ["别的页"]}, calls=calls)
    out = server.wait_digits_leave("135790", page_focus_contains="TargetPwd", **FAST)
    assert out["page_matched"] is False
    assert out["typed"] is False
    assert calls == []                      # 关键：没到页 → 一位都没点


def test_both_criteria_must_hold(monkeypatch):
    """窗口对上了但页面文本没出现 → 仍不算到页（两个判据都给就都要满足）。"""
    calls = []
    _stub(monkeypatch, {"focus": "pkg/TargetPwd", "texts": ["加载中"]}, calls=calls)
    out = server.wait_digits_leave("135790", page_text="请输入密码",
                                   page_focus_contains="TargetPwd", **FAST)
    assert out["page_matched"] is False and calls == []


def test_requires_at_least_one_criterion(monkeypatch):
    calls = []
    _stub(monkeypatch, {"focus": "pkg/TargetPwd", "texts": ["请输入密码"]}, calls=calls)
    out = server.wait_digits_leave("135790")
    assert out["ok"] is False and "page_text" in out["error"] and calls == []


def test_rejects_non_digits(monkeypatch):
    calls = []
    _stub(monkeypatch, {"focus": "pkg/TargetPwd", "texts": ["请输入密码"]}, calls=calls)
    out = server.wait_digits_leave("13a790", page_focus_contains="TargetPwd")
    assert out["ok"] is False and calls == []


# ---------- 正常路径：到页 → 输入 → 页面走掉 ----------

def test_types_then_reports_left(monkeypatch):
    state = {"focus": "pkg/TargetPwd", "texts": ["请输入密码"]}
    calls = []

    def _after(digits):                      # 输完密码 → 页面跳到下一页
        state["focus"] = "pkg/NextPage"
        state["texts"] = ["验证手机号"]
        return {"ok": True, "digits": digits}

    _stub(monkeypatch, state, digits_result=_after, calls=calls)
    out = server.wait_digits_leave("135790", page_focus_contains="TargetPwd", **FAST)
    assert out["page_matched"] is True
    assert out["typed"] is True and out["digits_n"] == 6
    assert out["left"] is True
    assert out["focus_at_page"] == "pkg/TargetPwd"
    assert out["focus_after"] == "pkg/NextPage"
    assert "验证手机号" in out["after_texts"]
    assert calls == ["135790"]


def test_leaves_when_page_text_disappears(monkeypatch):
    """只给 page_text（窗口名不变，如同一宿主窗口内换页）→ 靠文本消失判离开。"""
    state = {"focus": "pkg/Host", "texts": ["请输入密码"]}

    def _after(digits):
        state["texts"] = ["已完成"]           # 窗口名没变，只有文本变了
        return {"ok": True, "digits": digits}

    _stub(monkeypatch, state, digits_result=_after)
    out = server.wait_digits_leave("135790", page_text="请输入密码", **FAST)
    assert out["typed"] is True and out["left"] is True


# ---------- 纪律②：键盘读不到 → 不点，如实回报 ----------

def test_unreadable_keypad_reports_without_leaving_phase(monkeypatch):
    calls = []
    _stub(monkeypatch, {"focus": "pkg/TargetPwd", "texts": ["请输入密码"]},
          digits_result={"ok": False, "error": "数字键盘未就绪/找不到键: {'1'}",
                         "available_keys": ["0", "2"]},
          calls=calls)
    out = server.wait_digits_leave("135790", page_focus_contains="TargetPwd", **FAST)
    assert out["page_matched"] is True
    assert out["typed"] is False
    assert out["left"] is False
    assert out["available_keys"] == ["0", "2"]      # 把现场给大脑，别让它瞎点坐标
    assert calls == ["135790"]                      # tap_digits 调了一次就返回，没重试


# ---------- 纪律③：输完没走掉 → 如实报，绝不重输 ----------

def test_not_left_never_retypes(monkeypatch):
    calls = []
    _stub(monkeypatch, {"focus": "pkg/TargetPwd", "texts": ["请输入密码"]}, calls=calls)
    out = server.wait_digits_leave("135790", page_focus_contains="TargetPwd", **FAST)
    assert out["typed"] is True
    assert out["left"] is False                     # 页面没走掉 → 如实报
    assert calls == ["135790"]                      # 关键：只输了一次，绝不重输
    assert "不要重复输入" in out["hint"]


# ---------- 接线 & 停止闸 ----------

def test_registered_in_all():
    assert any(getattr(t, "name", "") == "wait_digits_leave" for t in tools._ALL)


def test_wrapper_passes_args_through(monkeypatch):
    got = {}

    def _fake(digits, page_text, page_focus_contains, timeout_s, leave_timeout_s,
              poll_ms, tap_confirm, confirm_text, after_screenshot_path):
        got.update(locals())
        return {"page_matched": True, "typed": True, "left": True}

    monkeypatch.setattr(tools.server, "wait_digits_leave", _fake)
    tools.CONTROL = None
    res = asyncio.run(tools.wait_digits_leave.handler(
        {"digits": "135790", "page_focus_contains": "TargetPwd", "leave_timeout_s": 3}))
    out = json.loads(res["content"][0]["text"])
    assert out["left"] is True
    assert got["digits"] == "135790" and got["page_focus_contains"] == "TargetPwd"
    assert got["leave_timeout_s"] == 3.0
    assert got["timeout_s"] == 10.0 and got["poll_ms"] == 400      # 默认值原样透传


def test_wrapper_injects_atomic_after_screenshot_path(monkeypatch, tmp_path):
    got = {}

    def _fake(*args):
        got["after_screenshot_path"] = args[-1]
        with open(args[-1], "wb") as evidence:
            evidence.write(b"png")
        return {
            "page_matched": True, "typed": True, "left": True,
            "after_screenshot": {"path": args[-1], "bytes": 3, "secure": False},
        }

    monkeypatch.setattr(tools.server, "wait_digits_leave", _fake)
    tools.CONTROL = None
    tools.TRACE_DIR = str(tmp_path)
    result = asyncio.run(tools.wait_digits_leave.handler(
        {"digits": "135790", "page_focus_contains": "TargetPwd"}))
    tools.TRACE_DIR = None
    payload = json.loads(result["content"][0]["text"])

    assert got["after_screenshot_path"] == str(tmp_path / "wait_digits_after.png")
    assert payload["evidence_path"] == got["after_screenshot_path"]


def test_stopped_short_circuits_without_touching_device(monkeypatch):
    called = []
    monkeypatch.setattr(tools.server, "wait_digits_leave", lambda *a, **k: called.append(1))

    class C:
        stopped = True

    tools.CONTROL = C()
    res = asyncio.run(tools.wait_digits_leave.handler({"digits": "135790", "page_text": "x"}))
    tools.CONTROL = None
    out = json.loads(res["content"][0]["text"])
    assert out.get("stopped") is True and called == []


# ---------- 可插拔红线：新工具里不许有 app 专属知识 ----------

FORBIDDEN = ["微信", "wechat", "任务清单", "支付密码", "CheckPwd",
             "FaceFlash", "AppBrand", "135790", "123456"]


def _source_block(path: str, marker: str) -> str:
    """取 marker 起到下一个顶层 def/@ 之前的源码段。"""
    src = open(path, encoding="utf-8").read()
    i = src.index(marker)
    m = re.search(r"\n@(mcp\.tool|tool)\(", src[i + len(marker):])
    return src[i:i + len(marker) + (m.start() if m else len(src))]


def test_new_tool_has_no_app_specific_knowledge():
    """代码层必须零 app 知识——具体页面/文案/口令一律走参数，由 xlsx 执行须知提供。"""
    blocks = {                                   # 直接问模块自己在哪，别猜相对路径
        "server.wait_digits_leave": _source_block(server.__file__, "def wait_digits_leave("),
        "tools.wait_digits_leave": _source_block(tools.__file__, '"wait_digits_leave",'),
    }
    for name, block in blocks.items():
        hits = [w for w in FORBIDDEN if w.lower() in block.lower()]
        assert not hits, f"{name} 里出现 app 专属词/具体值: {hits}（应参数化、知识放 xlsx 执行须知）"


def test_redline_detector_is_not_vacuous():
    """反证:同一个探测器能在已知含 app 举例的 tap_wait_tap 段里抓到词——证明上面那条不是白过。
    （tap_wait_tap 的举例属'文档举例'、不含值与逻辑，按评估保留，此处只借它自检探测器。）"""
    block = _source_block(server.__file__, "def _tap_wait_tap_impl(")
    assert [w for w in FORBIDDEN if w.lower() in block.lower()]
