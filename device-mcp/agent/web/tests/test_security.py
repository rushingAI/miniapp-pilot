import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from auth import claude_sdk_env


def test_safe_filename_strips_traversal():
    assert A.safe_filename("../../etc/passwd.xlsx") == "passwd.xlsx"
    assert A.safe_filename("a/b/c.xlsx") == "c.xlsx"
    assert A.safe_filename("ok.xlsx") == "ok.xlsx"


def test_safe_filename_rejects_bad_ext_or_empty():
    assert A.safe_filename("evil.sh") is None
    assert A.safe_filename("") is None
    assert A.safe_filename(".hidden") is None
    assert A.safe_filename("x.xlsx") == "x.xlsx"
    assert A.safe_filename("legacy.xls") is None


def test_evidence_path_confined(tmp_path):
    out = str(tmp_path)
    open(os.path.join(out, "a.png"), "w").close()
    assert A.evidence_path(out, "a.png") == os.path.realpath(os.path.join(out, "a.png"))
    assert A.evidence_path(out, "../a.png") is None
    assert A.evidence_path(out, "/etc/passwd") is None
    assert A.evidence_path(out, "missing.png") is None


def test_download_only_serves_named_artifact_inside_run(monkeypatch, tmp_path):
    run = tmp_path / "run"
    output = run / "attempts" / "a1" / "workspace" / "output"
    output.mkdir(parents=True)
    (output / "report.txt").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(A, "run_dir", lambda run_id: str(run) if run_id == "r1" else None)

    response = __import__("asyncio").run(A.download("r1", "report.txt"))
    denied = __import__("asyncio").run(A.download("r1", "../report.txt"))

    assert response.path == str(output / "report.txt")
    assert denied.status_code == 404


def test_per_user_key_override_does_not_mutate_process_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "developer-key")

    env = claude_sdk_env(api_key="colleague-key")

    assert env["ANTHROPIC_API_KEY"] == "colleague-key"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.kimi.com/coding/"
    assert env["ANTHROPIC_MODEL"] == "k3[1m]"
    assert os.environ["ANTHROPIC_API_KEY"] == "developer-key"


