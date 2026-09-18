"""把 device-mcp/server.py 的设备工具 in-process 包成 Claude Agent SDK 工具。"""

import asyncio
import base64
import io
import json
import math
import os
import sys
import tempfile
import time as _time

import execution_context

# 让 import server 找到上层 device-mcp/server.py
_HERE = os.path.dirname(os.path.abspath(__file__))
_DEVICE_MCP = os.path.dirname(_HERE)
if _DEVICE_MCP not in sys.path:
    sys.path.insert(0, _DEVICE_MCP)

import server  # noqa: E402  device-mcp/server.py（设备原语与固定流程执行器）
from claude_agent_sdk import tool, create_sdk_mcp_server  # noqa: E402

# 最近一次 screenshot 实际发给模型的缩略图坐标系。单设备/单会话顺序执行，
# execute_ui_actions.tap_image 用它把模型看到的坐标自动换回设备坐标。
LAST_SCREENSHOT_META: dict | None = None

_NAV_GUARD_RATIO = 0.955
_ABSOLUTE_BOTTOM_RATIO = 0.995

# 当结构化 a11y 命中已经完整证明当前步骤的最终观测点时，阻止模型再请求一张
# 没有新增判定价值的截图。TestExcel 切换 trace/步骤时会清除此单步骤状态。
_FINAL_ASSERTION_SOURCE = ""


def clear_final_assertion_guard() -> None:
    global _FINAL_ASSERTION_SOURCE
    _FINAL_ASSERTION_SOURCE = ""


def _confirm_final_assertion(source: str, payload: dict, args: dict) -> None:
    global _FINAL_ASSERTION_SOURCE
    if payload.get("final_assertion_confirmed") is True:
        evidence_path = _log_final_assertion(source, args, payload)
        if evidence_path:
            _FINAL_ASSERTION_SOURCE = str(source)
            payload["evidence_path"] = evidence_path

# Excel 操作文本可声明“【绑定流程:名称】”。这是单 Suite/单设备进程内的步骤级闸门：
# 绑定流程成功前允许观察，但拒绝替代写操作与结果上报，避免只到达同一终点就绕路判 pass。
_REQUIRED_FLOW: dict = {}
_REQUIRED_FLOW_READ_TOOLS = {
    "get_screen", "find", "get_focused_app", "screenshot",
}


def set_required_flow(flow: str = "", step_id: str = "",
                      allow_manual_fallback: bool = False) -> None:
    global _REQUIRED_FLOW
    name = str(flow or "").strip()
    _REQUIRED_FLOW = ({
        "flow": name, "step_id": str(step_id or ""), "satisfied": False,
        "attempted": False, "failure": "",
        "allow_manual_fallback": bool(allow_manual_fallback),
    } if name else {})


def clear_required_flow() -> None:
    global _REQUIRED_FLOW
    _REQUIRED_FLOW = {}


def required_flow_satisfied(flow: str = "", step_id: str = "") -> bool:
    state = _REQUIRED_FLOW
    if not state:
        return not str(flow or "").strip()
    return bool(
        state.get("satisfied")
        and (not flow or state.get("flow") == str(flow).strip())
        and (not step_id or state.get("step_id") == str(step_id))
    )


def required_flow_state() -> dict:
    return dict(_REQUIRED_FLOW)


def _required_flow_block(tool_name: str, args: dict) -> dict | None:
    state = _REQUIRED_FLOW
    if not state:
        return None
    expected = str(state.get("flow") or "")
    if tool_name == "run_ui_flow":
        attempted = str((args or {}).get("flow") or "").strip()
        if attempted == expected and not state.get("attempted"):
            return None
        if state.get("attempted") and state.get("allow_manual_fallback"):
            return _txt({
                "error": "route_already_attempted: setup 绑定流程已尝试，不得重复执行",
                "required_flow": expected,
                "step_id": state.get("step_id", ""),
                "failure": state.get("failure", ""),
            })
    if state.get("satisfied") or tool_name in _REQUIRED_FLOW_READ_TOOLS:
        return None
    if state.get("attempted") and state.get("allow_manual_fallback"):
        return None
    reason = (
        "绑定流程已失败，禁止改走其他路线"
        if state.get("attempted") else "必须先成功执行 Excel 绑定流程"
    )
    return _txt({
        "error": f"route_required: {reason}",
        "required_flow": expected,
        "step_id": state.get("step_id", ""),
        "attempted_tool": tool_name,
        "failure": state.get("failure", ""),
    })


def _tool_payload(result: dict) -> dict:
    try:
        for item in result.get("content", []):
            if item.get("type") == "text":
                payload = json.loads(item.get("text") or "{}")
                if isinstance(payload, dict):
                    return payload
    except Exception:
        pass
    return {}


def _txt(obj) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}]}


def _compact_el(e: dict) -> dict:
    """紧凑元素：只留大脑决策必要字段，砍 bounds/class/空字段，省累积 token。"""
    o = {"c": e["center"]}
    if e.get("text"):
        o["t"] = e["text"]
    if e.get("resource_id"):
        o["rid"] = e["resource_id"].split("/")[-1]
    if e.get("content_desc") and not e.get("text"):
        o["desc"] = e["content_desc"]
    if e.get("clickable"):
        o["clk"] = 1
    if e.get("scrollable"):
        o["scr"] = 1
    if e.get("bounds_unreliable"):
        o["unrel"] = 1
    return o


def _compact_screen(d: dict, cap: int = 60) -> dict:
    if "elements" not in d:
        return d
    els = d["elements"]
    return {
        "focused_app": d.get("focused_app"),
        "wh": [d.get("width"), d.get("height")],
        "n": d.get("element_count"),
        "elements": [_compact_el(e) for e in els[:cap]],
        "_fmt": "c=center[x,y] t=text rid=resource_id desc=content_desc clk=clickable scr=scrollable unrel=bounds不可信(用选择器/截图)",
    }


async def _run(fn, *a, **k):
    """server 函数是同步阻塞(adb/u2)，丢线程跑，别堵事件循环。"""
    return await asyncio.to_thread(fn, *a, **k)


# ---- 停止闸门（把控制点下沉到每个设备动作的工具边界） ----
# app 启动时注入 tools.CONTROL = 同一个 Control(有 .stopped:bool)。
# Native Harness 进程使用 tools.device_server() 建工具，并共享本模块级 CONTROL。
# CLI / 纯 MCP 场景 CONTROL 为 None → _gate 直接放行。
CONTROL = None


async def _gate() -> bool:
    """工具边界闸门；复合工具还会把同一信号下传到私有轮询循环。"""
    return bool(CONTROL is not None and getattr(CONTROL, "stopped", False))


def _stop_callback():
    """Capture the current Turn generation so Continue cannot revive an old tool thread."""
    control = CONTROL
    sequence = int(getattr(control, "control_seq", 0) or 0) if control is not None else 0

    def stopped() -> bool:
        return bool(
            control is not None
            and (getattr(control, "stopped", False)
                 or int(getattr(control, "control_seq", 0) or 0) != sequence)
        )

    return stopped


def _stopped_result() -> dict:
    return _txt({"stopped": True, "note": "用户已停止，未执行本动作"})


# 截图返回的图要跨 stdio 送进 SDK；claude-agent-sdk 消息读取器单条 JSON 硬上限 1MiB。
# stream-json 会在 message.content 和 tool_use_result 中各带一份图片，JPEG 经 base64 又会 ×4/3。
# 320KB 预算让双份图片约占 853KB，给 JSON 包装和文本留约 195KB 余量。
SDK_JSON_MESSAGE_MAX_BYTES = 1_048_576
MODEL_IMAGE_MAX_BYTES = 320_000


# 策略：从高质量 JPEG 起（q92 边缘清晰、治 q65 把按钮边缘压糊导致目测落点偏），
# 先逐级降质；极端复杂页在 q50 仍超限时再等比缩小，直到硬性进入预算。
# 本地证据仍保留 server.save_screenshot 写入的原始 PNG，不受这里影响。
def _encode_image_under(im, max_bytes: int = MODEL_IMAGE_MAX_BYTES):
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    rgb = im.convert("RGB")
    qualities = (92, 85, 78, 70, 60, 50)
    while True:
        data, q = b"", qualities[-1]
        for q in qualities:
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=q)
            data = buf.getvalue()
            if len(data) <= max_bytes:
                return data, q

        # q50 仍超限：按 JPEG 面积近似关系估算下一尺寸，并额外留 8% 收敛余量。
        if rgb.width == 1 and rgb.height == 1:
            raise RuntimeError(f"1x1 JPEG still exceeds {max_bytes} bytes")
        factor = min(0.85, (max_bytes / len(data)) ** 0.5 * 0.92)
        new_size = (
            max(1, min(rgb.width - 1, int(rgb.width * factor))),
            max(1, min(rgb.height - 1, int(rgb.height * factor))),
        )
        rgb = rgb.resize(new_size)


