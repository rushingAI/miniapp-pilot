"""Native Harness 路径、actor 与 Bash 权限策略。"""

from __future__ import annotations

import re
from typing import Awaitable, Callable

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from harness_workspace import HarnessWorkspace


_READ_TOOLS = {"Read", "Glob", "Grep", "NotebookRead"}
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_DEVICE_BASH = re.compile(
    r"\badb(?:\.exe)?\b|\bfastboot(?:\.exe)?\b|"
    r"\buiautomator2\b|\bsubprocess\s*\.\s*(?:run|Popen)\b.*\badb\b|"
    r"\badb\s+shell\s+(?:am|input|screencap)\b",
    re.IGNORECASE | re.DOTALL,
)
_PRODUCT_SOURCE_BASH = re.compile(
    r"(?:miniapp-pilot|device-mcp|run_store\.py|executor\.py|harness_session\.py|test_excel_mcp\.py)",
    re.IGNORECASE | re.DOTALL,
)
_DEVICE_MCP_PREFIX = "mcp__device__"
_NATIVE_MUTATIONS = {
    "mcp__test_excel__open_test_suite", "mcp__test_excel__next_test_step",
    "mcp__test_excel__record_test_step", "mcp__test_excel__finish_test_suite",
    "mcp__device__prepare_test_data",
}


class HarnessPermissionPolicy:
    def __init__(self, workspace: HarnessWorkspace,
                 request_permission: Callable[[dict], Awaitable[bool]] | None = None):
        self.workspace = workspace
        self.request_permission = request_permission
        self.device_actor_id: str | None = None

    @staticmethod
    def _path(tool_input: dict) -> str:
        for key in ("file_path", "path", "notebook_path"):
            if tool_input.get(key):
                return str(tool_input[key])
        return "."

    @staticmethod
    def _deny(reason: str) -> dict:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    async def pre_tool_use(self, hook_input: dict, _tool_use_id=None, _context=None) -> dict:
        name = str(hook_input.get("tool_name") or "")
        tool_input = hook_input.get("tool_input") or {}
        agent_id = str(hook_input.get("agent_id") or "")
        try:
            if name in _READ_TOOLS:
                self.workspace.resolve(self._path(tool_input), must_exist=False)
            elif name in _WRITE_TOOLS:
                self.workspace.resolve(self._path(tool_input), write=True, must_exist=False)
        except ValueError as exc:
            return self._deny(str(exc))
        if name == "Bash" and _DEVICE_BASH.search(str(tool_input.get("command") or "")):
            return self._deny("禁止通过 Bash 直接操控手机；请使用 MiniApp Pilot 设备 MCP")
        if name == "Bash" and _PRODUCT_SOURCE_BASH.search(str(tool_input.get("command") or "")):
            return self._deny("Harness 运行时禁止用 Bash 检索 MiniApp Pilot 产品源码；请报告工具错误")
        if agent_id and (name.startswith(_DEVICE_MCP_PREFIX) or name in _NATIVE_MUTATIONS):
            if not self.device_actor_id or agent_id != self.device_actor_id:
                return self._deny("该通用子 Agent 没有设备或套件状态变更权限")
        return {}

    async def can_use_tool(self, name: str, tool_input: dict, context):
        if name != "Bash":
            return PermissionResultAllow()
        command = str(tool_input.get("command") or "")
        if _DEVICE_BASH.search(command):
            return PermissionResultDeny(message="禁止通过 Bash 直接操控手机")
        if _PRODUCT_SOURCE_BASH.search(command):
            return PermissionResultDeny(message="禁止用 Bash 检索 MiniApp Pilot 产品源码")
        if not self.request_permission:
            return PermissionResultDeny(message="Bash 需要用户逐次批准")
        approved = await self.request_permission({
            "request_id": getattr(context, "tool_use_id", None) or "",
            "tool": "Bash",
            "title": getattr(context, "title", None) or "允许执行这次 Bash？",
            "description": getattr(context, "description", None) or "",
        })
        return PermissionResultAllow() if approved else PermissionResultDeny(message="用户拒绝了 Bash")
