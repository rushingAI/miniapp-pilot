import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import telemetry  # noqa: E402
from telemetry import PhaseTimer, environment_fingerprint, runtime_resource_snapshot  # noqa: E402


def _jsonl(path):
    return [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]


def test_phase_timer_records_monotonic_elapsed_without_business_payload(tmp_path):
    path = tmp_path / "phase_timing.jsonl"
    timer = PhaseTimer(
        str(path), {"scope": "interactive_turn", "turn_id": "turn-1"},
        origin_ns=1_000_000, origin_utc="2026-08-05T00:00:00.000Z",
    )

    timer.mark_at("executor_received", 3_500_000, "2026-08-05T00:00:00.003Z")

    record = _jsonl(path)[0]
    assert record["elapsed_ms"] == 2.5
    assert record["turn_id"] == "turn-1"
    assert "prompt" not in record


def test_environment_fingerprint_never_persists_key_or_proxy_value(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "https://user:secret@proxy.internal:8443")
    provider = {
        "ANTHROPIC_API_KEY": "kimi-super-secret",
        "ANTHROPIC_BASE_URL": "https://api.kimi.com/coding/",
    }

    fingerprint = environment_fingerprint(provider)
    serialized = json.dumps(fingerprint)

    assert fingerprint["api_hostname"] == "api.kimi.com"
    assert fingerprint["proxy_present"] is True
    assert len(fingerprint["key_fingerprint"]) == 8
    assert "kimi-super-secret" not in serialized
    assert "proxy.internal" not in serialized


def test_runtime_resource_snapshot_is_flat_safe_and_includes_windows_cli_process(
        monkeypatch):
    monkeypatch.setattr(telemetry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(telemetry, "_windows_system_snapshot", lambda: {
        "system_idle_ms": 1000.0,
        "system_kernel_ms": 1200.0,
        "system_user_ms": 300.0,
        "system_memory_load_pct": 42,
        "system_available_memory_mb": 8192.0,
    })
    monkeypatch.setattr(telemetry, "_windows_process_snapshot", lambda pid: {
        "process_cpu_ms": float(pid), "rss_mb": 12.5,
    })

    snapshot = runtime_resource_snapshot({"cli": 4321})

    assert snapshot["observer_cpu_ms"] >= 0
    assert snapshot["system_memory_load_pct"] == 42
    assert snapshot["cli_process_cpu_ms"] == 4321.0
    assert snapshot["cli_rss_mb"] == 12.5
    assert "pid" not in snapshot
