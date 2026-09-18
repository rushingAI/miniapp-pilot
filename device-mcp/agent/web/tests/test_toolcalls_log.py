import os, sys, asyncio, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import tools  # noqa: E402


def test_all_tools_instrumented_with_ts_ms_args_sig(tmp_path, monkeypatch):
    """原始流A:统一插桩——所有工具按调用顺序记 ts/ms/args摘要/信号/返回体大小。"""
    tools.CONTROL = None
    tools.set_trace_dir(str(tmp_path))
    monkeypatch.setattr(tools.server, "get_screen",
                        lambda: {"elements": [], "element_count": 3, "focused_app": "x", "width": 1, "height": 1})
    monkeypatch.setattr(tools.server, "find",
                        lambda *_args: {"count": 0, "matches": [], "webview_surface": True})
    monkeypatch.setattr(tools.server, "save_screenshot", lambda p: {"bytes": 0, "secure": True})
    monkeypatch.setattr(tools.server, "get_focused_app", lambda: "x/.MainActivity")
    monkeypatch.setattr(tools.server, "execute_ui_steps", lambda actions, *_args: {
        "ok": True, "done_steps": len(actions), "total_steps": len(actions), "log": [],
    })
    try:
        asyncio.run(tools.get_screen.handler({}))
        asyncio.run(tools.find.handler({"text": "手机号"}))
        asyncio.run(tools.screenshot.handler({"path": str(tmp_path / "s.png")}))
        asyncio.run(tools.execute_ui_actions.handler({
            "actions": [{"action": "press_key", "target": "back"}],
        }))
        recs = [json.loads(l) for l in open(os.path.join(str(tmp_path), "toolcalls.jsonl"))]
    finally:
        tools.set_trace_dir(None)
    assert [r["tool"] for r in recs] == ["get_screen", "find", "screenshot", "execute_ui_actions"]
    for r in recs:
        assert "ts" in r and "ms" in r and "out_chars" in r        # 时间戳/耗时/返回体大小齐
    assert recs[1]["args"]["text"] == "手机号"                     # 参数摘要(供重复簇检测)
    assert recs[1]["sig"]["count"] == 0 and recs[1]["sig"]["webview_surface"] is True
    assert recs[2]["sig"]["secure"] is True                        # 0字节安全页可辨
    assert recs[3]["args"]["actions"].startswith("[REDACTED:")


def test_no_log_when_trace_dir_unset(monkeypatch):
    tools.CONTROL = None
    tools.set_trace_dir(None)
    monkeypatch.setattr(tools.server, "get_screen",
                        lambda: {"elements": [], "element_count": 0, "focused_app": "x", "width": 1, "height": 1})
    # 对话档(无 TRACE_DIR)不该报错、也不该写盘
    asyncio.run(tools.get_screen.handler({}))   # 不抛异常即可


def test_final_assertion_and_redundant_screenshot_decisions_are_observable(tmp_path, monkeypatch):
    tools.CONTROL = None
    tools.set_trace_dir(str(tmp_path))
    monkeypatch.setattr(tools.server, "execute_ui_steps", lambda actions, *_args: {
        "ok": True, "done_steps": 1, "total_steps": 1,
        "log": [{
            "i": 1, "action": "wait", "postcondition_satisfied": True,
            "postcondition": {"satisfied": True, "checks": [{
                "type": "a11y", "state": "present", "satisfied": True,
                "matched_count": 1,
                "matched": {"text": "测试完成", "resource_id": "result"},
            }]},
        }],
    })
    monkeypatch.setattr(tools.server, "save_screenshot", lambda _path: {
        "bytes": 0, "secure": True,
    })
    monkeypatch.setattr(tools.server, "get_focused_app", lambda: "com.example/Main")
    try:
        asyncio.run(tools.execute_ui_actions.handler({
            "actions": [{
                "action": "wait",
                "postcondition": {"mode": "all", "settle_ms": 0, "timeout_s": 1,
                                  "checks": [{"type": "a11y", "selector": "text",
                                              "value": "测试完成", "state": "present"}]},
            }],
            "final_assertion": True,
        }))
        asyncio.run(tools.screenshot.handler({"path": str(tmp_path / "redundant.png")}))
        recs = [json.loads(line) for line in open(tmp_path / "toolcalls.jsonl")]
    finally:
        tools.set_trace_dir(None)

    assert recs[0]["sig"]["final_assertion_confirmed"] is True
    assert recs[0]["sig"]["action_count"] == 1
    assert recs[0]["sig"]["final_assertion_requested"] is True
    assert recs[0]["sig"]["positive_semantic_check_count"] == 1
    assert recs[0]["sig"]["weak_check_count"] == 0
    assert recs[0]["sig"]["settle_only"] is False
    assert recs[1]["sig"]["redundant_screenshot_blocked"] is True


def test_dynamic_batch_observation_metrics_do_not_log_selector_values(tmp_path, monkeypatch):
    tools.CONTROL = None
    tools.set_trace_dir(str(tmp_path))
    monkeypatch.setattr(tools.server, "execute_ui_steps", lambda actions, *_args: {
        "ok": True, "done_steps": 1, "total_steps": 1, "log": [],
    })
    monkeypatch.setattr(tools.server, "save_screenshot", lambda _path: {
        "bytes": 0, "secure": True,
    })
    monkeypatch.setattr(tools.server, "get_focused_app", lambda: "com.example/Main")
    secret_selector = "用户隐私页面文案"
    try:
        asyncio.run(tools.execute_ui_actions.handler({
            "actions": [{
                "action": "wait",
                "postcondition": {
                    "mode": "any", "settle_ms": 0, "timeout_s": 1,
                    "checks": [
                        {"type": "a11y", "selector": "text",
                         "value": secret_selector, "state": "present"},
                        {"type": "screen_change"},
                    ],
                },
            }],
            "final_assertion": True,
        }))
        record = json.loads((tmp_path / "toolcalls.jsonl").read_text().splitlines()[-1])
    finally:
        tools.set_trace_dir(None)

    assert record["sig"]["positive_semantic_check_count"] == 1
    assert record["sig"]["weak_check_count"] == 1
    assert secret_selector not in json.dumps(record, ensure_ascii=False)


def test_dynamic_batch_preflight_rejection_is_observable(tmp_path):
    tools.CONTROL = None
    tools.set_trace_dir(str(tmp_path))
    try:
        asyncio.run(tools.execute_ui_actions.handler({
            "actions": [
                {"action": "press_key", "target": "back"},
                {"action": "wait", "timeout_s": 3},
            ],
        }))
        records = [json.loads(line) for line in open(tmp_path / "toolcalls.jsonl")]
    finally:
        tools.set_trace_dir(None)

    assert records[-1]["tool"] == "execute_ui_actions"
    assert records[-1]["sig"]["preflight_rejected"] is True
    assert records[-1]["sig"]["preflight_checked"] == 2
    assert records[-1]["sig"]["fail_i"] == 2


def test_sensitive_tool_args_are_redacted_but_selectors_remain_analyzable():
    assert tools._digest_args({"text": "手机号"}, "find")["text"] == "手机号"
    assert "135790" not in tools._digest_args({"digits": "135790"}, "tap_digits")["digits"]
    assert "secret" not in tools.safe_arg_summary(
        "run_ui_steps", {"script": "登录", "vars": {"密码": "secret"}},
    )
