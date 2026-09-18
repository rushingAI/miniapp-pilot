import os
import sys


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from analyze_runs import _operator_performance  # noqa: E402


def _phase(name, elapsed, **fields):
    return {
        "scope": "device_operator", "suite_id": "suite-1",
        "operator_id": "operator-1", "phase": name, "elapsed_ms": elapsed,
        **fields,
    }


def test_operator_performance_separates_model_gaps_tool_roundtrips_and_local_cpu():
    records = [
        _phase("operator_started", 0, prompt_chars=2000, current_step_chars=200),
        _phase("operator_connect_started", 10),
        _phase("operator_connect_completed", 110),
        _phase("operator_query_submitted", 120),
        _phase("model_first_assistant_message", 1120),
        _phase(
            "operator_tool_use_emitted", 2120, tool_use_id="tool-1",
            observer_cpu_ms=100, cli_process_cpu_ms=500,
        ),
        _phase("operator_hook_pre_received", 2150, tool_use_id="tool-1"),
        _phase("operator_hook_post_received", 3100, tool_use_id="tool-1"),
        _phase(
            "operator_tool_result_received", 3120, tool_use_id="tool-1",
            observer_cpu_ms=130, cli_process_cpu_ms=550,
            system_idle_ms=1000, system_kernel_ms=1500, system_user_ms=500,
        ),
        _phase(
            "operator_tool_use_emitted", 8120, tool_use_id="tool-2",
            observer_cpu_ms=150, cli_process_cpu_ms=650,
            system_idle_ms=1200, system_kernel_ms=1800, system_user_ms=700,
        ),
        _phase("operator_hook_pre_received", 8130, tool_use_id="tool-2"),
        _phase("operator_hook_post_received", 9100, tool_use_id="tool-2"),
        _phase("operator_tool_result_received", 9120, tool_use_id="tool-2"),
        _phase(
            "operator_result_received", 10120, duration_ms=10000, api_ms=7000,
            input_tokens=100, output_tokens=20, cache_read_input_tokens=400,
        ),
    ]

    metrics = _operator_performance(records)

    assert metrics["operator_count"] == 1
    assert metrics["connect_ms"] == [100]
    assert metrics["first_assistant_ms"] == [1000]
    assert metrics["first_tool_ms"] == [2000]
    assert metrics["tool_roundtrip_ms"] == [1000, 1000]
    assert metrics["tool_dispatch_ms"] == [30, 10]
    assert metrics["tool_and_hook_ms"] == [950, 970]
    assert metrics["tool_result_delivery_ms"] == [20, 20]
    assert metrics["model_gap_ms"] == [5000]
    assert metrics["tail_ms"] == [1000]
    assert metrics["observer_cpu_during_gap_ms"] == [20]
    assert metrics["cli_cpu_during_gap_ms"] == [100]
    assert metrics["system_busy_during_gap_pct"] == [60.0]
    assert metrics["prompt_chars"] == [2000]
    assert metrics["input_tokens"] == 100
    assert metrics["cache_read_input_tokens"] == 400
