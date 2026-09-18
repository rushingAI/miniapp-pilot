"""Per-model-request diagnostics and fatal runtime-stop classification."""

from __future__ import annotations

import json
import uuid
import re


_FAILURE_FAMILIES = (
    ("no_healthy_deployment", (
        "no healthy deployment", "no available deployment",
        "all deployments are unhealthy", "无可用模型部署", "无健康模型节点",
    )),
    ("provider_overloaded", (
        "overloaded_error", "overloaded", "capacity exceeded", "server is busy",
        "模型服务繁忙", "模型服务过载",
    )),
    ("provider_unavailable", (
        "service unavailable", "temporarily unavailable", "upstream unavailable",
        "no healthy upstream", "upstream connect error", "模型服务暂时不可用",
        "上游服务不可用",
    )),
    ("provider_rate_limited", (
        "rate_limit_error", "rate limit exceeded", "too many requests", "模型服务限流",
    )),
    ("provider_gateway_error", (
        "bad gateway", "gateway timeout",
    )),
)

_STATUS_REASONS = {
    400: "provider_request_error",
    401: "provider_auth_error",
    403: "provider_auth_error",
    404: "provider_request_error",
    408: "provider_request_error",
    429: "provider_rate_limited",
    500: "provider_internal_error",
    502: "provider_gateway_error",
    503: "provider_unavailable",
    504: "provider_gateway_error",
    529: "provider_overloaded",
}
_TRUSTED_ERROR_PREFIX = re.compile(
    r"^\s*(?:api\s+error|provider\s+error|upstream\s+error|litellm\.[a-z]+error)\s*:",
    re.IGNORECASE,
)
_HTTP_STATUS_TEXT = re.compile(r"\b(?:api\s+error|http)\s*[:=]?\s*([45]\d\d)\b", re.IGNORECASE)
_REASON_LABELS = {
    "user_stop": "用户已停止",
    "no_healthy_deployment": "没有健康的模型部署",
    "provider_overloaded": "模型服务过载",
    "provider_unavailable": "模型服务暂时不可用",
    "provider_rate_limited": "模型服务限流",
    "provider_gateway_error": "模型网关异常",
    "provider_internal_error": "模型服务内部错误",
    "provider_auth_error": "模型服务认证失败",
    "provider_request_error": "模型请求无效",
    "invalid_model": "模型名称或模型配置无效",
    "sdk_error": "模型 SDK 执行异常",
    "device_unavailable": "测试设备不可用",
    "executor_failed": "执行器异常退出",
}

_INVALID_MODEL_MARKERS = (
    "invalid model", "model not found", "unknown model", "unsupported model",
    "模型不存在", "模型名称无效", "不支持的模型",
)
_AUTH_MARKERS = (
    "unauthorized", "forbidden", "invalid api key", "authentication",
    "authentication_error", "api key invalid", "鉴权失败", "认证失败", "密钥无效",
)
_DEVICE_MARKERS = (
    "no devices/emulators found", "device offline", "device not found",
    "device unavailable", "adb server is out of date", "设备已断开",
    "设备不可用", "未连接设备",
)