def _visual_result(payload: dict, path: str, observation_source: str) -> dict:
    """Return one existing evidence frame to the model and make its coordinates current."""
    global LAST_SCREENSHOT_META
    meta = dict(payload)
    if not path or not os.path.isfile(path) or os.path.getsize(path) <= 0:
        return _txt(meta)
    if "evidence_paths" not in meta:
        meta.setdefault("evidence_path", path)
    try:
        from PIL import Image
        with Image.open(path) as source:
            ow, oh = source.size
            im = source.copy()
        long_edge = 1568
        if max(im.size) > long_edge:
            factor = long_edge / max(im.size)
            im = im.resize((int(im.width * factor), int(im.height * factor)))
        data, used_q = _encode_image_under(im)
        with Image.open(io.BytesIO(data)) as sent_image:
            iw, ih = sent_image.size
        scale = round(oh / ih, 4)
        nav_top = round(oh * _NAV_GUARD_RATIO)
        safe_image_y_max = min(ih - 1, math.floor((nav_top - 1) / scale))
        meta.update({
            "path": path,
            "device_wh": [ow, oh],
            "image_wh": [iw, ih],
            "scale": scale,
            "safe_image_y_max": safe_image_y_max,
            "observation_source": observation_source,
            "coord_hint": (
                f"当前图是{iw}x{ih}，可点击内容的 y 不得超过{safe_image_y_max}；"
                "图上坐标直接传 execute_ui_actions 的 tap_image，禁止自行换算。"
            ),
            "img_quality": used_q,
        })
        LAST_SCREENSHOT_META = {
            "device_wh": [ow, oh], "image_wh": [iw, ih],
            "scale": scale, "path": path,
        }
        return {"content": [
            {"type": "text", "text": json.dumps(meta, ensure_ascii=False)},
            {"type": "image", "data": base64.b64encode(data).decode(), "mimeType": "image/jpeg"},
        ]}
    except Exception as exc:
        meta["img_error"] = f"图返回失败,仅存盘: {exc}"
        return _txt(meta)


# ======================================================================
# 动作留痕（可靠监控）：每个点击/滑动前后各截全分辨率图、在实际落点画红十字、
# 比对前后屏幕像素差 + 前台窗口变化，落盘到 TRACE_DIR。用于事后判定"点在哪、
# 屏幕到底变没变"——区分"点偏了" vs "点在按钮上但没生效"。不喂给 agent(不吃 token)。
# ======================================================================
TRACE_DIR: str | None = None   # TestExcel 每条步骤前设成 suite/trace/step-id
_trace_n = 0


def set_trace_dir(d: str | None) -> None:
    global TRACE_DIR, _trace_n
    clear_final_assertion_guard()
    TRACE_DIR = d
    _trace_n = 0
    execution_context.set_trace_dir(d)
    if d:
        os.makedirs(d, exist_ok=True)


def _shot(fname: str):
    """截全分辨率图(不缩放,给人看)。返回 (path 或 None, secure)。"""
    p = os.path.join(TRACE_DIR, fname)
    try:
        r = server.save_screenshot(p)
        return (p if r.get("bytes", 0) > 0 else None), bool(r.get("secure", False))
    except Exception:
        return None, False


def _imgdiff(p1: str, p2: str):
    """前后两图的平均像素差(缩到小图算)。0=完全一致；>3 视为屏幕有变化。"""
    try:
        from PIL import Image, ImageChops
        a = Image.open(p1).convert("L").resize((90, 195))
        b = Image.open(p2).convert("L").resize((90, 195))
        px = list(ImageChops.difference(a, b).getdata())
        return round(sum(px) / len(px), 2)
    except Exception:
        return None


def _mark(path: str, x: int, y: int) -> None:
    """在截图上画红十字+坐标,标出实际点击点。"""
    try:
        from PIL import Image, ImageDraw
        im = Image.open(path).convert("RGB")
        dr = ImageDraw.Draw(im)
        R = 44
        dr.line([(x - R, y), (x + R, y)], fill=(255, 0, 0), width=6)
        dr.line([(x, y - R), (x, y + R)], fill=(255, 0, 0), width=6)
        dr.ellipse([x - 16, y - 16, x + 16, y + 16], outline=(255, 0, 0), width=6)
        dr.text((x + 24, y + 10), f"({x},{y})", fill=(255, 0, 0))
        im.save(path)
    except Exception:
        pass