def test_per_user_base_url_override_is_optional_and_in_memory(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

    default_env = claude_sdk_env(api_key="colleague-key", base_url="")
    custom_env = claude_sdk_env(
        api_key="colleague-key", base_url="https://kimi-gateway.example.com/claude/",
    )

    assert default_env["ANTHROPIC_BASE_URL"] == "https://api.kimi.com/coding/"
    assert custom_env["ANTHROPIC_BASE_URL"] == "https://kimi-gateway.example.com/claude/"
    assert "ANTHROPIC_BASE_URL" not in os.environ


def test_per_user_model_defaults_to_k3_and_updates_all_agent_aliases(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "developer-model")

    default_env = claude_sdk_env(api_key="colleague-key", model="")
    custom_env = claude_sdk_env(api_key="colleague-key", model="k3-256k")

    assert default_env["ANTHROPIC_MODEL"] == "k3[1m]"
    for key in (
        "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL",
    ):
        assert custom_env[key] == "k3-256k"
    assert os.environ["ANTHROPIC_MODEL"] == "developer-model"


def test_provider_key_endpoint_returns_only_fingerprint(monkeypatch):
    import asyncio

    monkeypatch.setattr(A, "INTERACTIVE_MANAGER", None)
    monkeypatch.setattr(A, "TURN_TASK", None)
    monkeypatch.setattr(A, "PROVIDER_CONFIGURED", False)
    monkeypatch.setattr(A, "PROVIDER_KEY_FINGERPRINT", "")
    monkeypatch.setattr(A, "PROVIDER_ENV", None)
    monkeypatch.setattr(A, "PROVIDER_MODEL", "k3[1m]")
    monkeypatch.setattr(A.tools, "RUN_CONTEXT", {})

    result = asyncio.run(A.set_provider_key({
        "api_key": "kimi-secret-token",
        "anthropic_base_url": "https://gateway.example.com/coding/",
        "model": "k3-256k",
    }))

    assert result["ok"] is True
    assert result["provider"]["storage"] == "memory"
    assert result["provider"]["fingerprint"] != "kimi-secret-token"
    assert A.PROVIDER_ENV["ANTHROPIC_API_KEY"] == "kimi-secret-token"
    assert A.PROVIDER_ENV["ANTHROPIC_BASE_URL"] == "https://gateway.example.com/coding/"
    assert A.PROVIDER_ENV["ANTHROPIC_MODEL"] == "k3-256k"
    assert result["provider"]["model"] == "k3-256k"


def test_provider_key_can_be_remembered_without_returning_secret(monkeypatch):
    import asyncio

    saved = {}
    monkeypatch.setattr(A, "INTERACTIVE_MANAGER", None)
    monkeypatch.setattr(A, "TURN_TASK", None)
    monkeypatch.setattr(A, "PROVIDER_CONFIGURED", False)
    monkeypatch.setattr(A, "PROVIDER_KEY_FINGERPRINT", "")
    monkeypatch.setattr(A, "PROVIDER_ENV", None)
    monkeypatch.setattr(A, "PROVIDER_MODEL", "k3[1m]")
    monkeypatch.setattr(A.tools, "RUN_CONTEXT", {})
    monkeypatch.setattr(
        A.PROVIDER_STORE, "save",
        lambda key, base_url, model: saved.update(
            key=key, base_url=base_url, model=model,
        ),
    )

    result = asyncio.run(A.set_provider_key({
        "api_key": "kimi-secret-token",
        "anthropic_base_url": "https://gateway.example.com/coding/",
        "model": "k3-256k",
        "remember": True,
    }))

    assert saved == {
        "key": "kimi-secret-token",
        "base_url": "https://gateway.example.com/coding/",
        "model": "k3-256k",
    }
    assert "kimi-secret-token" not in json.dumps(result)
    assert result["provider"]["storage"] == "system_credential"


def test_blank_fields_reuse_current_provider_values(monkeypatch):
    import asyncio

    monkeypatch.setattr(A, "INTERACTIVE_MANAGER", None)
    monkeypatch.setattr(A, "TURN_TASK", None)
    monkeypatch.setattr(A, "PROVIDER_CONFIGURED", True)
    monkeypatch.setattr(A, "PROVIDER_KEY_FINGERPRINT", "existing")
    monkeypatch.setattr(A, "PROVIDER_ENV", claude_sdk_env(
        api_key="existing-secret",
        base_url="https://gateway.example.com/coding/",
        model="k3-256k",
    ))
    monkeypatch.setattr(A, "PROVIDER_MODEL", "k3-256k")
    monkeypatch.setattr(A.tools, "RUN_CONTEXT", {})
    monkeypatch.setattr(A.PROVIDER_STORE, "delete", lambda: None)

    result = asyncio.run(A.set_provider_key({
        "api_key": "", "anthropic_base_url": "", "model": "", "remember": False,
    }))

    assert result["ok"] is True
    assert A.PROVIDER_ENV["ANTHROPIC_API_KEY"] == "existing-secret"
    assert A.PROVIDER_ENV["ANTHROPIC_BASE_URL"] == "https://gateway.example.com/coding/"
    assert A.PROVIDER_ENV["ANTHROPIC_MODEL"] == "k3-256k"


def test_saved_provider_is_restored_without_browser_receiving_key(monkeypatch):
    saved = {
        "api_key": "restored-secret",
        "base_url": "https://gateway.example.com/coding/",
        "model": "k3-256k",
    }
    monkeypatch.setattr(A, "PROVIDER_CONFIGURED", False)
    monkeypatch.setattr(A, "PROVIDER_KEY_FINGERPRINT", "")
    monkeypatch.setattr(A, "PROVIDER_ENV", None)
    monkeypatch.setattr(A, "PROVIDER_MODEL", "k3[1m]")
    monkeypatch.setattr(A, "PROVIDER_STORAGE", "memory")
    monkeypatch.setattr(A.PROVIDER_STORE, "load", lambda: saved)

    assert A._restore_provider_config() is True
    assert A.PROVIDER_ENV["ANTHROPIC_API_KEY"] == "restored-secret"
    assert A.PROVIDER_ENV["ANTHROPIC_BASE_URL"] == saved["base_url"]
    assert A.PROVIDER_MODEL == saved["model"]
    assert A.PROVIDER_STORAGE == "system_credential"
    assert A.PROVIDER_KEY_FINGERPRINT != "restored-secret"


def test_provider_key_endpoint_rejects_invalid_base_url(monkeypatch):
    import asyncio

    monkeypatch.setattr(A, "INTERACTIVE_MANAGER", None)
    monkeypatch.setattr(A, "TURN_TASK", None)

    result = asyncio.run(A.set_provider_key({
        "api_key": "kimi-secret-token", "anthropic_base_url": "not-a-url",
    }))

    assert result.status_code == 400


def test_provider_key_endpoint_rejects_invalid_model_name(monkeypatch):
    import asyncio

    monkeypatch.setattr(A, "INTERACTIVE_MANAGER", None)
    monkeypatch.setattr(A, "TURN_TASK", None)

    result = asyncio.run(A.set_provider_key({
        "api_key": "kimi-secret-token", "model": "model with spaces",
    }))

    assert result.status_code == 400
