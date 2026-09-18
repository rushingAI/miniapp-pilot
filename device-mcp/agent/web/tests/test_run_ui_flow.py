"""run_ui_flow：由 xlsx 流程映射驱动的通用固定流程分支器。"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import server  # noqa: E402
import tools  # noqa: E402
import uiflows  # noqa: E402


def _write_flow_xlsx(path):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "流程映射"
    ws.append(["流程名", "阶段", "序", "匹配", "脚本名", "下一阶段", "失败后继续"])
    ws.append(["入口", "开始", 1, "always", "打开入口", "跳转后", "否"])
    ws.append(["入口", "跳转后", 2, "focus:Browser", "网页按钮", "", "否"])
    ws.append(["入口", "跳转后", 1, "text:允许打开", "允许跳转", "", "否"])
    wb.save(path)
    return path


def test_reads_routes_and_sorts_by_stage_sequence(tmp_path):
    flows = uiflows.read_flows(str(_write_flow_xlsx(tmp_path / "flow.xlsx")))
    assert list(flows) == ["入口"]
    assert [r["script"] for r in flows["入口"]] == ["打开入口", "允许跳转", "网页按钮"]
    assert flows["入口"][1]["continue_on_fail"] is False


def test_server_flow_waits_for_higher_priority_route(monkeypatch):
    """浏览器兜底先出现时先沉降；随后出现的高优先级弹窗必须胜出。"""
    state = {"n": 0}
    ran = []
    monkeypatch.setattr(server, "get_focused_app", lambda: "pkg/Browser")

    def screen(**kwargs):
        state["n"] += 1
        texts = ["允许打开"] if state["n"] >= 2 else []
        return 1080, 2340, [
            {"text": t, "content_desc": "", "resource_id": ""} for t in texts
        ]

    monkeypatch.setattr(server, "_screen_elements", screen)
    monkeypatch.setattr(server, "execute_ui_steps", lambda steps, **kwargs: (
        ran.append(steps[0]["target"]), {"ok": True, "done_steps": 1, "total_steps": 1, "log": []}
    )[1])
    routes = [
        {"stage": "开始", "i": 1, "match": "always", "script": "open", "next_stage": "跳转后", "continue_on_fail": False},
        {"stage": "跳转后", "i": 1, "match": "text:允许打开", "script": "allow", "next_stage": "", "continue_on_fail": False},
        {"stage": "跳转后", "i": 2, "match": "focus:Browser", "script": "browser", "next_stage": "", "continue_on_fail": False},
    ]
    scripts = {
        "open": [{"action": "press_key", "target": "home"}],
        "allow": [{"action": "tap_text", "target": "rid:ok"}],
        "browser": [{"action": "tap_xy", "target": "pct:50,50"}],
    }

    out = server.run_ui_flow(routes, scripts, route_settle_ms=5, route_timeout_s=0.1,
                             default_timeout_s=0, poll_ms=1, settle_ms=0)

    assert out["ok"] is True
    assert ran == ["home", "rid:ok"]
    assert out["route_log"][-1]["match"] == "text:允许打开"


def test_tool_exposes_flow_and_returns_failure_for_llm_fallback(monkeypatch):
    tools.set_ui_scripts({"open": [{"action": "press_key", "target": "home"}]})
    tools.set_ui_flows({"入口": [
        {"stage": "开始", "i": 1, "match": "always", "script": "open", "next_stage": "", "continue_on_fail": False},
    ]})
    monkeypatch.setattr(tools.server, "run_ui_flow", lambda *args, **kwargs: {
        "ok": False, "reason": "no_route_matched", "stage": "跳转后", "route_log": [],
    })
    tools.CONTROL = None

    result = asyncio.run(tools.run_ui_flow.handler({"flow": "入口", "vars": {}}))
    out = json.loads(result["content"][0]["text"])

    assert out == {"ok": False, "reason": "no_route_matched", "stage": "跳转后", "route_log": []}
    tools.set_ui_scripts({})
    tools.set_ui_flows({})


def test_registered_in_native_device_server():
    names = [getattr(t, "name", "") for t in tools._ALL]
    assert "run_ui_flow" in names
    _, native = tools.device_server()
    assert "mcp__device__run_ui_flow" in native


def test_flow_schema_requires_only_name_and_defaults_to_start_stage():
    schema = tools.run_ui_flow.input_schema

    assert schema["type"] == "object"
    assert schema["required"] == ["flow"]
    assert schema["properties"]["start_stage"]["default"] == "开始"
    assert "start_stage" not in schema["required"]


def test_required_flow_blocks_shortcuts_until_matching_flow_succeeds(monkeypatch):
    """Excel 绑定必须是工具边界约束，不能只靠 prompt 劝 Agent 走指定路线。"""
    tools.set_ui_scripts({"open": [{"action": "press_key", "target": "home"}]})
    tools.set_ui_flows({
        "入口": [{
            "stage": "开始", "i": 1, "match": "always", "script": "open",
            "next_stage": "", "continue_on_fail": False,
        }],
        "其他": [{
            "stage": "开始", "i": 1, "match": "always", "script": "open",
            "next_stage": "", "continue_on_fail": False,
        }],
    })
    monkeypatch.setattr(tools.server, "_wm_size", lambda: (1080, 2340))
    monkeypatch.setattr(tools.server, "execute_ui_steps", lambda actions, *_args: {
        "ok": True, "done_steps": len(actions), "total_steps": len(actions), "log": [],
    })
    monkeypatch.setattr(tools.server, "save_screenshot", lambda _path: {
        "bytes": 0, "secure": True,
    })
    monkeypatch.setattr(tools.server, "run_ui_flow", lambda *args, **kwargs: {
        "ok": True, "done_routes": 1, "stage": "开始", "route_log": [],
    })
    tools.CONTROL = None

    tools.set_required_flow("入口", "case-1")
    try:
        blocked = json.loads(asyncio.run(tools.execute_ui_actions.handler({
            "actions": [{"action": "tap_xy", "target": "xy:1,2"}],
        }))["content"][0]["text"])
        wrong = json.loads(asyncio.run(tools.run_ui_flow.handler({
            "flow": "其他", "vars": {},
        }))["content"][0]["text"])
        completed = json.loads(asyncio.run(tools.run_ui_flow.handler({
            "flow": "入口", "vars": {},
        }))["content"][0]["text"])
        allowed = json.loads(asyncio.run(tools.execute_ui_actions.handler({
            "actions": [{"action": "tap_xy", "target": "xy:1,2"}],
        }))["content"][0]["text"])
    finally:
        tools.clear_required_flow()
        tools.set_ui_scripts({})
        tools.set_ui_flows({})

    assert blocked["error"].startswith("route_required")
    assert wrong["error"].startswith("route_required")
    assert completed["ok"] is True
    assert allowed["ok"] is True


def test_failed_setup_required_flow_unlocks_manual_recovery_but_cannot_repeat_flow(monkeypatch):
    """Setup 绑定强制先尝试固定流程；失败后允许 LLM 从现场手工恢复。"""
    tools.set_ui_scripts({"open": [{"action": "press_key", "target": "home"}]})
    tools.set_ui_flows({
        "入口": [{
            "stage": "开始", "i": 1, "match": "always", "script": "open",
            "next_stage": "", "continue_on_fail": False,
        }],
    })
    monkeypatch.setattr(tools.server, "run_ui_flow", lambda *args, **kwargs: {
        "ok": False, "reason": "入口不可达", "stage": "开始", "route_log": [],
    })
    monkeypatch.setattr(tools.server, "_wm_size", lambda: (1080, 2340))
    monkeypatch.setattr(tools.server, "execute_ui_steps", lambda actions, *_args: {
        "ok": True, "done_steps": len(actions), "total_steps": len(actions), "log": [],
    })
    monkeypatch.setattr(tools.server, "save_screenshot", lambda _path: {
        "bytes": 0, "secure": True,
    })
    tools.CONTROL = None
    tools.set_required_flow("入口", "setup-0", allow_manual_fallback=True)
    try:
        before = json.loads(asyncio.run(tools.execute_ui_actions.handler({
            "actions": [{"action": "tap_xy", "target": "xy:1,2"}],
        }))["content"][0]["text"])
        failed = json.loads(asyncio.run(tools.run_ui_flow.handler({
            "flow": "入口", "vars": {},
        }))["content"][0]["text"])
        manual = json.loads(asyncio.run(tools.execute_ui_actions.handler({
            "actions": [{"action": "tap_xy", "target": "xy:1,2"}],
        }))["content"][0]["text"])
        repeated = json.loads(asyncio.run(tools.run_ui_flow.handler({
            "flow": "入口", "vars": {},
        }))["content"][0]["text"])
    finally:
        tools.clear_required_flow()
        tools.set_ui_scripts({})
        tools.set_ui_flows({})

    assert before["error"].startswith("route_required")
    assert failed["ok"] is False
    assert manual["ok"] is True
    assert repeated["error"].startswith("route_already_attempted")


def test_flow_code_has_no_app_knowledge():
    forbidden = ("微信", "wechat", "任务清单", "AppBrand", "BrowserActivity")
    blocks = [
        open(uiflows.__file__, encoding="utf-8").read(),
        open(server.__file__, encoding="utf-8").read().split("def run_ui_flow(", 1)[1].split("\n@mcp.tool()", 1)[0],
    ]
    assert not [word for word in forbidden if any(word.lower() in block.lower() for block in blocks)]
