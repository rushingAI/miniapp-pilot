import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness_permissions import HarnessPermissionPolicy  # noqa: E402
from harness_workspace import HarnessWorkspace  # noqa: E402


def _decision(result):
    return (result.get("hookSpecificOutput") or {}).get("permissionDecision")


def test_workspace_import_is_hashed_and_input_is_read_only(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("hello", encoding="utf-8")
    workspace = HarnessWorkspace.create(tmp_path / "attempt")

    imported = workspace.import_file(source)

    assert imported["sha256"]
    assert imported["bytes"] == 5
    assert workspace.resolve(imported["path"], must_exist=True).read_text() == "hello"


def test_workspace_reimport_of_identical_read_only_file_is_idempotent(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("hello", encoding="utf-8")
    workspace = HarnessWorkspace.create(tmp_path / "attempt")

    first = workspace.import_file(source)
    second = workspace.import_file(source)

    assert second == first
    assert workspace.resolve(second["path"], must_exist=True).read_text() == "hello"


def test_workspace_rejects_escape_and_symlink(tmp_path):
    workspace = HarnessWorkspace.create(tmp_path / "attempt")
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace.work_dir / "link").symlink_to(outside, target_is_directory=True)

    for value in ("../../outside/file.txt", str(outside / "file.txt"), "link/file.txt"):
        try:
            workspace.resolve(value, write=True)
        except ValueError:
            pass
        else:
            raise AssertionError(f"应拒绝 {value}")


def test_policy_allows_workspace_paths_and_denies_escape(tmp_path):
    workspace = HarnessWorkspace.create(tmp_path / "attempt")
    policy = HarnessPermissionPolicy(workspace)

    ok = asyncio.run(policy.pre_tool_use({"tool_name": "Write", "tool_input": {"file_path": "a.txt"}}))
    denied = asyncio.run(policy.pre_tool_use({"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}}))

    assert ok == {}
    assert _decision(denied) == "deny"


def test_policy_hard_denies_device_bash_even_if_permission_callback_allows(tmp_path):
    async def approve(_request):
        return True

    policy = HarnessPermissionPolicy(HarnessWorkspace.create(tmp_path / "attempt"), approve)
    for command in ("adb shell input tap 1 2", "fastboot devices", "python -c 'import uiautomator2'"):
        denied = asyncio.run(policy.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": command}}))
        assert _decision(denied) == "deny"


def test_policy_denies_bash_product_source_self_diagnosis(tmp_path):
    policy = HarnessPermissionPolicy(HarnessWorkspace.create(tmp_path / "attempt"))

    denied = asyncio.run(policy.pre_tool_use({
        "tool_name": "Bash",
        "tool_input": {"command": "rg -n begin_child /tmp/miniapp-pilot/device-mcp/agent/web/run_store.py"},
    }))

    assert _decision(denied) == "deny"


def test_generic_subagent_cannot_use_device_mcp(tmp_path):
    policy = HarnessPermissionPolicy(HarnessWorkspace.create(tmp_path / "attempt"))
    policy.device_actor_id = "device-1"

    denied = asyncio.run(policy.pre_tool_use({
        "tool_name": "mcp__device__tap", "tool_input": {"x": 1, "y": 2}, "agent_id": "explore-1",
    }))
    allowed = asyncio.run(policy.pre_tool_use({
        "tool_name": "mcp__device__tap", "tool_input": {"x": 1, "y": 2}, "agent_id": "device-1",
    }))

    assert _decision(denied) == "deny"
    assert allowed == {}
