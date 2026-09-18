"""Native Harness 推理模式选择器：允许关闭 Thinking，默认 Low。"""
import asyncio
import os
import sys

_WEB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # device-mcp/agent/web
_AGENT = os.path.dirname(_WEB)                                              # device-mcp/agent
for _p in (_AGENT, _WEB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import app as webapp  # noqa: E402


# 合法值放行；非法值回退 Low 并告警；空值静默使用默认值。
def _drain(bus):
    """把 BUS 队列里已有事件取空，返回列表（不阻塞）。"""
    out = []
    while not bus.q.empty():
        out.append(bus.q.get_nowait())
    return out


def test_pick_effort_accepts_whitelist():
    for v in ("none", "low", "high", "max"):
        assert asyncio.run(webapp._pick_effort(v)) == v
    # 大小写/空格宽容（前端不会送，但直接 POST 会）
    assert asyncio.run(webapp._pick_effort("  HIGH ")) == "high"


def test_frontend_offers_thinking_disabled_option():
    index = os.path.join(_WEB, "index.html")
    html = open(index, encoding="utf-8").read()
    assert '<option value="none">关闭</option>' in html


def test_pick_effort_rejects_non_kimi_levels():
    """medium/xhigh 不在 Kimi 前端三档中，回退 low。"""
    for v in ("medium", "xhigh"):
        assert asyncio.run(webapp._pick_effort(v)) == "low"


def test_pick_effort_garbage_falls_back_and_warns():
    _drain(webapp.BUS)
    assert asyncio.run(webapp._pick_effort("mediun")) == "low"   # 拼错
    evs = _drain(webapp.BUS)
    assert len(evs) == 1
    assert evs[0]["type"] == "warn"          # 是 warn 不是 error——error 会让前端 unlock 撞 409
    assert "mediun" in evs[0]["msg"]


def test_pick_effort_empty_is_silent():
    """空串/缺字段=旧客户端或非前端直接 POST，静默回退，不刷警告（否则 warn 成噪音）。"""
    _drain(webapp.BUS)
    for v in ("", None, "   "):
        assert asyncio.run(webapp._pick_effort(v)) == "low"
    assert _drain(webapp.BUS) == []