_STOP_REASON_MAP = {
    "no_healthy_deployment": "provider_unavailable",
    "provider_overloaded": "provider_unavailable",
    "provider_unavailable": "provider_unavailable",
    "provider_rate_limited": "provider_unavailable",
    "provider_gateway_error": "provider_unavailable",
    "provider_internal_error": "provider_unavailable",
    "provider_auth_error": "provider_auth_error",
    "provider_request_error": "provider_request_error",
    "invalid_model": "invalid_model",
    "sdk_error": "sdk_error",
    "device_unavailable": "device_unavailable",
    "executor_failed": "executor_failed",
}
_USAGE_KEYS = (
    "input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


class TurnStopped(RuntimeError):
    """A fatal runtime condition that stops only the current Agent turn."""

    def __init__(self, reason: str, request_id: str = "", failure: dict | None = None):
        self.reason = str(reason or "sdk_error")
        self.request_id = str(request_id or "")
        self.failure = dict(failure or {})
        label = _REASON_LABELS.get(self.reason, "当前执行发生错误")
        super().__init__(f"{label}，当前执行已停止")


def _status_from(value: object, explicit_status=None) -> int | None:
    candidates = [explicit_status]
    for name in ("api_error_status", "status_code", "status"):
        candidates.append(getattr(value, name, None))
    response = getattr(value, "response", None)
    if response is not None:
        candidates.append(getattr(response, "status_code", None))
    for candidate in candidates:
        try:
            status = int(candidate)
        except (TypeError, ValueError):
            continue
        if 100 <= status <= 599:
            return status
    match = _HTTP_STATUS_TEXT.search(str(value or ""))
    if match:
        return int(match.group(1))
    return None


def _error_text(value: object, errors=None) -> str:
    parts = []
    if value not in (None, ""):
        parts.append(str(value))
    for item in errors or []:
        if item not in (None, ""):
            parts.append(str(item))
    return "\n".join(parts).lower()


def classify_provider_failure(value: object = None, *, api_error_status=None,
                              is_error: bool = False, errors=None,
                              trusted_error: bool = False, _seen=None) -> dict | None:
    """Classify only trusted provider failures; ordinary assistant prose is never a signal."""
    seen = _seen if _seen is not None else set()
    if value is not None and not isinstance(value, (str, bytes, int, float, bool)):
        identity = id(value)
        if identity in seen:
            return None
        seen.add(identity)
        nested_values = list(getattr(value, "exceptions", None) or [])
        for name in ("__cause__", "__context__"):
            nested = getattr(value, name, None)
            if nested is not None:
                nested_values.append(nested)
        for nested in nested_values:
            failure = classify_provider_failure(
                nested, is_error=True, trusted_error=True, _seen=seen,
            )
            if failure:
                return failure
    status = _status_from(value, api_error_status)
    text = _error_text(value, errors)
    trusted = bool(is_error or trusted_error or _TRUSTED_ERROR_PREFIX.match(text))
    if not trusted:
        return None
    for reason, markers in _FAILURE_FAMILIES:
        if any(marker in text for marker in markers):
            return {"reason": reason, "signal": "error_family", "http_status": status}
    if status in _STATUS_REASONS:
        return {
            "reason": _STATUS_REASONS[status],
            "signal": "api_error_status", "http_status": status,
        }
    return None


def _normalized_stop_failure(failure: dict) -> dict:
    raw_reason = str(failure.get("reason") or "sdk_error")
    return {
        **failure,
        "provider_reason": raw_reason,
        "reason": _STOP_REASON_MAP.get(raw_reason, "sdk_error"),
    }


def classify_runtime_failure(value: object = None, *, api_error_status=None,
                             is_error: bool = False, errors=None,
                             trusted_error: bool = False, _seen=None) -> dict | None:
    """Map trusted runtime failures to the small public ``stop_reason`` vocabulary.

    Plain Assistant prose is deliberately ignored. Exceptions, SDK Result errors and
    explicitly trusted ToolResult payloads are inspected recursively, including
    ExceptionGroup/TaskGroup wrappers.
    """
    seen = _seen if _seen is not None else set()
    structured = value is not None and not isinstance(value, (str, bytes, int, float, bool))
    if structured:
        identity = id(value)
        if identity in seen:
            return None
        seen.add(identity)
        nested_values = list(getattr(value, "exceptions", None) or [])
        for name in ("__cause__", "__context__"):
            nested = getattr(value, name, None)
            if nested is not None:
                nested_values.append(nested)
        nested_fallback = None
        for nested in nested_values:
            failure = classify_runtime_failure(
                nested, is_error=True, trusted_error=True, _seen=seen,
            )
            if failure and failure.get("reason") != "sdk_error":
                return failure
            nested_fallback = failure or nested_fallback
        if nested_fallback:
            return nested_fallback

    trusted = bool(structured or is_error or trusted_error)
    if not trusted:
        return None
    status = _status_from(value, api_error_status)
    text = _error_text(value, errors)
    if any(marker in text for marker in _INVALID_MODEL_MARKERS):
        return {"reason": "invalid_model", "signal": "error_family", "http_status": status}
    if status in {401, 403} or any(marker in text for marker in _AUTH_MARKERS):
        return {"reason": "provider_auth_error", "signal": "error_family", "http_status": status}
    if any(marker in text for marker in _DEVICE_MARKERS):
        return {"reason": "device_unavailable", "signal": "error_family", "http_status": status}
    provider = classify_provider_failure(
        value, api_error_status=api_error_status, is_error=is_error,
        errors=errors, trusted_error=trusted_error, _seen=set(),
    )
    if provider:
        return _normalized_stop_failure(provider)
    if status in {400, 404, 408, 422}:
        return {"reason": "provider_request_error", "signal": "api_error_status", "http_status": status}
    if structured:
        return {"reason": "sdk_error", "signal": "exception", "http_status": status}
    return None


def classify_provider_message(message) -> dict | None:
    return classify_provider_failure(
        getattr(message, "result", ""),
        api_error_status=getattr(message, "api_error_status", None),
        is_error=bool(getattr(message, "is_error", False)),
        errors=getattr(message, "errors", None),
    )


def classify_runtime_message(message) -> dict | None:
    return classify_runtime_failure(
        getattr(message, "result", ""),
        api_error_status=getattr(message, "api_error_status", None),
        is_error=bool(getattr(message, "is_error", False)),
        errors=getattr(message, "errors", None),
    )


def turn_stopped_event(request_id: str, architecture: str,
                       failure: dict | None = None, **fields) -> dict:
    failure = dict(failure or {})
    reason = str(failure.get("reason") or "sdk_error")
    label = _REASON_LABELS.get(reason, "当前执行已停止")
    status = failure.get("http_status")
    status_text = f"（HTTP {status}）" if status else ""
    if reason == "user_stop":
        next_step = "需要继续时，请发送下一条消息；系统不会自动重放设备动作。"
        default_signal = "control"
    elif reason == "device_unavailable":
        next_step = "设备恢复后，请通过下一条消息选择是否继续当前测试。系统不会自动重放设备动作。"
        default_signal = "device_error"
    else:
        next_step = "修复配置或服务后，请通过下一条消息选择是否继续当前测试。系统不会自动重放设备动作。"
        default_signal = "runtime_error"
    return {
        "type": "turn_stopped",
        "status": "stopped",
        "reason": reason,
        "signal": str(failure.get("signal") or default_signal),
        "http_status": status,
        "request_id": request_id,
        "architecture": architecture,
        "msg": (
            f"{label}{status_text}，当前执行已停止；Suite、当前步骤和设备现场已保留。"
            f"{next_step}"
        ),
        **fields,
    }


def _provider_request_id(message) -> str:
    for name in ("request_id", "uuid", "id"):
        value = getattr(message, name, None)
        if value:
            return str(value)[:160]
    return ""


def safe_usage_summary(usage) -> dict:
    """Keep only numeric token counters; never persist provider payloads."""
    if not isinstance(usage, dict):
        return {}
    summary = {}
    for key in _USAGE_KEYS:
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            summary[key] = value
    return summary


def _json_bytes(value) -> int:
    try:
        return len(json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), default=str,
        ).encode("utf-8"))
    except Exception:
        return 0