async def _trace_action(tag: str, fn, xy=None, meta=None, include_after_path: bool = False,
                        reuse_step_after: bool = False):
    """留痕包装:前截→执行→等稳→后截→算屏幕差→写 trace.jsonl。返回原始工具结果(不变)。"""
    if not TRACE_DIR:
        return await _run(fn)
    global _trace_n
    n = _trace_n
    _trace_n += 1
    base = f"{n:02d}_{tag}"
    before, sec0 = await _run(_shot, f"{base}_before.png")
    foc0 = await _run(server.get_focused_app)
    res = await _run(fn)
    after, sec1 = None, False
    reused_after = False
    if reuse_step_after and isinstance(res, dict):
        flow = res.get("log") or []
        capture = (flow[-1].get("after_image") if flow else None)
        if capture is None and isinstance(res.get("failed_at"), dict):
            capture = res["failed_at"].get("after_image")
        if isinstance(capture, dict) and (capture.get("bytes", 0) > 0 or capture.get("secure")):
            candidate = str(capture.get("path") or "")
            after = candidate if capture.get("bytes", 0) > 0 and os.path.isfile(candidate) else None
            sec1 = bool(capture.get("secure"))
            reused_after = True
    if not reused_after:
        await asyncio.sleep(0.8)                   # 没有逐动作后图时才沉降并补批次末帧
        after, sec1 = await _run(_shot, f"{base}_after.png")
    foc1 = await _run(server.get_focused_app)
    diff = _imgdiff(before, after) if (before and after) else None
    if before and xy:                              # 先算完差再画十字(别污染 diff)
        _mark(before, xy[0], xy[1])
    rec = {
        "i": n, "action": tag, "xy": list(xy) if xy else None, "meta": meta,
        "focused_before": foc0, "focused_after": foc1, "focused_changed": foc0 != foc1,
        "screen_diff": diff, "screen_changed": (diff is not None and diff > 3.0),
        "secure": bool(sec0 or sec1),
        "before_img": before, "after_img": after,
    }
    try:
        with open(os.path.join(TRACE_DIR, "trace.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    result = dict(res) if isinstance(res, dict) else {"result": res}
    if include_after_path and after:
        result["_after_image_path"] = after
    return result


async def _capture_current_frame(tag: str) -> tuple[str, dict]:
    """Capture a response frame even when no Suite trace directory exists."""
    base_dir = TRACE_DIR or os.path.join(tempfile.gettempdir(), "miniapp-pilot-agent-frames")
    os.makedirs(base_dir, exist_ok=True)
    path = os.path.join(base_dir, f"{_time.time_ns()}_{tag}.png")
    try:
        capture = await _run(server.save_screenshot, path)
    except Exception as exc:
        return "", {"bytes": 0, "secure": False, "error": str(exc)}
    if capture.get("bytes", 0) <= 0:
        return "", capture
    return path, capture


# ======================================================================
# 原始流A·工具流：统一插桩。所有工具的 handler 包一层(见文件底部 _instrument 应用)，
# 按调用顺序记 per-case toolcalls.jsonl：ts/耗时ms/参数摘要/结果信号/返回体字符数/图token估算/异常。
# 纯观测、不改行为、只在套件跑测（TRACE_DIR 有值）时写。指标一律离线派生。
# ======================================================================
_SIG_KEYS = ("found", "webview_surface", "waited_s", "changed", "hit_xy", "tries", "diff",
             "count", "n", "bytes", "secure", "matched", "dismissed", "taps", "verdict",
             "stopped", "img_quality", "ok", "recorded", "reason", "package", "activity",
             "focused_app", "via", "requested_count", "completed_count", "coordinates",
             # 原子流程工具的判决字段。缺了它们，离线就分不清 wait_digits_leave 是"没到页/键盘读不到/
             # 输完没走掉"，也分不清 run_ui_steps 是"短路收工/跑满全程/卡在第几步"——而这正是要看的。
             "page_matched", "typed", "left", "digits_n", "leave_waited_s",
             "short_circuit", "done_steps", "total_steps", "stopped_at_step",
             "done_routes", "stage", "script",
             "final_assertion_confirmed", "redundant_screenshot_blocked",
             "postcondition_satisfied",
             "observation_source", "safe_image_y_max", "visual_fallback_requested",
             "visual_fallback_used", "bottom_edge_override_count",
             "preflight_rejected", "preflight_checked",
             "image_tap_count", "coordinate_source",
             "before_len", "after_len", "via", "label_source", "tap1_via", "tap1_ok")


_SENSITIVE_ARG_FIELDS = {
    "api_key", "authorization", "token", "password", "passwd", "pwd", "secret",
    "account", "subject", "user_id", "session_id",
    "账号", "帐号", "密码", "身份证", "手机号", "卡号",
}
_SENSITIVE_TOOL_FIELDS = {
    "run_ui_steps": {"vars"},
    "run_ui_flow": {"vars"},
    "execute_ui_actions": {"actions"},
    "tap_digits": {"digits"},
    "wait_digits_leave": {"digits"},
    "prepare_test_data": {"parameters"},
}


def _digest_args(args, tool_name: str = "") -> dict:
    """参数摘要:数值原样、长文本截断(供离线做重复调用簇/循环检测,别把大payload写进日志)。"""
    out = {}
    for k, v in (args or {}).items():
        key = str(k)
        lower = key.lower()
        sensitive = (
            lower in _SENSITIVE_ARG_FIELDS
            or key in _SENSITIVE_ARG_FIELDS
            or key in _SENSITIVE_TOOL_FIELDS.get(tool_name, set())
        )
        if sensitive:
            out[key] = f"[REDACTED:{len(str(v or ''))}]"
        else:
            out[key] = v if isinstance(v, (int, float, bool)) else str(v)[:60]
    return out


def safe_arg_summary(tool_name: str, args) -> str:
    return ", ".join(f"{k}={v}" for k, v in list(_digest_args(args, tool_name).items())[:3])


def _dynamic_action_observation_metrics(args: dict) -> dict:
    """Return selector-free adoption metrics for a dynamic action batch."""
    actions = (args or {}).get("actions") or []
    if not isinstance(actions, list):
        actions = []
    positive_semantic = 0
    weak = 0
    for action in actions:
        if not isinstance(action, dict):
            continue
        postcondition = action.get("postcondition") or {}
        for check in postcondition.get("checks") or []:
            if not isinstance(check, dict):
                continue
            check_type = str(check.get("type") or "")
            state = str(check.get("state") or "present")
            if check_type in {"a11y", "focus"} and state == "present":
                positive_semantic += 1
            if check_type == "screen_change" or state == "absent":
                weak += 1
    final_postcondition = {}
    if actions and isinstance(actions[-1], dict):
        final_postcondition = actions[-1].get("postcondition") or {}
    final_checks = final_postcondition.get("checks") or []
    try:
        settle_ms = float(final_postcondition.get("settle_ms") or 0)
    except (TypeError, ValueError):
        settle_ms = 0
    settle_only = bool(final_postcondition and not final_checks and settle_ms > 0)
    return {
        "action_count": len(actions),
        "final_assertion_requested": bool((args or {}).get("final_assertion", False)),
        "positive_semantic_check_count": positive_semantic,
        "weak_check_count": weak,
        "settle_only": settle_only,
    }


def _log_steps(rec: dict) -> str:
    """原始流D·UI步骤流:per-case uisteps.jsonl。逐步明细(动作/目标/成败/每步耗时/当时窗口)。
    【为什么单独存】Run 中成功时大脑不需要整份流水；离线仍要靠它答"哪一步最慢/最容易失败"。
    无 Run 目录时由调用入口直接返回逐步结果。纯 append，写失败不影响执行。"""
    if not TRACE_DIR:
        return ""
    path = os.path.join(TRACE_DIR, "uisteps.jsonl")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return path
    except Exception:
        return ""


def _log_final_assertion(source: str, args: dict, payload: dict) -> str:
    """Persist a structured final assertion so removing a screenshot never removes evidence."""
    if not TRACE_DIR:
        return ""
    path = os.path.join(TRACE_DIR, "final_assertions.jsonl")
    query = {
        "text": str((args or {}).get("text") or "")[:160],
        "resource_id": str((args or {}).get("resource_id") or "")[:160],
    }
    if source == "execute_ui_actions":
        actions = list((args or {}).get("actions") or [])
        query = {"postcondition": (actions[-1].get("postcondition") if actions else None)}
    record = {
        "ts": round(_time.time(), 3),
        "source": str(source),
        "query": query,
        "result": {
            key: payload[key]
            for key in (
                "found", "count", "center", "bounds_unreliable", "focused_app",
                "waited_s", "final_assertion_confirmed", "postcondition_satisfied",
                "final_postcondition",
            )
            if key in payload
        },
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path
    except Exception:
        return ""


def _log_event(rec: dict) -> None:
    if not TRACE_DIR:
        return
    try:
        with open(os.path.join(TRACE_DIR, "toolcalls.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _has_trusted_positive_final_check(step: dict) -> bool:
    """Whether a satisfied action result contains a positive semantic proof.

    Action success and deterministic Case completion are deliberately separate:
    visual change, settling and disappearance can drive execution, but only an
    observed positive a11y hit or a real focus transition can close a Case
    without returning the frame to the model.
    """
    postcondition = step.get("postcondition") or {}
    focus_before = str(step.get("_final_focus_before") or "")
    for check in postcondition.get("checks") or []:
        if not check.get("satisfied"):
            continue
        if str(check.get("state") or "present") != "present":
            continue
        check_type = str(check.get("type") or "")
        if check_type == "a11y":
            if int(check.get("matched_count") or 0) > 0 and isinstance(
                    check.get("matched"), dict):
                return True
        elif check_type == "focus":
            observed = str(check.get("observed") or "")
            expected = str(check.get("value") or "")
            if focus_before and observed and observed != focus_before and expected in observed:
                return True
    return False


def _strip_private_action_observations(flow: list[dict] | None) -> None:
    for step in flow or []:
        step.pop("_final_focus_before", None)


def _instrument(t):
    """把一个 @tool 的 handler 包上观测层:计时+参数摘要+从返回体提取信号。逐工具零侵入。"""
    orig = t.handler

    async def wrapped(args, _orig=orig, _name=t.name):
        t0 = _time.time()
        blocked = _required_flow_block(_name, args)
        if blocked is not None:
            _log_event({
                "ts": round(t0, 3), "tool": _name, "ms": 0,
                "args": _digest_args(args, _name),
                "sig": {"reason": "route_required"},
            })
            return blocked
        try:
            res = await _orig(args)
        except Exception as e:
            _log_event({"ts": round(t0, 3), "tool": _name, "ms": int((_time.time() - t0) * 1000),
                        "args": _digest_args(args, _name), "err": str(e)[:200]})
            raise
        if (_name == "run_ui_flow" and _REQUIRED_FLOW
                and str((args or {}).get("flow") or "").strip() == _REQUIRED_FLOW.get("flow")):
            payload = _tool_payload(res)
            if not payload.get("stopped"):
                _REQUIRED_FLOW["attempted"] = True
                _REQUIRED_FLOW["satisfied"] = payload.get("ok") is True
                if not _REQUIRED_FLOW["satisfied"]:
                    _REQUIRED_FLOW["failure"] = str(
                        payload.get("reason") or payload.get("error") or "流程执行失败"
                    )[:200]
        rec = {"ts": round(t0, 3), "tool": _name, "ms": int((_time.time() - t0) * 1000),
               "args": _digest_args(args, _name)}
        try:
            out_chars, sig, img_tok = 0, {}, 0
            for c in res.get("content", []):
                if c.get("type") == "text":
                    txt = c.get("text", "")
                    out_chars += len(txt)
                    if not sig:
                        try:
                            d = json.loads(txt)
                            sig = {k: d[k] for k in _SIG_KEYS if k in d}
                            fa = d.get("failed_at")
                            if isinstance(fa, dict):       # 卡在哪一步:离线要能直接聚合"最常失败的步"
                                sig["fail_i"] = fa.get("i")
                                sig["fail_action"] = fa.get("action")
                                sig["fail_reason"] = str(fa.get("reason", ""))[:80]
                                sig["action_executed"] = fa.get("action_executed")
                                sig["postcondition_satisfied"] = fa.get(
                                    "postcondition_satisfied"
                                )
                            wh = d.get("image_wh")
                            if wh:  # 视觉 token 估算 ≈ w*h/750(Anthropic 视觉计费公式)
                                img_tok = int(wh[0] * wh[1] / 750)
                        except Exception:
                            pass
                elif c.get("type") == "image":
                    rec["has_img"] = True
            rec["out_chars"] = out_chars
            if _name == "execute_ui_actions":
                sig.update(_dynamic_action_observation_metrics(args))
            if sig:
                rec["sig"] = sig
            if img_tok:
                rec["img_tok_est"] = img_tok
        except Exception:
            pass
        _log_event(rec)
        return res

    t.handler = wrapped
    return t


# ---------- 眼 ----------
@tool("get_screen", "取当前屏幕a11y元素(紧凑:c=center,t=text,rid,desc,clk=可点,unrel=bounds不可信)+前台app。结构化元素首选用它。", {})
async def get_screen(args):
    if await _gate():
        return _stopped_result()
    return _txt(_compact_screen(await _run(server.get_screen)))


@tool("find", "定向找元素(只回匹配项,token轻)。text子串/resource_id后缀匹配。点之前先确认元素在不在、坐标可不可信。"
      "count=1、matches[0].c 有效且没有 unrel 时，下一动作直接用 execute_ui_actions 的 tap_xy；"
      "多命中、无中心点或 unrel=1 时不得使用该坐标捷径。"
      "预期目标可能是自绘、列表滚动后的视觉内容或a11y不可靠时传 visual_on_miss=true；未命中会在同一次结果直接附当前画面。"
      "未显式传参但识别为webview_surface时也会自动带图；拿到图后应转视觉判断，不得重复相同find/wait或补调screenshot。"
      "仅当这次命中本身足以完整证明当前步骤的最终观测点时传 final_assertion=true；命中会返回结构化 evidence_path，"
      "应直接用于记录结果，冗余截图会被拒绝。",
      {"text": str, "resource_id": str, "clickable_only": bool, "final_assertion": bool,
       "visual_on_miss": bool})
async def find(args):
    if await _gate():
        return _stopped_result()
    d = await _run(
        server.find, args.get("text", ""), args.get("resource_id", ""),
        args.get("clickable_only", False), bool(args.get("final_assertion", False)),
        bool(args.get("visual_on_miss", False)),
    )
    d["matches"] = [_compact_el({**m, "center": m["center"]}) for m in d.get("matches", [])]
    _confirm_final_assertion("find", d, args)
    explicit_visual = bool(args.get("visual_on_miss", False))
    wants_visual = d.get("count") == 0 and (explicit_visual or d.get("webview_surface") is True)
    if wants_visual and TRACE_DIR:
        source = "find_visual_fallback" if explicit_visual else "find_webview_fallback"
        path = os.path.join(TRACE_DIR, f"{source}_{_time.time_ns()}.png")
        capture = await _run(server.save_screenshot, path)
        if capture.get("bytes", 0) > 0:
            d["visual_fallback_used"] = True
            return _visual_result(d, path, source)
        d.update({"secure": bool(capture.get("secure")), "capture": capture})
    return _txt(d)


@tool("get_focused_app", "返回当前前台窗口 包名/Activity。", {})
async def get_focused_app(args):
    if await _gate():
        return _stopped_result()
    return _txt({"focused_app": await _run(server.get_focused_app)})


@tool("screenshot", "在当前步骤证据目录自动保存截图并把图返回给你看(视觉兜底,a11y读不到/错层时用,图吃token)。"
      "图上选点后必须用 execute_ui_actions 的 tap_image，它会自动换算缩放；绝不要手算设备坐标。"
      "成功时返回顶层 evidence_path，记录步骤时原样放入 evidence_paths。"
      "0字节=FLAG_SECURE安全窗口(用get_screen的a11y取证)。", {})
async def screenshot(args):
    global LAST_SCREENSHOT_META
    if await _gate():
        return _stopped_result()
    if _FINAL_ASSERTION_SOURCE:
        return _txt({
            "ok": False,
            "redundant_screenshot_blocked": True,
            "reason": (
                f"最终观测点已经由 {_FINAL_ASSERTION_SOURCE} 的结构化命中确认；"
                "请直接记录步骤结果"
            ),
        })
    if not TRACE_DIR:
        return _txt({
            "ok": False,
            "error": "evidence_context_missing: 当前没有可用的证据目录",
        })
    path = os.path.join(TRACE_DIR, f"screenshot-{_time.time_ns()}.png")
    res = await _run(server.save_screenshot, path)
    LAST_SCREENSHOT_META = None
    if res.get("bytes", 0) > 0:
        return _visual_result(res, path, "screenshot")
    return _txt(res)


@tool(
    "tap_wait_tap",
    "时序关键动作原子化:点(tap1_x,tap1_y)→server端紧轮询前台窗口直到含 focus_contains→反复点(tap2_x,tap2_y)【直到前台离开触发页】才停,全程不回大脑(消除LLM往返延迟)。"
    "专治会超时的转瞬浮层/原生控件(如刷脸相机页:点'下一步'调起 FaceFlashUI、必须点左上×取消)。"
    "反复点是因为'焦点切到触发页≠目标按钮已可点'(相机页焦点0.8s就到、×要等渲染),单点常落空;点完回读焦点、没走就每隔 settle_ms 再点。"
    "tap1 传 -1,-1 可跳过(目标页已被上一步调起、只需等+点)。相机/安全页别拆成多次普通观察和点击——交给本工具一次做完。"
    "pre_tap2_ms=触发页出现后、首次点 tap2 前先等的毫秒数(默认0);嫌太快、×还没活就调大它。settle_ms=没点掉时每隔多久重试。"
    "【定位写法】tap1_target/tap2_target 都可用选择器(默认文字精确;~:片段=子串;rid:xxx;xy:x,y;pct:5,5.73按屏幕百分比),比写死像素抗改版、抗键盘位移、抗换分辨率。tap1 只读【触发页出现前】那一屏;tap2 在触发页读【一次】屏取坐标——实测相机页 dump 仅281ms,真正烧掉窗口的是 AI 分多次调工具的 LLM 往返(每次≈5s),不是这一次 dump。tap2 建议配 tap2_fallback=\"pct:5,5.73\":选择器没命中就退回该坐标并回报 tap2_via=fallback,绝不静默兜底。不传 target 则仍用 tap1_x/tap1_y、tap2_x/tap2_y 坐标,行为与以前完全一致。"
    "返回 {matched,dismissed,taps,waited_s,focus_before,focus_at_trigger,focus_after};dismissed=false 表示触发页出现但点了 taps 次仍没退出(坐标可能不对)。",
    {"tap1_x": int, "tap1_y": int, "focus_contains": str, "tap2_x": int, "tap2_y": int, "timeout_s": float, "poll_ms": int, "settle_ms": int, "pre_tap2_ms": int, "tap1_target": str, "tap2_target": str, "tap2_fallback": str},
)
async def tap_wait_tap(args):
    if await _gate():
        return _stopped_result()
    call_args = [
        int(args["tap1_x"]), int(args["tap1_y"]), args.get("focus_contains", ""),
        int(args["tap2_x"]), int(args["tap2_y"]),
        float(args.get("timeout_s", 10.0)), int(args.get("poll_ms", 200)),
        int(args.get("settle_ms", 500)), int(args.get("pre_tap2_ms", 0)),
        args.get("tap1_target", ""), args.get("tap2_target", ""),
        args.get("tap2_fallback", ""),
    ]
    function = server.tap_wait_tap
    if CONTROL is not None:
        function = server._tap_wait_tap_impl
        call_args.append(_stop_callback())
    return _txt(await _run(function, *call_args))


@tool(
    "wait_digits_leave",
    "数字输入页原子化:等目标页就绪→在app自绘数字键盘上逐位输入→等前台离开该页→回报落到哪一页,全程不回大脑(把'看页→输入→等结果'三个往返压成一次)。"
    "适用任何'出现一个要输N位数字的页、输完就跳走'的场景。到页判据用参数给:page_text(该页应出现的文本)/page_focus_contains(该页窗口名子串),"
    "至少给一个、都给则都满足才算到页;离开判据是它的反面——比'等下一页的预期文本'可靠得多(下一页常是webview自绘面,预期文本进不了a11y)。"
    "三条纪律:①没确认到页→一位都不点(page_matched=false);②a11y和精确真机标定都定位不到键盘→不点(typed=false+available_keys),自行判断;"
    "③输完没走掉→如实回 left=false,【绝不重输】(重复输入有锁定风险),自行截图看提示再决定。"
    "页面离开后会在同一次调用内抢拍首帧并返回after_texts/after_screenshot，优先用它核验短暂toast或半屏浮层。"
    "别再拆成到页观察、数字输入和离页等待三次调用。",
    {"digits": str, "page_text": str, "page_focus_contains": str, "timeout_s": float,
     "leave_timeout_s": float, "poll_ms": int, "tap_confirm": bool, "confirm_text": str},
)
async def wait_digits_leave(args):
    if await _gate():
        return _stopped_result()
    after_path = os.path.join(TRACE_DIR, "wait_digits_after.png") if TRACE_DIR else ""
    call_args = [
        args["digits"], args.get("page_text", ""), args.get("page_focus_contains", ""),
        float(args.get("timeout_s", 10.0)), float(args.get("leave_timeout_s", 8.0)),
        int(args.get("poll_ms", 400)), bool(args.get("tap_confirm", False)),
        args.get("confirm_text", "确定"), after_path,
    ]
    function = server.wait_digits_leave
    if CONTROL is not None:
        function = server._wait_digits_leave_impl
        call_args.append(_stop_callback())
    result = await _run(function, *call_args)
    after_screenshot = result.get("after_screenshot") or {}
    evidence_path = str(after_screenshot.get("path") or "")
    if (after_screenshot.get("bytes", 0) > 0 and evidence_path
            and os.path.isfile(evidence_path)):
        result["evidence_path"] = evidence_path
    return _txt(result)


# 本次跑测可用的 UI 步骤脚本：{脚本名: [步骤,…]}，由 TestExcel 在套件开始时从 xlsx『步骤脚本』表读入。
# 与 CONTROL/TRACE_DIR 同属"引擎注入的模块级状态"。为空时 run_ui_steps 如实报"没有脚本"。
# 引擎只认"步骤表"这个通用概念，不认任何具体流程——流程知识全在人维护的 xlsx 里。
UI_SCRIPTS: dict = {}
UI_FLOWS: dict = {}

_RUN_UI_STEPS_SCHEMA = {
    "type": "object",
    "properties": {
        "script": {
            "type": "string",
            "description": "步骤脚本名；省略或传空字符串时只列出本次可用脚本，不执行设备动作。",
            "default": "",
        },
        "vars": {"type": "object", "default": {}, "additionalProperties": True},
        "default_timeout_s": {"type": "number", "default": 5.0},
        "poll_ms": {"type": "integer", "default": 400},
        "settle_ms": {"type": "integer", "default": 500},
    },
    "required": [],
    "additionalProperties": False,
}

_DYNAMIC_STEP_ACTIONS = [*server._STEP_ACTIONS, "tap_image"]

_DYNAMIC_REQUIRED_TARGET_ACTIONS = {
    "tap_text", "tap_text_retry", "tap_xy", "tap_image", "input",
    "ok_if_text", "launch_app", "stop_app",
}


def _dynamic_preflight_error(actions: list[dict], index: int, action: dict,
                             reason: str, hint: str = "") -> dict:
    """Return one uniform rejection shape while guaranteeing no device action has run."""
    payload = {
        "ok": False,
        "preflight_rejected": True,
        "preflight_checked": index + 1,
        "done_steps": 0,
        "total_steps": len(actions),
        "failed_at": {
            "i": index + 1,
            "action": str(action.get("action") or ""),
            "action_executed": False,
            "reason": reason,
        },
        "error": f"批次预检失败：第 {index + 1} 个动作 {reason}",
        "hint": hint or "批次未执行任何设备动作；修正该动作后再提交完整批次。",
    }
    return payload


def _postcondition_error(value) -> str:
    if not isinstance(value, dict):
        return "postcondition 必须是对象"
    allowed = {"mode", "settle_ms", "timeout_s", "checks"}
    extra = sorted(set(value) - allowed)
    if extra:
        return f"postcondition 含不支持字段: {extra}"
    if str(value.get("mode") or "all") not in {"all", "any"}:
        return "postcondition.mode 只能是 all 或 any"
    try:
        settle_ms = int(value.get("settle_ms") or 0)
        timeout_s = float(value.get("timeout_s") or 5.0)
    except (TypeError, ValueError):
        return "postcondition 的 settle_ms/timeout_s 必须是数值"
    if settle_ms < 0 or not math.isfinite(timeout_s) or timeout_s <= 0:
        return "postcondition 要求 settle_ms >= 0 且 timeout_s > 0"
    checks = value.get("checks")
    if not isinstance(checks, list) or len(checks) > 4:
        return "postcondition.checks 必须包含 0 到 4 个检查"
    if not checks and settle_ms <= 0:
        return "空 checks 必须声明大于 0 的 settle_ms"
    for check_index, check in enumerate(checks):
        if not isinstance(check, dict):
            return f"第 {check_index + 1} 个 check 必须是对象"
        check_type = str(check.get("type") or "")
        if check_type not in {"a11y", "focus", "screen_change"}:
            return f"第 {check_index + 1} 个 check.type 无效"
        if str(check.get("state") or "present") not in {"present", "absent"}:
            return f"第 {check_index + 1} 个 check.state 只能是 present 或 absent"
        if check_type == "a11y":
            if str(check.get("selector") or "text") not in {
                    "text", "resource_id", "content_desc"}:
                return f"第 {check_index + 1} 个 a11y selector 无效"
            if str(check.get("match") or "contains") not in {"exact", "contains", "suffix"}:
                return f"第 {check_index + 1} 个 a11y match 无效"
            if not str(check.get("value") or ""):
                return f"第 {check_index + 1} 个 a11y check 缺少 value"
        elif check_type == "focus" and not str(check.get("value") or ""):
            return f"第 {check_index + 1} 个 focus check 缺少 value"
        elif check_type == "screen_change":
            try:
                threshold = float(check.get("diff_threshold", 3.0))
            except (TypeError, ValueError):
                threshold = -1.0
            if not math.isfinite(threshold) or threshold < 0:
                return f"第 {check_index + 1} 个 screen_change diff_threshold 无效"
    return ""


def _preflight_dynamic_actions(actions: list[dict]) -> dict:
    """Validate and translate the whole dynamic batch before touching the device."""
    prepared_actions = []
    image_taps = []
    bottom_edge_overrides = 0
    supported = set(_DYNAMIC_STEP_ACTIONS)
    valid_keys = {"wake", "back", "home", "enter", "delete"}

    for index, action in enumerate(actions):
        prepared = dict(action)
        action_name = str(prepared.get("action") or "").strip()
        target = str(prepared.get("target") or "").strip()
        if "expect" in prepared:
            return {"ok": False, "payload": _dynamic_preflight_error(
                actions, index, prepared,
                "动态动作不再接受 expect；请改用一个 postcondition 对象",
            )}
        postcondition = prepared.get("postcondition")

        if action_name not in supported:
            return {"ok": False, "payload": _dynamic_preflight_error(
                actions, index, prepared, f"未知动作 {action_name!r}"
            )}
        if action_name in _DYNAMIC_REQUIRED_TARGET_ACTIONS and not target:
            return {"ok": False, "payload": _dynamic_preflight_error(
                actions, index, prepared, f"{action_name} 必须提供非空 target"
            )}
        if prepared.get("allow_bottom_edge") and action_name != "tap_image":
            return {"ok": False, "payload": _dynamic_preflight_error(
                actions, index, prepared, "allow_bottom_edge 只允许用于 tap_image"
            )}
        if "index" in prepared:
            if action_name not in {"tap_text", "tap_text_retry"}:
                return {"ok": False, "payload": _dynamic_preflight_error(
                    actions, index, prepared, "index 只允许用于 tap_text/tap_text_retry",
                )}
            try:
                if int(prepared["index"]) < 0:
                    raise ValueError("negative index")
            except (TypeError, ValueError):
                return {"ok": False, "payload": _dynamic_preflight_error(
                    actions, index, prepared, "index 必须是非负整数",
                )}
        if "max_chars" in prepared:
            if action_name != "clear_input":
                return {"ok": False, "payload": _dynamic_preflight_error(
                    actions, index, prepared, "max_chars 只允许用于 clear_input",
                )}
            try:
                max_chars = int(prepared["max_chars"])
            except (TypeError, ValueError):
                max_chars = 0
            if not 1 <= max_chars <= 1000:
                return {"ok": False, "payload": _dynamic_preflight_error(
                    actions, index, prepared, "max_chars 必须在 1 到 1000 之间",
                )}
        if "timeout_s" in prepared:
            try:
                timeout_s = float(prepared["timeout_s"])
            except (TypeError, ValueError):
                timeout_s = 0.0
            if not math.isfinite(timeout_s) or timeout_s <= 0:
                return {"ok": False, "payload": _dynamic_preflight_error(
                    actions, index, prepared, "timeout_s 必须大于 0"
                )}
        if postcondition is not None:
            condition_error = _postcondition_error(postcondition)
            if condition_error:
                return {"ok": False, "payload": _dynamic_preflight_error(
                    actions, index, prepared, condition_error,
                )}
        if action_name == "wait" and postcondition is None:
            return {"ok": False, "payload": _dynamic_preflight_error(
                actions, index, prepared,
                "wait 必须声明 postcondition（检查条件或明确 settle_ms）",
            )}
        if action_name == "tap_text_retry":
            if postcondition is None or not postcondition.get("checks"):
                return {"ok": False, "payload": _dynamic_preflight_error(
                    actions, index, prepared,
                    "tap_text_retry 必须提供含检查的 postcondition",
                )}
            if server._resolve_target(target)[0] == "xy":
                return {"ok": False, "payload": _dynamic_preflight_error(
                    actions, index, prepared,
                    "tap_text_retry 不支持 xy:/pct: 坐标 target",
                )}
        if action_name == "press_key" and target and target.lower() not in valid_keys:
            return {"ok": False, "payload": _dynamic_preflight_error(
                actions, index, prepared,
                f"press_key 不支持 {target!r}；可用: {', '.join(sorted(valid_keys))}",
            )}
        if action_name == "launch_app" and target.startswith("app:") and not target[4:].strip():
            return {"ok": False, "payload": _dynamic_preflight_error(
                actions, index, prepared, "launch_app 的 app:显示名不能为空",
            )}
        if action_name == "tap_xy":
            try:
                coordinate = server._resolve_target(target)[1]
                is_percent = coordinate.startswith("pct:")
                if is_percent:
                    coordinate = coordinate[4:]
                raw_x, separator, raw_y = coordinate.partition(",")
                if not separator or "," in raw_y:
                    raise ValueError("invalid coordinate")
                x_value = float(raw_x)
                y_value = float(raw_y)
                if not math.isfinite(x_value) or not math.isfinite(y_value):
                    raise ValueError("non-finite coordinate")
                if is_percent:
                    if not (0 <= x_value < 100 and 0 <= y_value < 100):
                        raise ValueError("percent coordinate out of bounds")
                else:
                    width, height = server._wm_size()
                    if not width or not height:
                        raise ValueError("device viewport unavailable")
                    if not (0 <= x_value < width and 0 <= y_value < height):
                        raise ValueError("absolute coordinate out of bounds")
            except (TypeError, ValueError):
                return {"ok": False, "payload": _dynamic_preflight_error(
                    actions, index, prepared,
                    "tap_xy target 必须是 viewport 内有限、非负的 x,y/xy:x,y 或 pct:x,y",
                )}
        if action_name == "tap_image":
            if not LAST_SCREENSHOT_META:
                return {"ok": False, "payload": _dynamic_preflight_error(
                    actions, index, prepared, "tap_image 需要当前画面；请先观察页面"
                )}
            try:
                raw_x, separator, raw_y = target.partition(",")
                if not separator or "," in raw_y:
                    raise ValueError("invalid coordinate")
                image_x, image_y = int(float(raw_x)), int(float(raw_y))
            except (TypeError, ValueError):
                return {"ok": False, "payload": _dynamic_preflight_error(
                    actions, index, prepared,
                    "tap_image target 必须是当前图片坐标 x,y",
                )}
            image_w, image_h = LAST_SCREENSHOT_META["image_wh"]
            _, device_h = LAST_SCREENSHOT_META["device_wh"]
            if not (0 <= image_x < image_w and 0 <= image_y < image_h):
                payload = _dynamic_preflight_error(
                    actions, index, prepared, "tap_image 坐标越界"
                )
                payload.update({"image_xy": [image_x, image_y], "image_wh": [image_w, image_h]})
                return {"ok": False, "payload": payload}
            scale = float(LAST_SCREENSHOT_META["scale"])
            device_x, device_y = round(image_x * scale), round(image_y * scale)
            nav_top = round(device_h * _NAV_GUARD_RATIO)
            allow_bottom_edge = bool(prepared.get("allow_bottom_edge", False))
            absolute_bottom = math.floor(device_h * _ABSOLUTE_BOTTOM_RATIO)
            if device_y >= nav_top and not (
                    allow_bottom_edge and device_y < absolute_bottom):
                payload = _dynamic_preflight_error(
                    actions, index, prepared, "拒绝点击Android底部系统导航栏"
                )
                payload.update({
                    "image_xy": [image_x, image_y], "device_xy": [device_x, device_y],
                    "nav_top": nav_top,
                    "safe_image_y_max": min(
                        image_h - 1, math.floor((nav_top - 1) / scale)
                    ),
                })
                return {
                    "ok": False, "payload": payload,
                    "visual_path": LAST_SCREENSHOT_META["path"],
                }
            if device_y >= nav_top:
                bottom_edge_overrides += 1
            prepared["action"] = "tap_xy"
            prepared["target"] = f"xy:{device_x},{device_y}"
            prepared.pop("allow_bottom_edge", None)
            image_taps.append({
                "i": index + 1, "image_xy": [image_x, image_y],
                "device_xy": [device_x, device_y],
            })
        prepared_actions.append(prepared)

    return {
        "ok": True,
        "actions": prepared_actions,
        "image_taps": image_taps,
        "bottom_edge_overrides": bottom_edge_overrides,
        "preflight_checked": len(actions),
    }


_POSTCONDITION_CHECK_SCHEMA = {
    "type": "object",
    "description": "一个原子检查；多个检查只在所属 postcondition 的统一期限内扁平组合。",
    "properties": {
        "type": {
            "type": "string", "enum": ["a11y", "focus", "screen_change"],
            "description": "screen_change 只在页面确应变化时显式使用，绝不是点击默认判据。",
        },
        "selector": {
            "type": "string", "enum": ["text", "resource_id", "content_desc"],
            "default": "text",
        },
        "value": {
            "type": "string", "default": "",
            "description": "a11y 目标值或 focus 的包名/Activity 子串；screen_change 省略。",
        },
        "match": {
            "type": "string", "enum": ["exact", "contains", "suffix"],
            "default": "contains",
        },
        "state": {"type": "string", "enum": ["present", "absent"], "default": "present"},
        "diff_threshold": {"type": "number", "default": 3.0},
    },
    "required": ["type"],
    "additionalProperties": False,
}

_POSTCONDITION_SCHEMA = {
    "type": "object",
    "description": (
        "动作后的唯一组合条件：一个 settle、一个 timeout、一个整体结果。"
        "checks 可空但此时 settle_ms 必须大于 0。screen_change、settle 和单独 absent "
        "可以驱动动作但不能单独确认 final_assertion；没有可靠 a11y 的页面应返回画面做视觉核验。"
    ),
    "properties": {
        "mode": {"type": "string", "enum": ["all", "any"], "default": "all"},
        "settle_ms": {"type": "integer", "minimum": 0, "default": 0},
        "timeout_s": {"type": "number", "exclusiveMinimum": 0, "default": 5.0},
        "checks": {
            "type": "array", "minItems": 0, "maxItems": 4,
            "items": _POSTCONDITION_CHECK_SCHEMA,
        },
    },
    "required": ["checks"],
    "additionalProperties": False,
}


_EXECUTE_UI_ACTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": _DYNAMIC_STEP_ACTIONS,
                        "description": (
                            "通用动作类型；点击、输入、滑动、按键、等待、条件短路和应用启停"
                            "都由同一受保护执行器串行完成。tap_image 专用于当前返回画面上的坐标。"
                        ),
                    },
                    "target": {
                        "type": "string", "default": "",
                        "description": (
                            "默认是精确文本；也可用 ~:子串、row:行文字、rid:资源ID后缀、"
                            "desc:描述、xy:540,1746 设备坐标或 pct:50,75 屏幕百分比。"
                            "tap_image 时传当前工具返回图片上的 x,y，例如 240,860；"
                            "由工具统一换算并拒绝系统导航栏，禁止模型自行缩放。"
                            "input 时为待输入文本；press_key 时为 wake/back/home/enter/delete；"
                            "launch_app 可传包名或 app:显示名。"
                        ),
                    },
                    "postcondition": _POSTCONDITION_SCHEMA,
                    "index": {
                        "type": "integer", "minimum": 0, "default": 0,
                        "description": "仅 tap_text/tap_text_retry：点击第几个匹配元素（从 0 开始）。",
                    },
                    "max_chars": {
                        "type": "integer", "minimum": 1, "maximum": 1000, "default": 40,
                        "description": "仅 clear_input：最多发送多少次 delete。",
                    },
                    "allow_bottom_edge": {
                        "type": "boolean", "default": False,
                        "description": (
                            "仅用于 tap_image，且当前画面已明确看见应用自有数字键盘或底部控件时设为 true。"
                            "它只放行普通导航栏保护线以下、屏幕绝对底边以上的视觉落点；"
                            "不得用于系统返回/Home/最近任务区域。"
                        ),
                    },
                    "timeout_s": {"type": "number"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
        "default_timeout_s": {"type": "number", "default": 5.0},
        "poll_ms": {"type": "integer", "default": 400},
        "final_assertion": {
            "type": "boolean", "default": False,
            "description": (
                "最后一个动作的整体 postcondition 已完整证明最终观测点时设为 true。"
                "自动确认要求实际命中的正向 a11y，或动作前后真实发生的 focus/Activity 切换；"
                "mode=any 同时声明正向语义候选和弱条件时先检查语义；"
                "弱条件先满足后只保留短暂语义宽限，仍未命中则返回视觉保底。"
            ),
        },
    },
    "required": ["actions"],
    "additionalProperties": False,
}

_RUN_UI_FLOW_SCHEMA = {
    "type": "object",
    "properties": {
        "flow": {"type": "string", "description": "xlsx『流程映射』中的流程名。"},
        "vars": {"type": "object", "default": {}, "additionalProperties": True},
        "start_stage": {
            "type": "string",
            "description": "通常省略并从『开始』执行；仅可传流程映射中真实存在的阶段名，禁止根据当前页面猜测。",
            "default": "开始",
        },
        "default_timeout_s": {"type": "number", "default": 5.0},
        "poll_ms": {"type": "integer", "default": 400},
        "settle_ms": {"type": "integer", "default": 500},
        "route_timeout_s": {"type": "number", "default": 8.0},
        "route_settle_ms": {"type": "integer", "default": 2000},
        "max_routes": {"type": "integer", "default": 20},
    },
    "required": ["flow"],
    "additionalProperties": False,
}


def set_ui_scripts(scripts: dict | None) -> None:
    global UI_SCRIPTS
    UI_SCRIPTS = scripts or {}


def set_ui_flows(flows: dict | None) -> None:
    global UI_FLOWS
    UI_FLOWS = flows or {}


@tool(
    "run_ui_steps",
    "按【预先维护好的步骤脚本】一口气走完一条固定 UI 流程(点/输入/清空/滑动/按键+逐步校验),全程不回大脑——"
    "把十几步的固定路径从十几个往返压成一次调用。script=脚本名(见本次须知),vars=脚本里 {变量} 的取值(如账号/密码,从用例文本取)。"
    "任一步动作做不了或校验超时即【停止并返回】走到第几步、卡在哪一步的什么校验、当前窗口;执行器不重试不跳过不硬闯。"
    "中断后设备多半停在流程中间态:请先看屏确认真实状态再处置,别盲目从头重跑整串。"
    "不知道有哪些脚本就先不传 script 调一次,会以成功的枚举结果列出可用脚本名；这不是脚本执行失败。"
    "步骤流水会落盘；成功落盘时返回 evidence_path，记录结果时应将其作为证据传入。",
    _RUN_UI_STEPS_SCHEMA,
)
async def run_ui_steps(args):
    if await _gate():
        return _stopped_result()
    import uiscripts
    name = (args.get("script") or "").strip()
    if not name:
        return _txt({"ok": True, "listed": True, "available": sorted(UI_SCRIPTS)})
    if name not in UI_SCRIPTS:
        return _txt({"ok": False, "error": f"没有名为 {name!r} 的步骤脚本",
                     "available": sorted(UI_SCRIPTS)})
    steps = UI_SCRIPTS[name]
    variables = args.get("vars") or {}
    miss = uiscripts.missing_vars(steps, variables)
    if miss:
        return _txt({"ok": False, "error": f"脚本 {name!r} 缺变量取值: {miss}", "needed": miss})
    res = await _run(server.execute_ui_steps, uiscripts.resolve(steps, variables),
                     float(args.get("default_timeout_s", 5.0)),
                     int(args.get("poll_ms", 400)), int(args.get("settle_ms", 500)),
                     _stop_callback())
    flow = res.pop("log", None)                    # 逐步流水从返回体摘出
    if flow:
        evidence_path = _log_steps({"ts": round(_time.time(), 3), "script": name,
                                    "ok": res.get("ok"),
                                    "short_circuit": res.get("short_circuit"), "steps": flow})
        if evidence_path:
            res["evidence_path"] = evidence_path
        if not res.get("ok"):
            res["log"] = flow                      # 只有失败才回给大脑——它要据此判断卡在哪
    return _txt(res)


@tool(
    "execute_ui_actions",
    "一次执行1到8个临时通用UI动作；每个动作可带统一期限的 postcondition。"
    "有副作用动作的纯语义条件和 all 完整使用显式 timeout；最终 any 组合中语义条件优先，"
    "弱条件先满足后只保留短暂语义宽限，再转视觉保底。"
    "失败或Stop时在下一动作前停止并返回已完成前缀，已执行动作不会重放。"
    "final_assertion 请求以最后一个动作的 postcondition 作为最终检查；可信语义命中时返回"
    " final_assertion_confirmed 和 evidence_paths 且不返回内联图片，否则返回动作后画面供调用方判断。",
    _EXECUTE_UI_ACTIONS_SCHEMA,
)
async def execute_ui_actions(args):
    global LAST_SCREENSHOT_META
    if await _gate():
        return _stopped_result()
    actions = args.get("actions") or []
    if not isinstance(actions, list) or not 1 <= len(actions) <= 8:
        return _txt({"ok": False, "error": "actions 必须包含1到8个动作"})
    if any(not isinstance(action, dict) for action in actions):
        return _txt({"ok": False, "error": "actions 每一项都必须是动作对象"})
    preflight = _preflight_dynamic_actions(actions)
    if not preflight["ok"]:
        payload = preflight["payload"]
        visual_path = str(preflight.get("visual_path") or "")
        if (not visual_path and any(
                str(action.get("action") or "") == "tap_image" for action in actions)
                and not LAST_SCREENSHOT_META):
            visual_path, capture = await _capture_current_frame("tap_image_recovery")
            if not visual_path:
                payload["capture"] = capture
            else:
                payload["evidence_paths"] = [visual_path]
                return _visual_result(payload, visual_path, "failure_current_screen")
        if visual_path:
            return _visual_result(payload, visual_path, "rejected_coordinate_frame")
        return _txt(payload)

    prepared_actions = preflight["actions"]
    image_taps = preflight["image_taps"]
    bottom_edge_overrides = preflight["bottom_edge_overrides"]
    if TRACE_DIR:
        batch_id = _time.time_ns()
        for index, action in enumerate(prepared_actions, start=1):
            action["_after_image_path"] = os.path.join(
                TRACE_DIR, f"execute_ui_actions_{batch_id}_action_{index:02d}_after.png",
            )
    final_assertion = bool(args.get("final_assertion", False))
    final_postcondition = prepared_actions[-1].get("postcondition")
    if final_assertion and not final_postcondition:
        return _txt(_dynamic_preflight_error(
            actions, len(actions) - 1, actions[-1],
            "final_assertion=true 要求最后一个动作显式声明 postcondition",
        ))
    if final_assertion:
        # Private execution hint: when a final `any` condition combines a
        # positive semantic candidate with a weak visual/absence fallback, the
        # evaluator gives the semantic proof a brief grace period after the
        # weak condition succeeds. This does not change the public action schema.
        prepared_actions[-1]["_prefer_positive_semantic"] = True
    res = await _trace_action(
        "execute_ui_actions",
        lambda: server.execute_ui_steps(
            prepared_actions,
            float(args.get("default_timeout_s", 5.0)),
            int(args.get("poll_ms", 400)), 0,
            _stop_callback(),
        ),
        meta={"action_count": len(actions), "image_tap_count": len(image_taps)},
        include_after_path=True,
        reuse_step_after=True,
    )
    after_path = res.pop("_after_image_path", "")
    if not after_path:
        after_path, after_capture = await _capture_current_frame("execute_ui_actions_after")
        if not after_path and after_capture:
            res["after_capture"] = after_capture
    flow = res.pop("log", None)
    final_step = flow[-1] if flow else {}
    trusted_final_check = _has_trusted_positive_final_check(final_step)
    _strip_private_action_observations(flow)
    if image_taps:
        image_taps_by_i = {int(item["i"]): item for item in image_taps}
        for step in flow or []:
            declared = image_taps_by_i.get(int(step.get("i") or 0))
            if declared:
                step["action"] = "tap_image"
                step["target"] = str(actions[int(step["i"]) - 1].get("target") or "")
        failed_at = res.get("failed_at")
        if isinstance(failed_at, dict):
            declared = image_taps_by_i.get(int(failed_at.get("i") or 0))
            if declared:
                failed_at["action"] = "tap_image"
                failed_at["target"] = str(actions[int(failed_at["i"]) - 1].get("target") or "")
    evidence_paths = []
    if image_taps:
        res["image_tap_count"] = len(image_taps)
        res["coordinate_source"] = "current_visual_frame"
    if bottom_edge_overrides:
        res["bottom_edge_override_count"] = bottom_edge_overrides
    res["preflight_checked"] = preflight["preflight_checked"]
    if flow:
        for step in flow:
            capture = step.get("after_image")
            if (isinstance(capture, dict) and capture.get("bytes", 0) > 0
                    and capture.get("path")):
                evidence_paths.append(str(capture["path"]))
        evidence_path = _log_steps({
            "ts": round(_time.time(), 3), "source": "dynamic_actions",
            "ok": res.get("ok"), "stopped": res.get("stopped", False), "steps": flow,
            "image_taps": image_taps,
        })
        if evidence_path:
            evidence_paths.append(evidence_path)
        else:
            # 没有 Run 证据目录时，成功动作也必须保留逐步检查结果；不能只返回汇总。
            res["steps"] = flow
        if not res.get("ok"):
            res["log"] = flow
    failed_capture = (res.get("failed_at") or {}).get("after_image")
    if (isinstance(failed_capture, dict) and failed_capture.get("bytes", 0) > 0
            and failed_capture.get("path")):
        evidence_paths.append(str(failed_capture["path"]))
    if final_assertion and res.get("ok") and flow:
        last_step = flow[-1]
        if last_step.get("postcondition_satisfied") is True:
            res["postcondition_satisfied"] = True
            res["final_postcondition"] = last_step.get("postcondition")
            res["final_assertion_confirmed"] = bool(trusted_final_check)
        if res.get("final_assertion_confirmed") is True:
            _confirm_final_assertion("execute_ui_actions", res, args)
            assertion_path = str(res.pop("evidence_path", "") or "")
            if assertion_path:
                evidence_paths.append(assertion_path)
            else:
                # A semantic match without persisted structured evidence is not
                # sufficient to suppress the visual fallback or auto-record.
                res["final_assertion_confirmed"] = False
    if after_path:
        evidence_paths.append(after_path)
    res["evidence_paths"] = list(dict.fromkeys(evidence_paths))
    if res.get("final_assertion_confirmed") is True:
        # The final frame was deliberately not sent to the model, so an older
        # model-visible frame must not remain eligible for a later tap_image.
        LAST_SCREENSHOT_META = None
        return _txt(res)
    return _visual_result(res, after_path, "batch_after") if after_path else _txt(res)


@tool(
    "run_ui_flow",
    "按 xlsx『流程映射』一次执行完整固定流程。flow=流程名，vars=关联步骤脚本里的变量。"
    "执行器每到一个阶段只观察真实前台/a11y，按表内优先级选择脚本；低优先级页面会短暂沉降，"
    "允许稍后出现的高优先级弹窗或自动跳转抢占。任一脚本失败、页面无匹配或映射缺失都会立即返回"
    "结构化失败，不在执行器内猜测；此时可由 LLM 观察一次并接管。start_stage 通常省略并默认从『开始』执行，"
    "禁止根据当前页面文案猜阶段名。成功时返回精简摘要与 evidence_path。",
    _RUN_UI_FLOW_SCHEMA,
)
async def run_ui_flow(args):
    if await _gate():
        return _stopped_result()
    import uiscripts

    name = (args.get("flow") or "").strip()
    if not name or name not in UI_FLOWS:
        return _txt({"ok": False,
                     "error": f"没有名为 {name!r} 的流程映射" if name else "未指定 flow",
                     "available": sorted(UI_FLOWS)})
    routes = UI_FLOWS[name]
    variables = args.get("vars") or {}
    script_names = []
    for route in routes:
        script_name = str(route.get("script", "")).strip()
        if script_name and script_name not in script_names:
            script_names.append(script_name)
    missing_scripts = [script_name for script_name in script_names if script_name not in UI_SCRIPTS]
    if missing_scripts:
        return _txt({"ok": False, "error": f"流程 {name!r} 引用了不存在的步骤脚本: {missing_scripts}",
                     "missing_scripts": missing_scripts})
    needed = []
    resolved = {}
    for script_name in script_names:
        steps = UI_SCRIPTS[script_name]
        for variable in uiscripts.missing_vars(steps, variables):
            if variable not in needed:
                needed.append(variable)
        resolved[script_name] = uiscripts.resolve(steps, variables)
    if needed:
        return _txt({"ok": False, "error": f"流程 {name!r} 缺变量取值: {needed}", "needed": needed})

    res = await _run(
        server.run_ui_flow, routes, resolved, args.get("start_stage", "开始"),
        float(args.get("default_timeout_s", 5.0)), int(args.get("poll_ms", 400)),
        int(args.get("settle_ms", 500)), float(args.get("route_timeout_s", 8.0)),
        int(args.get("route_settle_ms", 2000)), int(args.get("max_routes", 20)),
        _stop_callback(),
    )
    route_log = res.pop("route_log", None)
    if route_log:
        evidence_path = _log_steps({"ts": round(_time.time(), 3), "flow": name,
                                    "ok": res.get("ok"), "routes": route_log})
        if evidence_path:
            res["evidence_path"] = evidence_path
    if not res.get("ok") and route_log is not None:
        res["route_log"] = route_log
    return _txt(res)


@tool(
    "tap_until_change",
    "反馈式点击:点(x,y)→截图比像素差→屏幕没变就在纵向上下微调再点,一变就停并回报命中点(不靠一次点准)。"
    "专治 webview/小程序自绘大按钮:视觉目测其像素中心常系统性偏低约百px、单次 tap 落到按钮下沿外死区不触发，且 a11y 又读不到该按钮。"
    "实测:合同'同意协议并确认'按钮,从目测中心往上每级50px、3次内命中。"
    "何时用:大按钮点了没反应、且 find/get_screen 读不到它(webview 自绘)——别原地重试同一坐标,把你目测的按钮中心 x,y 传进来让它带反馈找。"
    "x,y=你目测的按钮中心;step=每级微调像素(默认50);max_tries=最多点几次(默认6);settle_ms=每次点后等多久再判变化(默认800);diff_threshold=判定屏幕变化的像素差阈值(默认3)。"
    "候选点=先原点→逐级上移(偏低是主因)→末尾插下移兜底。返回{changed,hit_xy,tries,diff}:changed=true即在hit_xy命中;changed=false=全程没变(可能坐标区域完全不对/真无响应,再排查别硬试)。",
    {"x": int, "y": int, "step": int, "max_tries": int, "settle_ms": int, "diff_threshold": float},
)
async def tap_until_change(args):
    if await _gate():
        return _stopped_result()
    import tempfile
    x, y = int(args["x"]), int(args["y"])
    step = int(args.get("step", 50))
    max_tries = int(args.get("max_tries", 6))
    settle_ms = int(args.get("settle_ms", 800))
    thr = float(args.get("diff_threshold", 3.0))
    # 候选纵坐标阶梯:先原点→逐级上移(webview 底部按钮视觉常被读低→真按钮在上方,实证有效)→末位插下移兜底
    ladder = [0, -1, -2, -3, -4, +1, -5, +2]
    offs = [ladder[i] * step for i in range(min(max_tries, len(ladder)))]
    d = os.path.join(tempfile.gettempdir(), "tuc")
    os.makedirs(d, exist_ok=True)
    tried = []
    should_stop = _stop_callback()
    for i, off in enumerate(offs):
        if should_stop():
            return _txt({"changed": False, "stopped": True, "tries": len(tried),
                         "tried": tried})
        ty = y + off
        b = os.path.join(d, f"{i}_b.png")
        before = await _run(server.save_screenshot, b)
        await _run(server.tap, x, ty)
        condition = await _run(server._evaluate_postcondition, {
            "mode": "all", "settle_ms": settle_ms, "timeout_s": 0,
            "checks": [{"type": "screen_change", "diff_threshold": thr}],
        }, 0.001, before, False, False, "",
        should_stop)
        if condition.get("stopped"):
            return _txt({"changed": False, "stopped": True, "tries": len(tried),
                         "tried": tried})
        check = condition["checks"][0]
        diff = check.get("diff")
        tried.append({"xy": [x, ty], "diff": diff,
                      "postcondition_satisfied": condition["satisfied"]})
        if condition["satisfied"]:
            return _txt({"changed": True, "hit_xy": [x, ty], "tries": i + 1, "diff": diff})
    return _txt({"changed": False, "tries": len(tried), "tried": tried})


@tool("swipe", "从(x1,y1)滑到(x2,y2),duration_ms毫秒。滚动/翻页。滑后核验。", {"x1": int, "y1": int, "x2": int, "y2": int, "duration_ms": int})
async def swipe(args):
    if await _gate():
        return _stopped_result()
    x1, y1, x2, y2, ms = int(args["x1"]), int(args["y1"]), int(args["x2"]), int(args["y2"]), int(args.get("duration_ms", 300))
    return _txt(await _trace_action("swipe", lambda: server.swipe(x1, y1, x2, y2, ms), xy=(x1, y1), meta={"to": [x2, y2], "ms": ms}))


_REPEAT_SWIPE_SCHEMA = {
    "type": "object",
    "properties": {
        "count": {
            "type": "integer", "minimum": 1, "maximum": 20,
            "description": "本次原子调用内连续执行的普通滑动次数",
        },
        "x1": {"type": "integer", "minimum": 0, "description": "可选；观测所得起点 x"},
        "y1": {"type": "integer", "minimum": 0, "description": "可选；观测所得起点 y"},
        "x2": {"type": "integer", "minimum": 0, "description": "可选；观测所得终点 x"},
        "y2": {"type": "integer", "minimum": 0, "description": "可选；观测所得终点 y"},
        "duration_ms": {
            "type": "integer", "minimum": 1, "maximum": 5000, "default": 250,
            "description": "每次普通滑动的时长；省略使用快速滑动默认值",
        },
        "interval_ms": {
            "type": "integer", "minimum": 0, "maximum": 5000, "default": 80,
            "description": "相邻两次滑动之间的间隔；省略使用快速连续默认值",
        },
    },
    "required": ["count"],
    "additionalProperties": False,
}


@tool(
    "repeat_swipe",
    "一次调用连续执行 N 次普通滑动，并返回最后一次滑动后的当前画面供下一步判断。"
    "仅用于当前任务明确要求‘快速连续滑动/重复滚动’的普通可滚动页面；"
    "不得用于滑块、拖拽控件或必须持续按住的交互。"
    "坐标可全部省略以使用当前 viewport 的通用内容区比例；如显式提供，必须来自本次页面观测且四项齐全。",
    _REPEAT_SWIPE_SCHEMA,
)
async def repeat_swipe(args):
    if await _gate():
        return _stopped_result()
    count = int(args["count"])
    x1, y1 = args.get("x1"), args.get("y1")
    x2, y2 = args.get("x2"), args.get("y2")
    duration_ms = int(args.get("duration_ms", 250))
    interval_ms = int(args.get("interval_ms", 80))
    explicit_xy = (int(x1), int(y1)) if x1 is not None and y1 is not None else None
    should_stop = _stop_callback() if CONTROL is not None else None

    def execute_repeat_swipe():
        kwargs = {
            "count": count, "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "duration_ms": duration_ms, "interval_ms": interval_ms,
        }
        if CONTROL is None:
            return server.repeat_swipe(**kwargs)
        return server._repeat_swipe_impl(**kwargs, should_stop=should_stop)

    res = await _trace_action(
        "repeat_swipe",
        execute_repeat_swipe,
        xy=explicit_xy,
        meta={
            "count": count, "to": [x2, y2] if x2 is not None and y2 is not None else None,
            "duration_ms": duration_ms, "interval_ms": interval_ms,
        },
        include_after_path=True,
    )
    after_path = str(res.pop("_after_image_path", "") or "")
    if res.get("ok") is True and not after_path:
        after_path, capture = await _capture_current_frame("repeat_swipe_after")
        if not after_path:
            res["after_capture"] = capture
    if after_path:
        return _visual_result(res, after_path, "repeat_swipe_after")
    return _txt(res)


@tool(
    "drag",
    "真实长拖拽：先在(x1,y1)按住hold_ms，再持续duration_ms分段拖到(x2,y2)后释放。"
    "用于普通swipe无效、必须持续接触屏幕的下拉面板/拖拽控件；起点不要贴屏幕顶边，避免拉出系统通知栏。拖后核验。",
    {"x1": int, "y1": int, "x2": int, "y2": int, "hold_ms": int, "duration_ms": int, "steps": int},
)
async def drag(args):
    if await _gate():
        return _stopped_result()
    x1, y1 = int(args["x1"]), int(args["y1"])
    x2, y2 = int(args["x2"]), int(args["y2"])
    hold_ms = int(args.get("hold_ms", 500))
    duration_ms = int(args.get("duration_ms", 1200))
    steps = int(args.get("steps", 12))
    should_stop = _stop_callback() if CONTROL is not None else None

    def execute_drag():
        if CONTROL is None:
            return server.drag(x1, y1, x2, y2, hold_ms, duration_ms, steps)
        return server._drag_impl(
            x1, y1, x2, y2, hold_ms, duration_ms, steps, should_stop,
        )

    return _txt(await _trace_action(
        "drag", execute_drag,
        xy=(x1, y1), meta={"to": [x2, y2], "hold_ms": hold_ms,
                           "duration_ms": duration_ms, "steps": steps}))


@tool("tap_digits", "【所有屏上数字键盘的首选，不限验密】一次输入整串数字：验证码/OTP、支付密码、金额、手机号、PIN等，只要页面显示0-9可点键且普通 input action 不适用就先调用本工具。"
      "工具优先读取设备真实数字键坐标；安全窗口完全隐藏键盘时，仅在窗口名+分辨率精确命中已验证标定后自动输入，不做比例猜测。"
      "禁止先截图逐个tap；只有返回ok=false/找不到数字键后才按现场决定兜底。"
      "tap_confirm=True可在输完后点确认键。", {"digits": str, "tap_confirm": bool})
async def tap_digits(args):
    if await _gate():
        return _stopped_result()
    if CONTROL is None:
        result = await _run(
            server.tap_digits, args["digits"], bool(args.get("tap_confirm", False)),
        )
    else:
        result = await _run(
            server._tap_digits_impl, args["digits"], bool(args.get("tap_confirm", False)),
            "确定", _stop_callback(),
        )
    if not result.get("ok") and TRACE_DIR:
        path = os.path.join(TRACE_DIR, f"{_trace_n:02d}_tap_digits_failure.png")
        capture = await _run(server.save_screenshot, path)
        if capture.get("bytes", 0) > 0:
            return _visual_result(result, path, "failure_current_screen")
    return _txt(result)


@tool("long_press", "长按(x,y)。", {"x": int, "y": int, "duration_ms": int})
async def long_press(args):
    if await _gate():
        return _stopped_result()
    return _txt(await _run(server.long_press, int(args["x"]), int(args["y"]), int(args.get("duration_ms", 800))))


# 由 Web 运行器在每次 Native Harness 运行前注入输出目录、控制器等上下文。
RUN_CONTEXT: dict = {}


_ALL = [
    get_screen, find, get_focused_app, screenshot,
    tap_wait_tap, tap_until_change, wait_digits_leave,
    run_ui_steps, run_ui_flow, execute_ui_actions, swipe, repeat_swipe, drag,
    tap_digits, long_press,
]

# Optional preparation tools share the same instrumentation and permission path.
try:
    from preparation.tool import PREPARATION_TOOLS
except ModuleNotFoundError as exc:
    if exc.name not in {"preparation", "preparation.tool"}:
        raise
else:
    _ALL += PREPARATION_TOOLS
    _REQUIRED_FLOW_READ_TOOLS.add("check_environmentironment")

# 原始流A：全部工具统一插桩(含插件工具)。零侵入,TRACE_DIR 未设(对话档/CLI)不写盘。
for _t in _ALL:
    _instrument(_t)


def device_server():
    """返回 Native Harness 唯一设备工具集。"""
    srv = create_sdk_mcp_server(name="device", version="0.1.0", tools=_ALL)
    allowed = [f"mcp__device__{t.name}" for t in _ALL]
    return srv, allowed
