import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import events as e


def test_helpers_carry_type_and_payload():
    assert e.chat_delta("hi")["type"] == "chat_delta"
    assert e.chat_delta("hi")["text"] == "hi"
    assert e.user_message("打开应用", client_message_id="m1")["client_message_id"] == "m1"
    assert e.tool_call("tap", "539,1794", "ok") == {
        "type": "tool_call", "name": "tap", "summary": "539,1794", "status": "ok"}
    assert e.suite_start("r1", [{"seq": 1}])["run_id"] == "r1"
    assert e.download_ready("r1", "a.xlsx")["filename"] == "a.xlsx"
    assert e.case_result(seq=3, verdict="pass")["verdict"] == "pass"
    assert e.need_human("插好USB")["question"] == "插好USB"
    assert e.error("boom")["msg"] == "boom"


def test_tool_error_detail_keeps_reason_but_redacts_secrets():
    detail = e.safe_result_detail(
        '{"error":"evidence_paths 不合法; token=secret-token"}', True
    )
    assert "evidence_paths 不合法" in detail
    assert "secret-token" not in detail
    assert "[REDACTED]" in detail
    assert e.safe_result_detail("secret success payload", False) == "完成"


def test_user_stop_is_interrupted_instead_of_tool_failure():
    status, detail = e.tool_result_outcome(
        "The user doesn't want to proceed with this tool use. The tool use was rejected.",
        True,
    )
    assert (status, detail) == ("interrupted", "已中断")
    assert e.tool_result_outcome("设备离线", True) == ("fail", "设备离线")


def test_domain_error_payload_is_a_readable_tool_failure():
    content = [{"type": "text", "text": json.dumps({
        "error": "suite_not_active: 指定的 suite 不存在",
    }, ensure_ascii=False)}]

    assert e.tool_result_outcome(content, False) == (
        "fail", "suite_not_active: 指定的 suite 不存在",
    )


def test_domain_ok_false_payload_is_a_readable_tool_failure():
    content = [{"type": "text", "text": json.dumps({
        "ok": False,
        "reason": "映射不到测试数据「账号G1转F1」。候选: ",
    }, ensure_ascii=False)}]

    assert e.tool_result_outcome(content, False) == (
        "fail", "映射不到测试数据「账号G1转F1」。候选:",
    )


def test_tool_result_text_preserves_string_list_and_object_payloads():
    assert e.tool_result_text(" 完整结果 ") == "完整结果"
    assert e.tool_result_text([
        {"type": "text", "text": "第一段"},
        {"type": "text", "text": "第二段"},
    ]) == "第一段\n第二段"
    assert e.tool_result_text({"status": "ok", "count": 2}) == (
        '{"status": "ok", "count": 2}'
    )


def test_tool_result_text_excludes_binary_blocks_but_keeps_other_structure():
    content = [
        {"type": "text", "text": "可见结果"},
        {"type": "image", "data": "base64-image-secret", "mimeType": "image/jpeg"},
        {"type": "audio", "data": "base64-audio-secret", "mimeType": "audio/wav"},
        {"status": "ok", "count": 2},
    ]

    result = e.tool_result_text(content)

    assert result == '可见结果\n{"status": "ok", "count": 2}'
    assert "base64-image-secret" not in result
    assert "base64-audio-secret" not in result


def test_step_timing_has_precise_time_actor_and_step_identity():
    event = e.step_timing(
        "next_requested", "device_operator",
        {"step_id": "case-2", "kind": "case", "seq": 2},
        architecture="native_harness", agent_id="agent-1",
    )

    assert event["type"] == "step_timing"
    assert event["point"] == "next_requested"
    assert event["actor"] == "device_operator"
    assert event["agent_id"] == "agent-1"
    assert event["architecture"] == "native_harness"
    assert event["step_id"] == "case-2"
    assert event["seq"] == 2
    assert isinstance(event["observed_at_ms"], int)


def test_device_link_event_hashes_serials_and_classifies_disconnect():
    event = e.device_link(
        {
            "status": "ready",
            "selected_serial": "phone-a",
            "devices": [{"serial": "phone-a", "state": "device"}],
            "adb_returncode": 0,
        },
        {
            "status": "disconnected",
            "selected_serial": None,
            "devices": [{"serial": "phone-b", "state": "device"}],
            "adb_returncode": 0,
        },
    )

    serialized = json.dumps(event, ensure_ascii=False)
    assert event["type"] == "device_link"
    assert event["previous_status"] == "ready"
    assert event["status"] == "disconnected"
    assert event["reason"] == "selected_missing"
    assert event["adb_returncode"] == 0
    assert len(event["selected_serial_hash"]) == 64
    assert len(event["devices"][0]["serial_hash"]) == 64
    assert event["devices"][0]["state"] == "device"
    assert isinstance(event["observed_at_ms"], int)
    assert "phone-a" not in serialized
    assert "phone-b" not in serialized


def test_device_link_event_records_malformed_adb_output_reason():
    for reason in ("malformed_output", "malformed_output_recovered"):
        event = e.device_link(
            {"status": "ready", "selected_serial": "phone-a"},
            {
                "status": "ready",
                "selected_serial": "phone-a",
                "devices": [{"serial": "phone-a", "state": "device"}],
                "adb_returncode": 0,
                "adb_error_type": reason,
            },
        )

        assert event["status"] == "ready"
        assert event["reason"] == reason
        assert event["adb_returncode"] == 0
        assert len(event["selected_serial_hash"]) == 64


def test_executor_failure_event_keeps_safe_signature_without_raw_message():
    event = e.executor_failure(
        RuntimeError("password=do-not-store C:/private/customer.txt"),
        source="turn_execution",
        failure_class="turn_exception",
        child_error_type="OSError",
        process_alive=True,
        process_exitcode=23,
    )

    serialized = json.dumps(event, ensure_ascii=False)
    assert event["type"] == "executor_failure"
    assert event["source"] == "turn_execution"
    assert event["failure_class"] == "turn_exception"
    assert event["error_type"] == "RuntimeError"
    assert event["child_error_type"] == "OSError"
    assert event["process_alive"] is True
    assert event["process_exitcode"] == 23
    assert len(event["message_fingerprint"]) == 16
    assert isinstance(event["observed_at_ms"], int)
    assert "do-not-store" not in serialized
    assert "customer.txt" not in serialized
    assert "message" not in event
    assert "detail" not in event


def test_event_bus_replays_before_live_events():
    import asyncio

    class FakeSocket:
        def __init__(self):
            self.sent = []

        async def send_json(self, event):
            self.sent.append(("json", event))

    async def scenario():
        bus = e.EventBus()
        socket = FakeSocket()
        await bus.subscribe(socket, lambda: [{"type": "user_message", "event_seq": 1}])
        reliable = asyncio.create_task(bus.broadcast_loop())
        await bus.put({"type": "chat_delta", "event_seq": 2})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        reliable.cancel()
        assert socket.sent[0] == ("json", {"type": "user_message", "event_seq": 1})
        assert ("json", {"type": "chat_delta", "event_seq": 2}) in socket.sent

    asyncio.run(scenario())
