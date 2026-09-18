"""Deterministic Excel-suite state adapter for Native Harness."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl
from claude_agent_sdk import create_sdk_mcp_server, tool

_WEB = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.dirname(_WEB)
if _AGENT not in sys.path:
    sys.path.insert(0, _AGENT)

import execution_context
import events
import suite_data
import tools
import uiflows
import uiscripts

from harness_workspace import HarnessWorkspace


_VERDICTS = {"pass", "fail", "blocked", "needs_review", "cancelled"}


def _text(payload: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}]}


def _error(exc: Exception) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(
            {"error": str(exc), "action": "停止重试并向用户报告此基础设施错误"},
            ensure_ascii=False,
        )}],
        "is_error": True,
    }


@dataclass
class SuiteState:
    suite_id: str
    xlsx: Path
    sheet: str
    cases: list[suite_data.Case]
    setup: list[tuple[str, str, str]]
    notes: str
    account_context: str
    start_seq: int
    max_cases: int
    out_dir: Path
    out_xlsx: Path
    run_handle: object | None = None
    phase: str = "setup"
    setup_index: int = 0
    case_index: int = 0
    current_step: dict | None = None
    results: list[suite_data.CaseResult] = field(default_factory=list)
    setup_verdicts: list[str] = field(default_factory=list)
    stopped_at: str | int | None = None
    ready_to_finish: bool = False
    status: str = "active"
    terminal_reason: str = ""
    suite_started_monotonic: float = 0.0
    current_step_started_monotonic: float | None = None
    first_device_action_observed: bool = False
    cursor_revalidation_required: bool = False

    @property
    def finished(self) -> bool:
        return self.status != "active"


class TestExcelService:
    __test__ = False
    def __init__(self, workspace: HarnessWorkspace, *, coordinator=None,
                 parent_run_id: str = "", emit=None, control=None):
        self.workspace = workspace
        self.coordinator = coordinator
        self.parent_run_id = parent_run_id
        self.emit = emit
        self.control = control
        self.suites: dict[str, SuiteState] = {}
        self.active_suite_id = ""
        self.clock = time.perf_counter
        self._tool_actors: dict[str, tuple[str, str]] = {}

    async def _emit(self, state: SuiteState, event: dict):
        emitter = state.run_handle.emit if state.run_handle else self.emit
        if emitter:
            await emitter(event)

    def note_tool_actor(self, tool_name: str, actor: str, agent_id: str = "") -> None:
        """Attach the PreToolUse caller to the next matching MCP handler invocation."""
        self._tool_actors[str(tool_name)] = (str(actor or "unknown"), str(agent_id or ""))

    def _take_tool_actor(self, tool_name: str) -> tuple[str, str]:
        return self._tool_actors.pop(str(tool_name), ("unknown", ""))

    async def _emit_timing(self, state: SuiteState, point: str,
                           actor: tuple[str, str], step: dict | None = None,
                           **fields) -> None:
        try:
            await self._emit(state, events.step_timing(
                point, actor[0], step, architecture="native_harness",
                agent_id=actor[1], suite_id=state.suite_id, **fields,
            ))
        except Exception:
            # Observation must never alter Suite execution or cursor semantics.
            pass

    def _pending_step(self, state: SuiteState) -> dict | None:
        """Predict only step identity for observation; never advances the cursor."""
        if state.current_step:
            return state.current_step
        if state.phase == "setup" and state.setup_index < len(state.setup):
            return {
                "step_id": f"setup-{state.setup_index}", "kind": "setup",
                "index": state.setup_index,
            }
        if state.stopped_at is None and state.case_index < len(state.cases):
            case = state.cases[state.case_index]
            return {"step_id": f"case-{case.seq}", "kind": "case", "seq": case.seq}
        return None

    def require_cursor_revalidation(self) -> None:
        """A continued turn must re-claim the durable cursor before touching the device."""
        state = self.suites.get(self.active_suite_id)
        if state and state.status == "active" and not state.ready_to_finish:
            state.cursor_revalidation_required = True

    def device_action_block_reason(self) -> str:
        """Return a Native Harness guardrail reason, or empty when device work is legal."""
        state = self.suites.get(self.active_suite_id)
        if not state or state.status != "active":
            return ""
        if state.ready_to_finish or state.stopped_at is not None:
            return "当前 Suite 已进入终止收尾，只能调用 finish_test_suite，禁止继续操作设备"
        if state.cursor_revalidation_required:
            return "恢复测试后必须先调用同一 suite_id 的 next_test_step 重新确认游标"
        if not state.current_step:
            return "当前没有已领取步骤；必须先通过 open_test_suite、record_test_step 或恢复时的 next_test_step 领取步骤"
        return ""

    def _block_remaining_cases(self, state: SuiteState) -> None:
        """Complete dependent rows deterministically after a terminal fail/blocked verdict."""
        state.phase = "case"
        while state.case_index < len(state.cases):
            case = state.cases[state.case_index]
            state.results.append(suite_data.CaseResult(
                seq=case.seq, verdict="blocked",
                observed_effect=f"(前置步骤{state.stopped_at}未通过，跳过)",
            ))
            state.case_index += 1
        state.ready_to_finish = True

    async def _next_returned(self, state: SuiteState, payload: dict,
                             actor: tuple[str, str], requested_step: dict | None) -> dict:
        returned_step = payload if payload.get("step_id") else requested_step
        if payload.get("step_id"):
            state.first_device_action_observed = False
        await self._emit_timing(
            state, "next_returned", actor, returned_step,
            result_status=str(payload.get("status") or "step"),
        )
        return payload

    async def observe_device_action(self, tool_name: str, actor: str,
                                    agent_id: str = "") -> None:
        """Record the first permitted device MCP call after a step was returned."""
        state = self.suites.get(self.active_suite_id)
        if (not state or state.status != "active" or not state.current_step
                or state.first_device_action_observed):
            return
        state.first_device_action_observed = True
        await self._emit_timing(
            state, "first_device_action", (actor, agent_id), state.current_step,
            tool_name=str(tool_name or ""),
        )

    def _state(self, suite_id: str) -> SuiteState:
        requested = str(suite_id or "")
        state = self.suites.get(requested)
        if not state:
            raise ValueError("suite_not_found: suite_id 不存在")
        if requested != self.active_suite_id:
            raise ValueError(
                f"suite_superseded: suite 已不再是当前 active suite(status={state.status})"
            )
        if state.status != "active":
            raise ValueError(
                f"suite_not_active: suite 已结束(status={state.status}, reason={state.terminal_reason})"
            )
        return state

    def _state_payload(self, state: SuiteState) -> dict:
        return {
            "suite_id": state.suite_id,
            "parent_run_id": self.parent_run_id,
            "run_id": getattr(state.run_handle, "run_id", ""),
            "attempt_id": getattr(state.run_handle, "attempt_id", ""),
            "status": state.status,
            "terminal_reason": state.terminal_reason,
            "phase": state.phase,
            "setup_index": state.setup_index,
            "setup_verdicts": state.setup_verdicts,
            "case_index": state.case_index,
            "current_step": state.current_step,
            "ready_to_finish": state.ready_to_finish,
        }

    def _persist_state(self, state: SuiteState) -> None:
        path = state.out_dir / "suite_state.json"
        tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(self._state_payload(state), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    async def finalize_active(self, reason: str, *, status: str = "interrupted") -> None:
        """Finalize only an explicit Suite lifecycle transition; ordinary Stop must not call this."""
        if status not in {"interrupted", "completed", "failed"}:
            raise ValueError("Suite 终态非法")
        state = self.suites.get(self.active_suite_id)
        if not state or state.status != "active":
            self.active_suite_id = ""
            return
        summary = self._persist(state, final=status == "completed")
        state.status = status
        state.terminal_reason = reason
        self._persist_state(state)
        await self._emit(state, {
            "type": "suite_done", "summary": summary, "status": status,
            "reason": reason, "current_step": state.current_step,
            "out_xlsx": str(state.out_xlsx),
        })
        if state.run_handle:
            state.run_handle.finish(status, summary.get("verdicts"))
        self.active_suite_id = ""
        tools.set_ui_scripts({})
        tools.set_ui_flows({})
        tools.clear_required_flow()
        tools.set_trace_dir(None)
        tools.LAST_SCREENSHOT_META = None
        execution_context.set_trace_dir(None)

    async def interrupt_turn(self, reason: str = "用户停止当前执行") -> None:
        """Emit UI/audit state without changing Suite identity, cursor or verdicts."""
        state = self.suites.get(self.active_suite_id)
        if not state or state.status != "active":
            return
        self._persist_state(state)
        await self._emit(state, {
            "type": "turn_interrupted", "status": "turn_interrupted",
            "reason": reason, "suite_id": state.suite_id,
            "current_step": state.current_step,
        })

    async def open(self, args: dict) -> dict:
        actor = self._take_tool_actor("mcp__test_excel__open_test_suite")
        # open_test_suite 的语义始终是“开始新测试”。旧 Suite 即使文件/Sheet 相同也不能复用。
        filename = str(args.get("filename") or "")
        candidate = filename
        if filename == os.path.basename(filename):
            candidate = str(self.workspace.input_dir / filename)
        path = self.workspace.resolve(candidate, must_exist=True)
        if path.suffix.lower() != ".xlsx":
            raise ValueError("TestExcel MCP 只接受 .xlsx")
        requested_sheet = str(args.get("sheet") or "").strip()
        workbook = openpyxl.load_workbook(path, read_only=True)
        try:
            sheet_names = list(workbook.sheetnames)
        finally:
            workbook.close()
        sheet = requested_sheet
        if sheet not in sheet_names and requested_sheet:
            folded = requested_sheet.casefold()
            matches = [name for name in sheet_names if name.casefold() == folded]
            if not matches:
                matches = [name for name in sheet_names if name.casefold().startswith(folded)]
            if len(matches) == 1:
                sheet = matches[0]
        if sheet not in sheet_names:
            return {
                "status": "choose_sheet",
                "requested_sheet": requested_sheet,
                "available_sheets": sheet_names,
                "instruction": "从 available_sheets 选择与用户文本唯一匹配的名称后，再调用 open_test_suite；不要猜 Sheet1。",
            }
        start_seq = max(0, int(args.get("start_seq") or 0))
        max_cases = max(0, int(args.get("max_cases") or 0))
        cases = suite_data.read_cases(str(path), sheet)
        if start_seq:
            cases = [case for case in cases if case.seq >= start_seq]
        if max_cases:
            cases = cases[:max_cases]
        setup = [] if start_seq else suite_data.read_setup_steps(str(path), sheet)
        notes = "\n".join(part for part in (
            suite_data.read_notes(str(path), sheet), str(args.get("notes") or "").strip(),
        ) if part).strip()
        account = suite_data.read_login_requirement(str(path), sheet)
        scripts = uiscripts.read_scripts(str(path))
        flows = uiflows.read_flows(str(path))
        suite_data.validate_suite_flow_bindings(cases, setup, flows)
        # 先完成工作簿与绑定预检；失败时不中断旧 Suite，也不创建子 Run。
        if self.active_suite_id:
            await self.finalize_active("开始新测试", status="interrupted")
        tools.clear_required_flow()
        # A new Suite must never inherit coordinates from an older device context.
        tools.LAST_SCREENSHOT_META = None
        run_handle = None
        if self.coordinator and self.parent_run_id:
            run_handle = self.coordinator.begin_child(self.parent_run_id, {
                "source": "test_excel", "filename": path.name, "sheet": sheet,
                "start_seq": start_seq, "max_cases": max_cases,
                "architecture": "native_harness",
            })
            out_dir = Path(run_handle.attempt_dir)
        else:
            out_dir = self.workspace.output_dir / f"suite-{uuid.uuid4()}"
            out_dir.mkdir(parents=True, exist_ok=False)
        state = SuiteState(
            suite_id=str(uuid.uuid4()), xlsx=path, sheet=sheet, cases=cases, setup=setup,
            notes=notes, account_context=account, start_seq=start_seq, max_cases=max_cases,
            out_dir=out_dir, out_xlsx=out_dir / path.name, run_handle=run_handle,
        )
        state.suite_started_monotonic = self.clock()
        shutil.copyfile(path, state.out_xlsx)
        try:
            state.out_xlsx.chmod(0o644)
        except OSError:
            pass
        state.phase = "setup" if setup else "case"
        self.suites[state.suite_id] = state
        self.active_suite_id = state.suite_id
        tools.set_ui_scripts(scripts)
        tools.set_ui_flows(flows)
        self._persist_state(state)
        await self._emit(state, {
            "type": "suite_start", "suite_id": state.suite_id,
            "parent_run_id": self.parent_run_id,
            "cases": [{"seq": case.seq, "operation": case.operation, "observe": case.observe,
                       "required_flow": case.required_flow}
                      for case in cases],
            "notes_hash": __import__("hashlib").sha256(notes.encode()).hexdigest() if notes else "",
        })
        first = await self._claim_next(state, actor)
        response = {
            "suite_id": state.suite_id,
            "run_id": getattr(run_handle, "run_id", ""),
            "sheet": sheet,
            "setup_count": len(setup), "case_count": len(cases),
            "start_seq_skips_setup": bool(start_seq),
        }
        if first.get("step_id"):
            response.update({"current_step": first, "ready_to_finish": False})
        else:
            response.update({
                "current_step": None,
                "ready_to_finish": first.get("status") == "complete",
                "aggregate_verdict": first.get("verdict"),
            })
        return response

    def _activate(self, state: SuiteState, step: dict) -> dict:
        trace = state.out_dir / "trace" / str(step["step_id"])
        trace.mkdir(parents=True, exist_ok=True)
        tools.set_trace_dir(str(trace))
        execution_context.set_trace_dir(str(trace))
        tools.set_required_flow(
            step.get("required_flow", ""), step["step_id"],
            allow_manual_fallback=(step.get("kind") == "setup"),
        )
        state.current_step = step
        state.current_step_started_monotonic = self.clock()
        state.cursor_revalidation_required = False
        self._persist_state(state)
        first_payload = state.setup_index == 0 and state.case_index == 0
        return self._step_payload(state, step, include_notes=first_payload)

    def _step_payload(self, state: SuiteState, step: dict, *, include_notes: bool) -> dict:
        trace = state.out_dir / "trace" / str(step["step_id"])
        payload = {
            **step,
            "account_context": state.account_context if step.get("operation") != state.account_context else "",
            "evidence_root": str(trace),
        }
        if include_notes:
            payload["notes"] = state.notes
        else:
            payload["notes_unchanged"] = True
        return payload

    async def _claim_next(self, state: SuiteState, actor: tuple[str, str]) -> dict:
        requested_step = self._pending_step(state)
        await self._emit_timing(state, "next_requested", actor, requested_step)
        if state.current_step:
            # Stop/新 turn 后用同一 suite_id 查询时，幂等返回原步骤，绝不推进游标。
            state.cursor_revalidation_required = False
            payload = {
                "status": "resume_current",
                **self._step_payload(state, state.current_step, include_notes=True),
            }
            return await self._next_returned(state, payload, actor, requested_step)
        if self.control is not None:
            if getattr(self.control, "stopped", False):
                return await self._next_returned(
                    state, {"status": "stopped"}, actor, requested_step,
                )
        if state.phase == "setup" and state.setup_index < len(state.setup):
            op, observe, required_flow = state.setup[state.setup_index]
            step = {"step_id": f"setup-{state.setup_index}", "kind": "setup",
                    "index": state.setup_index, "operation": op, "observe": observe or "完成",
                    "required_flow": required_flow}
            await self._emit(state, {"type": "setup_step", **step})
            payload = self._activate(state, step)
            return await self._next_returned(state, payload, actor, requested_step)
        state.phase = "case"
        if state.stopped_at is not None:
            self._block_remaining_cases(state)
            self._persist(state)
            self._persist_state(state)
            payload = {"status": "complete", "verdict": suite_data.aggregate_verdict(
                           state.results, state.setup_verdicts,
                       ),
                       "suite_id": state.suite_id}
            return await self._next_returned(state, payload, actor, requested_step)
        if state.case_index >= len(state.cases):
            state.ready_to_finish = True
            self._persist_state(state)
            self._persist(state)
            payload = {"status": "complete", "verdict": suite_data.aggregate_verdict(
                           state.results, state.setup_verdicts,
                       ),
                       "suite_id": state.suite_id}
            return await self._next_returned(state, payload, actor, requested_step)
        case = state.cases[state.case_index]
        step = {"step_id": f"case-{case.seq}", "kind": "case", "seq": case.seq,
                "operation": case.operation, "observe": case.observe, "scenario": case.scenario,
                "required_flow": case.required_flow}
        await self._emit(state, {"type": "case_start", **step})
        payload = self._activate(state, step)
        return await self._next_returned(state, payload, actor, requested_step)

    async def next(self, args: dict) -> dict:
        actor = self._take_tool_actor("mcp__test_excel__next_test_step")
        completed = self.suites.get(str(args.get("suite_id") or ""))
        if completed and completed.status == "completed":
            return {
                "status": "complete", "verdict": self._completed_payload(completed)["verdict"],
                "suite_id": completed.suite_id,
            }
        state = self._state(args.get("suite_id", ""))
        return await self._claim_next(state, actor)

    def _evidence(self, state: SuiteState, raw) -> list[str]:
        values = raw if isinstance(raw, list) else [part.strip() for part in str(raw or "").split(",")]
        accepted = []
        root = state.out_dir.resolve()
        for value in values:
            if not value:
                continue
            path = Path(value).resolve(strict=True)
            if not path.is_file() or not (path == root or root in path.parents):
                raise ValueError("evidence_paths 必须是当前 suite Run 内已存在文件")
            accepted.append(str(path))
        return accepted

    def _persist(self, state: SuiteState, *, final: bool = False) -> dict:
        summary = suite_data.persist_results(
            str(state.out_dir), str(state.out_xlsx), str(state.xlsx), state.sheet, "",
            state.results, state.cases, state.setup_verdicts, final=final,
        )
        # A continuous Harness has one billing Result per user turn, not per case. Never
        # present missing child-level attribution as zero cost/tokens/turns.
        summary["total_cost_usd"] = None
        summary["tokens"] = None
        for item in summary.get("per_case", []):
            item["cost_usd"] = None
            item["turns"] = None
            item["api_s"] = None
        summary["metrics_source"] = {
            "billing": "parent_run", "step_duration": "child_run",
        }
        summary["total_duration_s"] = round(
            max(0.0, self.clock() - state.suite_started_monotonic), 3
        )
        (state.out_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for result in state.results:
            log_path = state.out_dir / f"case{result.seq}_log.txt"
            if log_path.exists() and log_path.stat().st_size == 0:
                log_path.write_text(
                    "Native Harness 使用持续主 Agent 会话。完整模型与工具日志见父 Run "
                    f"{self.parent_run_id}/events.jsonl；本 case 的证据见 trace/case-{result.seq}/。\n",
                    encoding="utf-8",
                )
        return summary

    def _step_duration_s(self, state: SuiteState) -> float:
        started = state.current_step_started_monotonic
        if started is None:
            return 0.0
        return round(max(0.0, self.clock() - started), 3)

    async def record(self, args: dict) -> dict:
        lock = getattr(self.control, "control_lock", None)
        if lock is not None:
            async with lock:
                if getattr(self.control, "stopped", False):
                    return _text({
                        "stopped": True,
                        "reason": getattr(self.control, "stop_reason", "") or "user_stop",
                        "note": "当前 Turn 已停止，未提交结果、未推进游标",
                    })
                return await self._record_committed(args)
        if getattr(self.control, "stopped", False):
            return _text({
                "stopped": True,
                "reason": getattr(self.control, "stop_reason", "") or "user_stop",
                "note": "当前 Turn 已停止，未提交结果、未推进游标",
            })
        return await self._record_committed(args)

    async def _record_committed(self, args: dict) -> dict:
        actor = self._take_tool_actor("mcp__test_excel__record_test_step")
        state = self._state(args.get("suite_id", ""))
        current = state.current_step
        if not current or str(args.get("step_id") or "") != current["step_id"]:
            raise ValueError("step_id 过期、错序或跨 suite")
        verdict = str(args.get("verdict") or "")
        if verdict not in _VERDICTS:
            raise ValueError("verdict 非法")
        required_flow = str(current.get("required_flow") or "")
        if required_flow and not tools.required_flow_satisfied(required_flow, current["step_id"]):
            flow_state = tools.required_flow_state()
            setup_manual_recovery = bool(
                current.get("kind") == "setup"
                and flow_state.get("attempted")
                and flow_state.get("allow_manual_fallback")
            )
            may_record_failure = bool(
                flow_state.get("attempted")
                and verdict in {"fail", "blocked", "needs_review"}
            )
            if not (setup_manual_recovery or may_record_failure):
                raise ValueError(
                    f"route_required: 必须先成功执行 Excel 绑定流程 {required_flow!r}"
                )
        evidence = self._evidence(state, args.get("evidence_paths"))
        if verdict == "pass" and not evidence:
            verdict = "needs_review"
        observed = str(args.get("observed_effect") or "").strip()
        duration_s = self._step_duration_s(state)
        if current["kind"] == "setup":
            await self._emit(state, {"type": "setup_result", "index": current["index"],
                                     "verdict": verdict, "observed_effect": observed,
                                     "evidence": evidence, "duration_s": duration_s})
            state.setup_index += 1
            state.setup_verdicts.append(verdict)
            if verdict in {"fail", "blocked"}:
                state.stopped_at = f"setup-{current['index']}({verdict})"
            if state.setup_index >= len(state.setup):
                state.phase = "case"
        else:
            result = suite_data.CaseResult(
                seq=int(current["seq"]), verdict=verdict, observed_effect=observed,
                evidence=evidence, duration_ms=round(duration_s * 1000),
            )
            state.results.append(result)
            state.case_index += 1
            if verdict in {"fail", "blocked"}:
                state.stopped_at = current["seq"]
            if verdict not in {"fail", "blocked"}:
                self._persist(state)
            await self._emit(state, {"type": "case_result", "seq": result.seq,
                                     "verdict": verdict, "observed_effect": observed,
                                     "evidence": evidence, "duration_s": duration_s})
        state.current_step = None
        tools.clear_required_flow()
        state.current_step_started_monotonic = None
        state.cursor_revalidation_required = False
        execution_context.set_trace_dir(None)
        terminal = verdict in {"fail", "blocked"}
        if terminal:
            self._block_remaining_cases(state)
            self._persist(state)
        self._persist_state(state)
        await self._emit_timing(
            state, "record_finished", actor, current, verdict=verdict,
        )
        if terminal:
            completed = await self._complete_suite(state)
            return {
                "ok": True,
                "verdict": verdict,
                "recorded_verdict": verdict,
                "terminal": True,
                "current_step": None,
                "ready_to_finish": False,
                "aggregate_verdict": suite_data.aggregate_verdict(
                    state.results, state.setup_verdicts,
                ),
                **completed,
            }
        following = await self._claim_next(state, actor)
        if following.get("step_id"):
            return {
                "ok": True,
                "verdict": verdict,
                "recorded_verdict": verdict,
                "current_step": following,
                "ready_to_finish": False,
            }
        completed = await self._complete_suite(state)
        return {
            "ok": True,
            "verdict": verdict,
            "recorded_verdict": verdict,
            "current_step": None,
            "ready_to_finish": False,
            "aggregate_verdict": following.get("verdict"),
            **completed,
        }

    def _completed_payload(self, state: SuiteState, summary: dict | None = None) -> dict:
        if summary is None:
            summary = json.loads((state.out_dir / "summary.json").read_text(encoding="utf-8"))
        return {
            "suite_completed": True,
            "summary": summary,
            "verdict": summary["verdict"],
            "out_xlsx": str(state.out_xlsx),
            "run_id": getattr(state.run_handle, "run_id", ""),
        }

    async def _complete_suite(self, state: SuiteState) -> dict:
        if state.status == "completed":
            return self._completed_payload(state)
        if state.current_step:
            raise ValueError("当前步骤尚未记录，不能结束套件")
        if not state.ready_to_finish:
            raise ValueError("套件仍有未完成步骤；继续处理 current_step，并由 record_test_step 返回下一步")
        summary = self._persist(state, final=True)
        state.status = "completed"
        state.terminal_reason = "全部步骤完成"
        self._persist_state(state)
        self.active_suite_id = ""
        tools.set_ui_scripts({})
        tools.set_ui_flows({})
        tools.set_trace_dir(None)
        tools.LAST_SCREENSHOT_META = None
        execution_context.set_trace_dir(None)
        await self._emit(state, {"type": "suite_done", "status": "completed",
                                 "suite_id": state.suite_id,
                                 "verdict": summary["verdict"], "summary": summary,
                                 "out_xlsx": str(state.out_xlsx)})
        await self._emit(state, {"type": "download_ready", "filename": state.out_xlsx.name})
        if state.run_handle:
            state.run_handle.finish("completed", summary.get("verdicts"))
        return self._completed_payload(state, summary)

    async def finish(self, args: dict) -> dict:
        requested = str(args.get("suite_id") or "")
        state = self.suites.get(requested)
        if state and state.status == "completed":
            return self._completed_payload(state)
        state = self._state(requested)
        return await self._complete_suite(state)

    def server(self):
        @tool("open_test_suite", "开始新的 .xlsx 测试并创建全新 suite_id，同时直接返回第一条 current_step；若已有 active suite，会先将旧 suite 标为 interrupted，绝不复用。",
              {"filename": str, "sheet": str, "start_seq": int, "max_cases": int, "notes": str})
        async def open_test_suite(args):
            try:
                return _text(await self.open(args))
            except Exception as exc:
                return _error(exc)

        @tool("next_test_step", "仅用于恢复/重验游标：若当前步骤因 turn 中断尚未记录，则幂等返回同一步并标记 resume_current。正常推进由 open_test_suite 和 record_test_step 直接返回 current_step。",
              {"suite_id": str})
        async def next_test_step(args):
            try:
                return _text(await self.next(args))
            except Exception as exc:
                return _error(exc)

        @tool("record_test_step", "记录当前步骤真实结果与证据，并直接返回下一条 current_step；最后一步会原子生成 summary/回填文件并返回 suite_completed=true，无硬证据的 pass 自动降为 needs_review。"
              "fail/blocked 是不可恢复的终止性判定，服务端会补齐后续并自动完成 Suite；"
              "可由用户解除的阻碍不要记录，保留当前步骤等待用户。"
              "evidence_paths 的每一项必须逐字复制当前步骤设备工具返回的顶层 evidence_path。",
              {"suite_id": str, "step_id": str, "verdict": str,
               "observed_effect": str, "evidence_paths": list[str]})
        async def record_test_step(args):
            try:
                return _text(await self.record(args))
            except Exception as exc:
                return _error(exc)

        @tool("finish_test_suite", "兼容性收口：结束已无待记录步骤的套件；最后一次 record 已自动完成时幂等返回同一结果。",
              {"suite_id": str})
        async def finish_test_suite(args):
            try:
                return _text(await self.finish(args))
            except Exception as exc:
                return _error(exc)

        tools_list = [open_test_suite, next_test_step, record_test_step, finish_test_suite]
        return create_sdk_mcp_server(name="test_excel", version="0.1.0", tools=tools_list), [
            f"mcp__test_excel__{item.name}" for item in tools_list
        ]
