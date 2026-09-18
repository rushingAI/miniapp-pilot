"""Transient Suite execution ownership and typed operator outcomes.

Suite Registry remains the durable source of truth.  This module only owns the
short-lived right of one foreground operator invocation to act on that Suite.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ExecutionLease:
    generation: int
    turn_epoch: int
    suite_id: str
    operator_id: str
    route: str


class ExecutionLeases:
    """Issue one in-memory execution capability and revoke it atomically."""

    def __init__(self) -> None:
        self._generation = 0
        self._current: ExecutionLease | None = None

    @property
    def current(self) -> ExecutionLease | None:
        return self._current

    def issue(self, *, turn_epoch: int, suite_id: str,
              operator_id: str, route: str) -> ExecutionLease:
        self._generation += 1
        lease = ExecutionLease(
            generation=self._generation,
            turn_epoch=int(turn_epoch),
            suite_id=str(suite_id),
            operator_id=str(operator_id),
            route=str(route),
        )
        self._current = lease
        return lease

    def revoke(self, lease: ExecutionLease | None) -> bool:
        if lease is None or self._current != lease:
            return False
        self._current = None
        return True

    def is_current(self, lease: ExecutionLease | None, *, turn_epoch: int,
                   turn_active: bool) -> bool:
        if lease is None or self._current != lease:
            return False
        return bool(turn_active) and lease.turn_epoch == int(turn_epoch)


def safe_call_fingerprint(tool_name: str, tool_input: Any) -> str:
    """Correlate retries without persisting selectors, coordinates or input text."""
    encoded = json.dumps(
        {"tool": str(tool_name), "input": tool_input},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


class NoProgressGuard:
    """Stop unsafe replay and a fourth write attempt on an unchanged step."""

    def __init__(self) -> None:
        self._step_key: tuple[str, str] | None = None
        self._last_state = ""
        self._failures = 0
        self._executed_failures: set[str] = set()

    def _select(self, suite_id: str, step_id: str) -> None:
        key = (str(suite_id), str(step_id))
        if self._step_key == key:
            return
        self._step_key = key
        self._last_state = ""
        self._failures = 0
        self._executed_failures.clear()

    def block_reason(self, *, suite_id: str, step_id: str,
                     call_fingerprint: str) -> str:
        self._select(suite_id, step_id)
        if call_fingerprint in self._executed_failures:
            return "相同设备动作已执行但后置条件失败，禁止重放；请使用动作后现场判断"
        if self._failures >= 3:
            return "当前步骤已连续三次设备写入无进展，第四次动作前已停止；请报告现场或请求用户处理"
        return ""

    def observe(self, *, suite_id: str, step_id: str, call_fingerprint: str,
                ok: bool, state_fingerprint: str,
                action_executed: bool) -> int:
        self._select(suite_id, step_id)
        if ok:
            self._last_state = ""
            self._failures = 0
            self._executed_failures.clear()
            return 0
        if action_executed:
            self._executed_failures.add(call_fingerprint)
        current_state = str(state_fingerprint or call_fingerprint)
        if current_state == self._last_state:
            self._failures += 1
        else:
            self._last_state = current_state
            self._failures = 1
        return self._failures


@dataclass(frozen=True)
class OperatorOutcome:
    status: str
    suite_id: str
    question: str = ""
    current_step_id: str = ""
    verdict: str = ""
    reason: str = ""

    def payload(self) -> dict[str, Any]:
        result = {key: value for key, value in asdict(self).items() if value}
        result["ok"] = self.status in {"completed", "needs_user_input"}
        return result


def parse_operator_outcome(*, raw_text: str, structured_output: Any,
                           suite_id: str, registry_status: str,
                           current_step_id: str) -> OperatorOutcome:
    """Validate the operator protocol without inferring state from prose."""
    if registry_status == "completed":
        return OperatorOutcome(status="completed", suite_id=suite_id)

    candidate = structured_output
    if not isinstance(candidate, dict):
        try:
            candidate = json.loads(str(raw_text or "").strip())
        except (TypeError, ValueError, json.JSONDecodeError):
            candidate = None
    if not isinstance(candidate, dict):
        return OperatorOutcome(
            status="operator_protocol_error", suite_id=suite_id,
            reason="outcome_not_json_object",
        )

    status = str(candidate.get("status") or "")
    returned_suite_id = str(candidate.get("suite_id") or "")
    if returned_suite_id != suite_id:
        return OperatorOutcome(
            status="operator_protocol_error", suite_id=suite_id,
            reason="outcome_suite_mismatch",
        )
    if status == "needs_user_input":
        question = str(candidate.get("question") or "").strip()
        returned_step_id = str(candidate.get("current_step_id") or "")
        if not question:
            reason = "missing_user_question"
        elif returned_step_id != current_step_id:
            reason = "outcome_step_mismatch"
        else:
            return OperatorOutcome(
                status=status, suite_id=suite_id, question=question,
                current_step_id=returned_step_id,
            )
        return OperatorOutcome(
            status="operator_protocol_error", suite_id=suite_id, reason=reason,
        )
    if status == "completed":
        return OperatorOutcome(
            status="operator_protocol_error", suite_id=suite_id,
            reason="completed_without_registry_completion",
        )
    return OperatorOutcome(
        status="operator_protocol_error", suite_id=suite_id,
        reason="unsupported_operator_status",
    )
