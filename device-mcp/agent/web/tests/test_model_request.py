import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from model_request import (  # noqa: E402
    ModelRequestTracker, classify_provider_failure, classify_provider_message,
)


def test_structured_provider_statuses_are_failure_signals():
    expected = {
        429: "provider_rate_limited",
        500: "provider_internal_error",
        502: "provider_gateway_error",
        503: "provider_unavailable",
        504: "provider_gateway_error",
        529: "provider_overloaded",
    }
    for status, reason in expected.items():
        failure = classify_provider_message(SimpleNamespace(
            result="request failed", errors=[], is_error=True,
            api_error_status=status,
        ))
        assert failure == {
            "reason": reason, "signal": "api_error_status", "http_status": status,
        }


def test_known_provider_error_families_cover_multiple_vendors_and_languages():
    samples = {
        "API Error: 400 There are no healthy deployments for this model": "no_healthy_deployment",
        "Provider Error: overloaded_error": "provider_overloaded",
        "Upstream Error: service unavailable": "provider_unavailable",
        "API Error: too many requests": "provider_rate_limited",
        "API Error: 模型服务暂时不可用": "provider_unavailable",
    }
    for text, reason in samples.items():
        assert classify_provider_failure(text)["reason"] == reason


def test_assistant_prose_is_not_a_signal_but_structured_request_errors_are():
    assert classify_provider_failure(
        "日志里出现 no healthy deployments，但现在继续分析"
    ) is None
    assert classify_provider_message(SimpleNamespace(
        result="invalid request parameter", errors=[], is_error=True,
        api_error_status=400,
    ))["reason"] == "provider_request_error"
    assert classify_provider_message(SimpleNamespace(
        result="unauthorized", errors=[], is_error=True,
        api_error_status=401,
    ))["reason"] == "provider_auth_error"


def test_task_group_unwraps_the_real_provider_failure():
    grouped = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [RuntimeError("API Error: upstream service unavailable")],
    )
    failure = classify_provider_failure(grouped, trusted_error=True)
    assert failure["reason"] == "provider_unavailable"


def test_persistent_client_tracks_each_query_as_a_distinct_request():
    class Timer:
        def __init__(self): self.records = []
        def mark(self, phase, **fields): self.records.append({"phase": phase, **fields})

    timer = Timer()
    tracker = ModelRequestTracker(timer, "legacy_row")
    first = tracker.submitted()
    second = tracker.submitted()
    tracker.message(SimpleNamespace(request_id="provider-1"))
    tracker.finish_result("result")
    tracker.message(SimpleNamespace(request_id="provider-2"))
    tracker.finish_result("result")

    assert first.request_id != second.request_id
    submitted = [item for item in timer.records if item["phase"] == "model_request_submitted"]
    finished = [item for item in timer.records if item["phase"] == "model_request_finished"]
    assert [item["request_id"] for item in submitted] == [first.request_id, second.request_id]
    assert [item["request_id"] for item in finished] == [first.request_id, second.request_id]
    assert [item["provider_request_id"] for item in finished] == ["provider-1", "provider-2"]
