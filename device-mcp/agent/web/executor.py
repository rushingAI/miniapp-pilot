"""每个 active Run 一个独立 spawn 子进程；Windows 与 macOS 使用同一边界。"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import queue
import time
import uuid

try:
    from telemetry import PhaseTimer, environment_fingerprint, utc_ts_ms
except ModuleNotFoundError:
    from agent.telemetry import PhaseTimer, environment_fingerprint, utc_ts_ms
try:
    from silence import SilenceObserver
except ModuleNotFoundError:
    from agent.web.silence import SilenceObserver
try:
    from model_request import TurnStopped, turn_stopped_event
except ModuleNotFoundError:
    from agent.model_request import TurnStopped, turn_stopped_event
try:
    import events as event_factory
except ModuleNotFoundError:
    from agent.web import events as event_factory


_SDK_STOP_GRACE_SECONDS = 5.0
_DEVICE_ATOMIC_STOP_GRACE_SECONDS = 2.0
_STOP_HARD_LIMIT_SECONDS = 6.0


class _ChildControl:
    def __init__(self):
        self.stopped = False
        self.stop_reason = ""
        self.control_seq = 0
        self.control_lock = asyncio.Lock()
        self.interrupt_hook = None


def _queue_get(channel, timeout=0.2):
    try:
        return channel.get(timeout=timeout)
    except queue.Empty:
        return None


def _process_diagnostics(process) -> tuple[bool | None, int | None]:
    if process is None:
        return None, None
    try:
        alive = bool(process.is_alive())
    except Exception:
        alive = None
    try:
        exitcode = process.exitcode
    except Exception:
        exitcode = None
    if not isinstance(exitcode, int) or isinstance(exitcode, bool):
        exitcode = None
    return alive, exitcode


async def _interactive_child(config: dict, channel, commands):
    """持久 interactive executor：一个子进程内保留多轮 SDK 会话和设备上下文。"""
    control = _ChildControl()
    active_turn_id = ""
    turn_chat_done = False
    current_turn = None
    human_future = None
    permission_future = None
    shutting_down = False
    phase_path = os.path.join(config["attempt_dir"], "trace", "interactive", "phase_timing.jsonl")
    session_timing = PhaseTimer(
        phase_path,
        {"scope": "interactive_session"},
        origin_ns=config.get("executor_queued_perf_ns"),
        origin_utc=config.get("executor_queued_utc", ""),
    )
    session_timing.mark("executor_received", pid=os.getpid())

    async def emit(event):
        nonlocal turn_chat_done
        payload = dict(event or {})
        if payload.get("type") == "chat_done" and active_turn_id:
            turn_chat_done = True
        if active_turn_id and not payload.get("turn_id"):
            payload["turn_id"] = active_turn_id
        channel.put({"kind": "event", "event": payload})

    async def ask_human(question):
        nonlocal human_future
        human_future = asyncio.get_running_loop().create_future()
        await emit({"type": "need_human", "question": question})
        try:
            return await human_future
        finally:
            human_future = None

    async def ask_permission(request):
        nonlocal permission_future
        permission_future = asyncio.get_running_loop().create_future()
        await emit({"type": "tool_permission", **request})
        try:
            return bool(await permission_future)
        finally:
            permission_future = None

    session = None
    server = None
    tools = None
    try:
        if not config.get("probe"):
            import server as device_server
            import tools as device_tools
            from run_store import RunCoordinator
            server = device_server
            tools = device_tools
            serial = config["serial"]
            server.configure_device(serial)
            tools.CONTROL = control
            tools.set_trace_dir(os.path.join(config["attempt_dir"], "trace", "interactive"))

            async def forward_nested(event):
                payload = dict(event or {})
                if payload.get("run_id"):
                    payload["nested_run_id"] = payload["run_id"]
                await emit(payload)

            fixed_device = dict(config["device"])
            nested_coordinator = RunCoordinator(
                config["runs_root"], forward_nested, lambda: fixed_device, recover=False,
                borrowed_lease=config.get("parent_device_lease"),
            )
            tools.RUN_CONTEXT = {
                "uploads_dir": config["uploads_dir"],
                "out_base": config["attempt_dir"],
                "emit": emit,
                "control": control,
                "provider_env": config.get("provider_env"),
                "run_coordinator": nested_coordinator,
                "model": config.get("model", ""),
                "effort": config.get("effort", "low"),
                "key_fingerprint": config.get("key_fingerprint", ""),
                "parent_run_id": config.get("parent_run_id", ""),
            }
            from harness_session import HarnessSession
            session = HarnessSession(
                emit, attempt_dir=config["attempt_dir"], uploads_dir=config["uploads_dir"],
                provider_env=config.get("provider_env") or {},
                effort=config.get("effort", "low"), coordinator=nested_coordinator,
                parent_run_id=config.get("parent_run_id", ""), control=control,
                ask_user=ask_human, ask_permission=ask_permission,
            )
            tools.RUN_CONTEXT["suite_active"] = lambda: bool(
                session.test_excel.active_suite_id
            )
            session_timing.mark("sdk_connect_started")
            await session.connect()
            session_timing.mark("sdk_connect_completed")
        channel.put({"kind": "ready", "pid": os.getpid()})

        async def run_turn(command):
            nonlocal active_turn_id, turn_chat_done
            active_turn_id = command["turn_id"]
            turn_chat_done = False
            control.stopped = False
            control.stop_reason = ""
            stop_emitted = False
            turn_timing = PhaseTimer(
                phase_path,
                {"scope": "interactive_turn", "turn_id": active_turn_id},
                origin_ns=command.get("server_received_perf_ns"),
                origin_utc=command.get("server_received_utc", ""),
            )
            if command.get("server_received_perf_ns"):
                turn_timing.mark_at(
                    "server_received", command["server_received_perf_ns"],
                    command.get("server_received_utc", ""),
                )
            if command.get("queued_perf_ns"):
                turn_timing.mark_at(
                    "queued", command["queued_perf_ns"], command.get("queued_utc", ""),
                )
            turn_timing.mark("executor_received")
            try:
                if config.get("probe"):
                    turn_timing.mark("sdk_query_started")
                    await emit({"type": "chat_delta", "text": f"echo:{command.get('text', '')}"})
                    turn_timing.mark("first_sdk_message", message_type="probe")
                    turn_timing.mark("first_assistant_content", model="probe")
                    turn_timing.mark("result_received", model="probe", service_tier="local")
                    await emit({"type": "chat_done"})
                else:
                    tools.RUN_CONTEXT["effort"] = command.get("effort", "low")
                    tools.RUN_CONTEXT["turn_id"] = active_turn_id
                    await session.change_effort(command.get("effort", "low"))
                    prefix = await session.prepare_turn(command.get("turn_mode", ""))
                    await session.send(
                        prefix + command.get("text", ""), file=command.get("file"),
                        phase_timer=turn_timing,
                    )
            except TurnStopped as exc:
                control.stopped = True
                control.stop_reason = exc.reason
                await emit(turn_stopped_event(
                    exc.request_id, "native_harness",
                    {**exc.failure, "reason": exc.reason},
                    turn_id=active_turn_id,
                ))
                stop_emitted = True
            except BaseException as exc:
                if not control.stopped:
                    control.stopped = True
                    control.stop_reason = "sdk_error"
                    await emit(turn_stopped_event(
                        "", "native_harness",
                        {"reason": "sdk_error", "signal": "exception"},
                        turn_id=active_turn_id, error_type=type(exc).__name__,
                    ))
                    stop_emitted = True
            finally:
                if control.stopped and not stop_emitted:
                    # User Stop may not pass through a model exception; emit the same final contract.
                    await emit(turn_stopped_event(
                        "", "native_harness",
                        {"reason": control.stop_reason or "user_stop", "signal": "control"},
                        turn_id=active_turn_id,
                    ))
                if not turn_chat_done:
                    await emit({"type": "chat_done"})
                channel.put({"kind": "turn_done", "turn_id": active_turn_id})
                active_turn_id = ""

        while not shutting_down:
            command = await asyncio.to_thread(_queue_get, commands)
            if command is None:
                continue
            if isinstance(command, str):
                command = {"action": command}
            action = command.get("action")
            if action == "send":
                if current_turn and not current_turn.done():
                    channel.put({"kind": "turn_error", "turn_id": command.get("turn_id", ""),
                                 "message": "上一轮仍在执行"})
                else:
                    current_turn = asyncio.create_task(run_turn(command))
            elif action == "device_binding" and server:
                serial = str(command.get("serial") or "").strip()
                if serial:
                    server.invalidate_device_session(serial)
                else:
                    server.clear_device()
            elif action == "append_input":
                text = str(command.get("text") or "").strip()
                if text and session:
                        try:
                            await session.append_input(dict(command))
                        except TurnStopped as exc:
                            async with control.control_lock:
                                control.stopped = True
                                control.stop_reason = exc.reason
                                control.control_seq += 1
                            await session.stop()
            elif action == "reconfigure_provider":
                request_id = str(command.get("request_id") or "")
                try:
                    await session.reconfigure_provider(
                        command.get("provider_env") or {},
                        str(command.get("model") or ""),
                        str(command.get("effort") or ""),
                    )
                    tools.RUN_CONTEXT["provider_env"] = dict(command.get("provider_env") or {})
                    tools.RUN_CONTEXT["model"] = str(command.get("model") or "")
                    if command.get("effort"):
                        tools.RUN_CONTEXT["effort"] = str(command.get("effort"))
                    channel.put({"kind": "reconfigured", "request_id": request_id})
                except BaseException as exc:
                    channel.put({
                        "kind": "reconfigure_error", "request_id": request_id,
                        "message": str(exc)[:500], "error_type": type(exc).__name__,
                    })
            elif action == "respond":
                if human_future and not human_future.done():
                    human_future.set_result(command.get("answer", ""))
            elif action == "respond_permission":
                if permission_future and not permission_future.done():
                    permission_future.set_result(bool(command.get("allow")))
            elif action == "stop":
                stop_started = asyncio.get_running_loop().time()
                async with control.control_lock:
                    control.stopped = True
                    control.stop_reason = str(command.get("reason") or "user_stop")
                    control.control_seq += 1
                if human_future and not human_future.done():
                    human_future.set_result("停止")
                if permission_future and not permission_future.done():
                    permission_future.set_result(False)
                if session:
                    stop_task = asyncio.create_task(session.stop())
                    if current_turn and not current_turn.done():
                        current_grace = (
                            _DEVICE_ATOMIC_STOP_GRACE_SECONDS
                            if getattr(session, "_operator_client", None)
                            else _SDK_STOP_GRACE_SECONDS
                        )
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(current_turn), timeout=current_grace,
                            )
                        except asyncio.TimeoutError:
                            current_turn.cancel()
                            remaining = max(
                                0.0,
                                _STOP_HARD_LIMIT_SECONDS
                                - (asyncio.get_running_loop().time() - stop_started),
                            )
                            try:
                                await asyncio.wait_for(
                                    asyncio.shield(current_turn), timeout=min(0.5, remaining),
                                )
                            except (asyncio.TimeoutError, asyncio.CancelledError):
                                pass
                    remaining = max(
                        0.0,
                        _STOP_HARD_LIMIT_SECONDS
                        - (asyncio.get_running_loop().time() - stop_started),
                    )
                    try:
                        await asyncio.wait_for(stop_task, timeout=remaining)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        stop_task.cancel()
            elif action == "shutdown":
                shutting_down = True
                async with control.control_lock:
                    control.stopped = True
                    control.stop_reason = str(command.get("reason") or "executor_failed")
                    control.control_seq += 1
                if human_future and not human_future.done():
                    human_future.set_result("停止")
                if permission_future and not permission_future.done():
                    permission_future.set_result(False)
                # An idle/stopped turn has already been interrupted and drained. Calling
                # stop() again here would emit a second turn_interrupted without a turn_id
                # immediately before close() finalizes the active Suite.
                if session and current_turn and not current_turn.done():
                    await session.stop()

        if current_turn and not current_turn.done():
            try:
                await asyncio.wait_for(current_turn, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                current_turn.cancel()
        if session:
            await session.close()
    except BaseException as exc:
        if not (shutting_down and isinstance(exc, asyncio.CancelledError)):
            channel.put({"kind": "fatal", "error_type": type(exc).__name__,
                         "message": str(exc)[:500]})
    finally:
        if server:
            server.clear_device()
        channel.put({"kind": "closed"})


def _worker_main(config: dict, events, commands):
    if config.get("kind") == "probe":
        events.put({"kind": "result", "summary": {
            "pid": os.getpid(), "serial": config.get("serial", ""),
        }})
        events.put({"kind": "done"})
        return
    asyncio.run(_interactive_child(config, events, commands))


class InteractiveExecutorManager:
    """主进程 supervisor：持久子进程、多轮 turn、durable Run 与浏览器无关生命周期。"""

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.handle = None
        self.process = None
        self.events = None
        self.commands = None
        self._pump_task = None
        self._ready = None
        self._turns: dict[str, asyncio.Future] = {}
        self._turn_state = "idle"
        self._stopping = False
        self._stop_reason = ""
        self._control_lock = asyncio.Lock()
        self._input_seq = 0
        self._seen_input_ids: set[str] = set()
        self._reconfigure_waiters: dict[str, asyncio.Future] = {}
        self._suite_running = False
        self._parent_turn_active = False
        self._active_suite: dict = {}
        self._current_turn_id = ""
        self._closing = False
        self._human_waiting = False
        self._permission_waiting = False
        self.pid = 0
        self._silence = None
        self.selected_effort = ""
        self.session_effort = ""

    @property
    def busy(self) -> bool:
        return bool(
            self._parent_turn_active
            or self._stopping
            or (self._turn_state == "running" and self._current_turn_id)
        )

    @property
    def turn_state(self) -> str:
        if self._turn_state == "stopped":
            return "stopped"
        if self.busy:
            return "running"
        return "idle" if self._turn_state == "running" else self._turn_state

    @property
    def stop_reason(self) -> str:
        return self._stop_reason

    @property
    def active(self) -> bool:
        return self.handle is not None

    @property
    def suite_running(self) -> bool:
        return self._suite_running

    @property
    def active_suite(self) -> dict:
        return dict(self._active_suite)

    @property
    def mutation_locked(self) -> bool:
        return bool(self.busy or self._suite_running or self._human_waiting
                    or self._permission_waiting or self._closing)

    @property
    def run_id(self) -> str:
        return self.handle.run_id if self.handle else ""

    def events_after(self, after_seq: int = 0) -> list[dict]:
        if not self.handle:
            return []
        return self.coordinator.store.read_events(self.handle.record, after_seq)

    async def _persist(self, event: dict):
        if self._silence and event.get("type") != "silence_warning":
            self._silence.touch()
        if self.handle:
            return await self.handle.emit(event)
        return event

    async def record_device_link(self, previous: dict | None, current: dict):
        """Persist device transitions and keep the persistent child binding current."""
        event = None
        if self.handle:
            event = await self._persist(event_factory.device_link(previous, current))
        previous_serial = (
            str((previous or {}).get("selected_serial") or "")
            if (previous or {}).get("status") == "ready" else ""
        )
        current_serial = (
            str(current.get("selected_serial") or "")
            if current.get("status") == "ready" else ""
        )
        if self.commands is not None and previous_serial != current_serial:
            # Keep the selected serial while it is temporarily missing. Clearing it would
            # let server._serial() auto-select a different attached phone inside the child.
            binding_serial = current_serial or previous_serial
            self.commands.put({"action": "device_binding", "serial": binding_serial})
        return event

    async def _pump(self):
        try:
            while True:
                message = await asyncio.to_thread(_queue_get, self.events)
                if message is None:
                    if self.process and not self.process.is_alive():
                        exc = RuntimeError("interactive executor process exited")
                        exc.failure_class = "child_process_exit"
                        raise exc
                    continue
                kind = message.get("kind")
                if kind == "event":
                    event = message.get("event") or {}
                    if event.get("type") == "suite_start":
                        self._suite_running = True
                        if self._turn_state != "stopped":
                            self._turn_state = "running"
                        self._active_suite = {
                            "suite_id": event.get("suite_id", ""),
                            "run_id": event.get("nested_run_id") or event.get("run_id", ""),
                            "current_step": None,
                        }
                    elif event.get("type") in {"setup_step", "case_start"}:
                        self._active_suite["current_step"] = {
                            key: event.get(key) for key in
                            ("step_id", "kind", "index", "seq", "operation", "observe")
                            if event.get(key) is not None
                        }
                    elif event.get("type") in {"setup_result", "case_result"}:
                        self._active_suite["current_step"] = None
                    elif event.get("type") in {"turn_interrupted", "suite_interrupted", "suite_resumed"}:
                        if event.get("type") in {"turn_interrupted", "suite_interrupted"}:
                            self._suite_running = False
                        if event.get("suite_id"):
                            self._active_suite["suite_id"] = event.get("suite_id")
                        if event.get("current_step") is not None:
                            self._active_suite["current_step"] = event.get("current_step")
                    elif event.get("type") == "suite_done":
                        self._suite_running = False
                        self._active_suite = {}
                        if not self._parent_turn_active and self._turn_state == "running":
                            self._turn_state = "idle"
                    if event.get("type") == "need_human":
                        self._human_waiting = True
                    if event.get("type") == "turn_stopped":
                        if self._turn_state == "stopped" and self._stop_reason:
                            continue
                        event.setdefault("turn_id", self._current_turn_id)
                        event.setdefault("suite_id", self._active_suite.get("suite_id", ""))
                        event.setdefault("current_step", self._active_suite.get("current_step"))
                        self._turn_state = "stopped"
                        self._suite_running = False
                        self._stopping = False
                        self._stop_reason = str(event.get("reason") or "sdk_error")
                    if event.get("type") == "tool_permission":
                        self._permission_waiting = True
                    if event.get("type") == "effort_changed":
                        self.session_effort = str(event.get("effort") or self.session_effort)
                    await self._persist(event)
                    if event.get("type") in {
                            "suite_start", "turn_interrupted", "suite_interrupted", "suite_done"}:
                        await self._persist({
                            "type": "interactive_status", "busy": self.busy,
                            "turn_state": self.turn_state, "stop_reason": self._stop_reason,
                            "mutation_locked": self.mutation_locked,
                            "active_suite": self.active_suite,
                        })
                elif kind == "ready":
                    self.pid = int(message.get("pid") or 0)
                    if self._ready and not self._ready.done():
                        self._ready.set_result(message)
                elif kind == "turn_done":
                    self._parent_turn_active = False
                    turn_id = message.get("turn_id", "")
                    future = self._turns.pop(turn_id, None)
                    if future and not future.done():
                        future.set_result(True)
                    if self._turn_state == "running":
                        self._turn_state = "idle"
                    self._stopping = False
                    self._current_turn_id = ""
                    self._human_waiting = False
                    self._permission_waiting = False
                    await self._persist({
                        "type": "interactive_status", "busy": self.busy,
                        "turn_state": self.turn_state, "stop_reason": self._stop_reason,
                        "mutation_locked": self.mutation_locked,
                        "active_suite": self.active_suite,
                    })
                elif kind == "turn_error":
                    turn_id = message.get("turn_id", "")
                    future = self._turns.pop(turn_id, None)
                    if future and not future.done():
                        future.set_exception(RuntimeError(message.get("message", "turn error")))
                    self._turn_state = "stopped"
                    self._parent_turn_active = False
                    self._stopping = False
                    self._stop_reason = "executor_failed"
                    self._current_turn_id = ""
                    self._human_waiting = False
                    self._permission_waiting = False
                    await self._persist({
                        "type": "interactive_status", "busy": False,
                        "turn_state": self._turn_state, "stop_reason": self._stop_reason,
                        "mutation_locked": self.mutation_locked,
                        "active_suite": self.active_suite,
                    })
                elif kind in {"reconfigured", "reconfigure_error"}:
                    request_id = str(message.get("request_id") or "")
                    waiter = self._reconfigure_waiters.pop(request_id, None)
                    if waiter and not waiter.done():
                        if kind == "reconfigured":
                            waiter.set_result(True)
                        else:
                            waiter.set_exception(RuntimeError(message.get("message", "重连失败")))
                elif kind == "fatal":
                    exc = RuntimeError(str(message.get("message") or "interactive executor fatal"))
                    exc.failure_class = "worker_fatal"
                    exc.child_error_type = str(
                        message.get("error_type") or "InteractiveExecutorError"
                    )
                    raise exc
                elif kind == "closed":
                    return
        except asyncio.CancelledError:
            # 事件泵受控取消不是 executor 崩溃；正常 close 写 completed，
            # 进程异常消失则由下次启动的 recover_incomplete 写 interrupted。
            raise
        except Exception as exc:
            if self._ready and not self._ready.done():
                self._ready.set_exception(exc)
            for future in self._turns.values():
                if not future.done():
                    future.set_exception(exc)
            self._turns.clear()
            self._turn_state = "stopped"
            self._stopping = False
            self._stop_reason = "executor_failed"
            self._suite_running = False
            self._active_suite = {}
            self._current_turn_id = ""
            self._human_waiting = False
            self._permission_waiting = False
            if self.handle and not self._closing:
                process_alive, process_exitcode = _process_diagnostics(self.process)
                await self._persist(event_factory.executor_failure(
                    exc,
                    source="event_pump",
                    failure_class=str(
                        getattr(exc, "failure_class", "event_pump_exception")
                    ),
                    child_error_type=str(getattr(exc, "child_error_type", "")),
                    process_alive=process_alive,
                    process_exitcode=process_exitcode,
                ))
                self.coordinator.store.finish_running_children(
                    self.handle.run_id, "interrupted", {"reason": "parent_executor_failed"}
                )
                await self._persist(turn_stopped_event(
                    "", "executor", {"reason": "executor_failed", "signal": "exception"},
                    error_type=type(exc).__name__,
                ))
                await self._persist({"type": "chat_done"})
                self.handle.finish("failed")
                self.handle = None

    async def _spawn(self, config: dict):
        context = multiprocessing.get_context("spawn")
        self.events = context.Queue()
        self.commands = context.Queue()
        worker_config = {
            **config,
            "kind": "interactive",
            "executor_queued_perf_ns": time.perf_counter_ns(),
            "executor_queued_utc": utc_ts_ms(),
        }
        self.process = context.Process(
            target=_worker_main, args=(worker_config, self.events, self.commands), daemon=False,
        )
        self._ready = asyncio.get_running_loop().create_future()
        self.process.start()
        self._pump_task = asyncio.create_task(self._pump())
        await asyncio.wait_for(asyncio.shield(self._ready), timeout=45.0)

    async def _execute_turn(self, config: dict, command: dict, future: asyncio.Future):
        observer = SilenceObserver(
            self._persist, waiting=lambda: self._human_waiting or self._permission_waiting,
        )
        self._silence = observer
        observer_task = asyncio.create_task(observer.watch())
        try:
            if not self.process or not self.process.is_alive():
                await self._spawn(config)
            self.commands.put(command)
            await future
        except asyncio.CancelledError:
            # HTTP/FastAPI 收尾时取消后台 task 不应把正常关闭改写为 failed。
            raise
        except Exception as exc:
            if self._closing:
                # The child transport may report a final error while controlled shutdown is
                # disconnecting the SDK. It is not an executor crash and must not win the Run race.
                raise asyncio.CancelledError() from exc
            self._turn_state = "stopped"
            self._stop_reason = "executor_failed"
            self._turns.pop(command["turn_id"], None)
            process_alive, process_exitcode = _process_diagnostics(self.process)
            await self._persist(event_factory.executor_failure(
                exc,
                source="turn_execution",
                failure_class="turn_exception",
                process_alive=process_alive,
                process_exitcode=process_exitcode,
            ))
            await self._persist(turn_stopped_event(
                "", "native_harness",
                {"reason": "executor_failed", "signal": "exception"},
                error_type=type(exc).__name__, turn_id=command["turn_id"],
            ))
            await self._persist({"type": "chat_done", "turn_id": command["turn_id"]})
            if self.handle:
                self.coordinator.store.finish_running_children(
                    self.handle.run_id, "interrupted", {"reason": "parent_turn_failed"}
                )
                self.handle.finish("failed")
                self.handle = None
            raise
        finally:
            observer.close()
            observer_task.cancel()
            try:
                await observer_task
            except asyncio.CancelledError:
                pass
            finally:
                if self._silence is observer:
                    self._silence = None

    async def submit(self, config: dict, text: str, file=None, effort: str = "low",
                     client_message_id: str = "", received_perf_ns: int | None = None,
                     received_utc: str = "", turn_mode: str = "") -> asyncio.Task:
        if self.busy:
            raise RuntimeError("上一条还在执行，请先停止或等待完成")
        if self._suite_running and self.session_effort and effort != self.session_effort:
            raise RuntimeError("套件尚未结束，不能在此时切换 effort")
        self.selected_effort = effort
        if not self.handle:
            self.handle = self.coordinator.begin({
                "source": "interactive", "model": config.get("model", ""), "effort": effort,
                "architecture": "native_harness",
            }, environment=environment_fingerprint(
                config.get("provider_env"), config.get("key_fingerprint", ""),
            ))
            self.session_effort = effort
        config = {
            **config,
            "attempt_dir": self.handle.attempt_dir,
            "runs_root": self.coordinator.store.root,
            "parent_run_id": self.handle.run_id,
            "parent_device_lease": {
                "token": self.handle.lease.token,
                "serial": self.handle.lease.serial,
                "run_id": self.handle.lease.run_id,
                "acquired_at": self.handle.lease.acquired_at,
            } if self.handle.lease else None,
        }
        turn_id = str(uuid.uuid4())
        message_id = client_message_id or str(uuid.uuid4())
        if turn_mode:
            await self._persist({
                "type": "turn_mode_selected", "mode": turn_mode,
                "active_suite": self.active_suite, "turn_id": turn_id,
            })
        await self._persist({
            "type": "user_message", "text": text, "file": file,
            "client_message_id": message_id, "turn_id": turn_id,
        })
        self._turn_state = "running"
        self._parent_turn_active = True
        self._stopping = False
        self._stop_reason = ""
        self._current_turn_id = turn_id
        future = asyncio.get_running_loop().create_future()
        self._turns[turn_id] = future
        server_received_perf_ns = int(received_perf_ns or time.perf_counter_ns())
        server_received_utc = received_utc or utc_ts_ms()
        command = {"action": "send", "turn_id": turn_id, "text": text,
                   "file": file, "effort": effort,
                   "turn_mode": turn_mode,
                   "server_received_perf_ns": server_received_perf_ns,
                   "server_received_utc": server_received_utc,
                   "queued_perf_ns": time.perf_counter_ns(),
                   "queued_utc": utc_ts_ms()}
        task = asyncio.create_task(self._execute_turn(config, command, future))
        return task

    async def reconfigure_provider(self, provider_env: dict, model: str = "",
                                   effort: str = "") -> bool:
        if self.busy:
            return False
        if not self.process or not self.process.is_alive() or not self.commands:
            self.selected_effort = effort or self.selected_effort
            self.session_effort = effort or self.session_effort
            return True
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self._reconfigure_waiters[request_id] = future
        self.commands.put({
            "action": "reconfigure_provider", "request_id": request_id,
            "provider_env": dict(provider_env or {}), "model": model, "effort": effort,
        })
        try:
            await asyncio.wait_for(future, timeout=45.0)
        finally:
            self._reconfigure_waiters.pop(request_id, None)
        self.selected_effort = effort or self.selected_effort
        self.session_effort = effort or self.session_effort
        return True

    async def append_input(self, text: str, client_message_id: str = "") -> bool:
        text = str(text or "").strip()
        if not text or not self.busy or self._stopping or not self.commands:
            return False
        message_id = client_message_id or str(uuid.uuid4())
        async with self._control_lock:
            if not self.busy or self._stopping or message_id in self._seen_input_ids:
                return message_id in self._seen_input_ids
            self._seen_input_ids.add(message_id)
            self._input_seq += 1
            step = self._active_suite.get("current_step") or {}
            command = {
                "action": "append_input", "text": text,
                "client_message_id": message_id, "input_seq": self._input_seq,
                "turn_id": self._current_turn_id,
                "suite_id": self._active_suite.get("suite_id", ""),
                "step_id": step.get("step_id", ""),
            }
            await self._persist({
                "type": "turn_input", "status": "accepted", "text": text,
                **{key: command[key] for key in (
                    "client_message_id", "input_seq", "turn_id", "suite_id", "step_id"
                )},
            })
            self.commands.put(command)
            return True

    async def control(self, action: str, reason: str = "user_stop") -> bool:
        if action != "stop" or not self.commands:
            return False
        async with self._control_lock:
            if self._stopping:
                return True
            if self._turn_state == "stopped" and self._current_turn_id:
                return True
            if not self.busy:
                return False
            self._stopping = True
            self._stop_reason = str(reason or "user_stop")
            await self._persist({
                "type": "control_requested", "action": "stop", "scope": "turn",
                "turn_id": self._current_turn_id, "suite_running": self._suite_running,
                "reason": self._stop_reason,
            })
            self.commands.put({"action": "stop", "reason": self._stop_reason})
            if action == "stop":
                was_waiting = self._human_waiting
                was_permission = self._permission_waiting
                self._human_waiting = False
                self._permission_waiting = False
                if was_waiting:
                    await self._persist({
                        "type": "human_resolved", "reason": "stopped",
                        "turn_id": self._current_turn_id,
                    })
                if was_permission:
                    await self._persist({
                        "type": "permission_resolved", "reason": "stopped",
                        "turn_id": self._current_turn_id,
                    })
            return True

    async def respond(self, answer: str) -> bool:
        if not self.commands or not self.busy or not self._human_waiting:
            return False
        self.commands.put({"action": "respond", "answer": answer})
        self._human_waiting = False
        # 只落“已处理”状态，不记录人工回答内容；刷新回放时据此关闭旧弹窗。
        await self._persist({
            "type": "human_resolved", "reason": "answered",
            "turn_id": self._current_turn_id,
        })
        return True

    async def respond_permission(self, allow: bool) -> bool:
        if not self.commands or not self.busy or not self._permission_waiting:
            return False
        self.commands.put({"action": "respond_permission", "allow": bool(allow)})
        self._permission_waiting = False
        await self._persist({
            "type": "permission_resolved", "reason": "allowed" if allow else "denied",
            "turn_id": self._current_turn_id,
        })
        return True

    async def close(self):
        """受控关闭父对话 Run；未完成 Child Suite 独立收敛为 interrupted。"""
        self._closing = True
        parent_run_id = self.handle.run_id if self.handle else ""
        try:
            if self.commands:
                self.commands.put({"action": "shutdown"})
            if self.process:
                await asyncio.to_thread(self.process.join, 5.0)
                if self.process.is_alive():
                    self.process.terminate()
                    await asyncio.to_thread(self.process.join, 2.0)
            if self._pump_task and not self._pump_task.done():
                try:
                    await asyncio.wait_for(self._pump_task, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    self._pump_task.cancel()
            if self.handle:
                self.coordinator.store.finish_running_children(
                    parent_run_id, "interrupted", {"reason": "parent_session_closed"}
                )
                self.handle.finish("completed")
        finally:
            self.handle = None
            self.process = None
            self.pid = 0
            self._turn_state = "idle"
            self._parent_turn_active = False
            self._stopping = False
            self._stop_reason = ""
            self._suite_running = False
            self._active_suite = {}
            self._current_turn_id = ""
            self._human_waiting = False
            self._permission_waiting = False
            self._input_seq = 0
            self._seen_input_ids.clear()
            self.selected_effort = ""
            self.session_effort = ""
            if self.events:
                self.events.close()
            if self.commands:
                self.commands.close()
            self.events = None
            self.commands = None
            self._closing = False