def tool_result_metrics(content) -> dict:
    """Structural ToolResult size only; no text, image bytes or business values."""
    metrics = {
        "text_chars": 0, "image_count": 0, "image_encoded_chars": 0,
        "other_block_count": 0,
    }
    if isinstance(content, str):
        metrics["text_chars"] = len(content)
        return metrics
    if not isinstance(content, list):
        if content is not None:
            metrics["other_block_count"] = 1
        return metrics
    for item in content:
        if not isinstance(item, dict):
            metrics["other_block_count"] += 1
            continue
        kind = str(item.get("type") or "")
        if kind == "text":
            metrics["text_chars"] += len(str(item.get("text") or ""))
        elif kind == "image":
            metrics["image_count"] += 1
            encoded = item.get("data")
            source = item.get("source")
            if not encoded and isinstance(source, dict):
                encoded = source.get("data")
            metrics["image_encoded_chars"] += len(str(encoded or ""))
        else:
            metrics["other_block_count"] += 1
    return metrics


def safe_message_metrics(message) -> dict:
    """Message shape, sizes and usage without retaining model or tool contents."""
    metrics = safe_usage_summary(getattr(message, "usage", None))
    model = str(getattr(message, "model", "") or "")[:120]
    if model:
        metrics["model"] = model
    usage = getattr(message, "usage", None)
    if isinstance(usage, dict):
        service_tier = str(usage.get("service_tier") or "")[:40]
        if service_tier:
            metrics["service_tier"] = service_tier
    for source, target in (("stop_reason", "stop_reason"), ("error", "model_error")):
        value = str(getattr(message, source, "") or "")[:80]
        if value:
            metrics[target] = value
    content = getattr(message, "content", None)
    if isinstance(content, str):
        metrics["content_chars"] = len(content)
        return metrics
    if not isinstance(content, list):
        return metrics
    counts = {
        "text_block_count": 0,
        "thinking_block_count": 0,
        "tool_use_block_count": 0,
        "tool_result_block_count": 0,
        "other_block_count": 0,
        "text_chars": 0,
        "thinking_chars": 0,
        "tool_input_bytes": 0,
        "tool_result_text_chars": 0,
        "tool_result_image_count": 0,
        "tool_result_image_encoded_chars": 0,
    }
    for block in content:
        kind = type(block).__name__
        if kind == "TextBlock":
            counts["text_block_count"] += 1
            counts["text_chars"] += len(str(getattr(block, "text", "") or ""))
        elif kind == "ThinkingBlock":
            counts["thinking_block_count"] += 1
            counts["thinking_chars"] += len(str(getattr(block, "thinking", "") or ""))
        elif kind == "ToolUseBlock":
            counts["tool_use_block_count"] += 1
            counts["tool_input_bytes"] += _json_bytes(getattr(block, "input", None))
        elif kind == "ToolResultBlock":
            counts["tool_result_block_count"] += 1
            result = tool_result_metrics(getattr(block, "content", None))
            counts["tool_result_text_chars"] += result["text_chars"]
            counts["tool_result_image_count"] += result["image_count"]
            counts["tool_result_image_encoded_chars"] += result["image_encoded_chars"]
            counts["other_block_count"] += result["other_block_count"]
        else:
            counts["other_block_count"] += 1
    metrics.update({key: value for key, value in counts.items() if value})
    return metrics


class ModelRequestTrace:
    """Append-only lifecycle facts; never records prompts or response bodies."""

    def __init__(self, phase_timer, architecture: str):
        self.phase_timer = phase_timer
        self.architecture = architecture
        self.request_id = str(uuid.uuid4())
        self.message_count = 0
        self.last_message_type = ""
        self.provider_request_id = ""
        self._first_seen = False
        self._first_assistant_seen = False
        self._first_assistant_content_seen = False
        self._first_text_seen = False
        self._first_thinking_seen = False
        self._first_tool_use_seen = False
        self._first_tool_result_seen = False
        self._finished = False

    def _mark(self, phase: str, **fields) -> None:
        if self.phase_timer:
            self.phase_timer.mark(
                phase, request_id=self.request_id, architecture=self.architecture, **fields,
            )

    def submitted(self) -> None:
        self._mark("model_request_submitted")

    def message(self, message) -> None:
        self.message_count += 1
        self.last_message_type = type(message).__name__
        self.provider_request_id = _provider_request_id(message) or self.provider_request_id
        fields = {
            "message_index": self.message_count,
            "message_type": self.last_message_type,
            "provider_request_id": self.provider_request_id,
            **safe_message_metrics(message),
        }
        if not self._first_seen:
            self._mark("model_first_message", **fields)
            self._first_seen = True
        content = getattr(message, "content", None)
        blocks = content if isinstance(content, list) else []
        if self.last_message_type == "AssistantMessage":
            if not self._first_assistant_seen:
                self._mark("model_first_assistant_message", **fields)
                self._first_assistant_seen = True
            has_content = False
            for block in blocks:
                kind = type(block).__name__
                if kind == "TextBlock" and str(getattr(block, "text", "") or "").strip():
                    has_content = True
                    if not self._first_text_seen:
                        self._mark("model_first_text", **fields)
                        self._first_text_seen = True
                elif (kind == "ThinkingBlock"
                      and str(getattr(block, "thinking", "") or "").strip()):
                    has_content = True
                    if not self._first_thinking_seen:
                        self._mark("model_first_thinking", **fields)
                        self._first_thinking_seen = True
                elif kind == "ToolUseBlock":
                    has_content = True
                    if not self._first_tool_use_seen:
                        self._mark("model_first_tool_use", **fields)
                        self._first_tool_use_seen = True
            if has_content and not self._first_assistant_content_seen:
                self._mark("model_first_assistant_content", **fields)
                self._first_assistant_content_seen = True
        if not self._first_tool_result_seen and any(
                type(block).__name__ == "ToolResultBlock" for block in blocks):
            self._mark("model_first_tool_result", **fields)
            self._first_tool_result_seen = True
        # Every received message updates the durable last-message fact. The last record wins.
        self._mark("model_last_message", **fields)

    def finish(self, end_reason: str, **fields) -> None:
        if self._finished:
            return
        self._finished = True
        self._mark(
            "model_request_finished",
            end_reason=end_reason,
            message_count=self.message_count,
            last_message_type=self.last_message_type,
            provider_request_id=self.provider_request_id,
            **fields,
        )


class ModelRequestTracker:
    """FIFO diagnostics for multiple ``query()`` calls on one persistent client."""

    def __init__(self, phase_timer, architecture: str):
        self.phase_timer = phase_timer
        self.architecture = architecture
        self.traces: list[ModelRequestTrace] = []
        self.completed = 0

    def submitted(self) -> ModelRequestTrace:
        trace = ModelRequestTrace(self.phase_timer, self.architecture)
        trace.submitted()
        self.traces.append(trace)
        return trace

    @property
    def current(self) -> ModelRequestTrace | None:
        if self.completed < len(self.traces):
            return self.traces[self.completed]
        return self.traces[-1] if self.traces else None

    @property
    def current_request_id(self) -> str:
        trace = self.current
        return trace.request_id if trace else ""

    def message(self, message) -> None:
        trace = self.current
        if trace:
            trace.message(message)

    def finish_result(self, end_reason: str, **fields) -> None:
        trace = self.current
        if trace:
            trace.finish(end_reason, **fields)
            self.completed = min(self.completed + 1, len(self.traces))

    def finish_current(self, end_reason: str, **fields) -> None:
        trace = self.current
        if trace:
            trace.finish(end_reason, **fields)

    def finish_request(self, request_id: str, end_reason: str, **fields) -> None:
        for trace in self.traces:
            if trace.request_id == request_id:
                trace.finish(end_reason, **fields)
                return

    def finish_open(self, end_reason: str, **fields) -> None:
        for trace in self.traces:
            trace.finish(end_reason, **fields)
