"""
device-mcp: 手眼工具层（adb + uiautomator2）给 Claude 大脑用。

设计原则（针对 mobile-use 三崩点）：
- 用未压缩 a11y 树（compressed=False），修复 webview 元素 bounds 塌缩成整屏的问题。
- 工具只做"手眼"，不替大脑判定成功/失败——避免静默谎报。tap 后由大脑自行 get_screen 核验。
- 截图走 adb screencap 落盘，0 字节即 FLAG_SECURE 安全窗口（大脑改用 a11y 取证）。

device serial 由环境变量 MINIAPP_PILOT_DEVICE_SERIAL 或 Companion 显式指定；未指定时只允许恰好一台 ready 设备。
"""

import os
import json
import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

import uiautomator2 as u2
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("device-mcp")

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
from agent.runtime_paths import data_root
_KEYPAD_CALIBRATIONS = str(data_root() / "keypad-calibrations.json")
_WEBVIEW_POSTCONDITION_TIMEOUT_S = 3.0
_FINAL_SEMANTIC_GRACE_S = 5.0


def _serial(retries: int = 4) -> str:
    s = os.environ.get("MINIAPP_PILOT_DEVICE_SERIAL")
    if s:
        return s
    # 对 adb 瞬时抖动（devices 一时为空）重试，避免首次工具调用偶发失败。
    for i in range(retries):
        result = subprocess.run(
            ["adb", "devices", "-l"], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        ready = []
        for line in (result.stdout or "").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                ready.append(parts[0])
        if len(ready) == 1:
            return ready[0]
        if len(ready) > 1:
            raise RuntimeError("检测到多台 adb 设备，请先在 Companion 中明确选择")
        time.sleep(0.5 * (i + 1))
    raise RuntimeError("没有已连接的 adb 设备")


_SERIAL: str | None = None       # 懒解析:import 期不连设备(让纯逻辑单测能脱离设备跑)
_DEV: u2.Device | None = None
_RESTART_UIAUTOMATOR = False


def configure_device(serial: str) -> None:
    """显式切换 executor 绑定设备，并清理旧 uiautomator2 连接缓存。"""
    value = (serial or "").strip()
    if not value or any(ch.isspace() for ch in value):
        raise ValueError("设备 serial 无效")
    global _SERIAL, _DEV, _RESTART_UIAUTOMATOR
    os.environ["MINIAPP_PILOT_DEVICE_SERIAL"] = value
    _SERIAL = value
    _DEV = None
    _RESTART_UIAUTOMATOR = False


def invalidate_device_session(serial: str) -> None:
    """保留明确绑定的设备身份，并在下一次使用前重启 uiautomator。"""
    global _RESTART_UIAUTOMATOR
    configure_device(serial)
    _RESTART_UIAUTOMATOR = True


def clear_device() -> None:
    """设备断开或取消选择时清理绑定，防止后续动作落到旧缓存。"""
    global _SERIAL, _DEV, _RESTART_UIAUTOMATOR
    os.environ.pop("MINIAPP_PILOT_DEVICE_SERIAL", None)
    _SERIAL = None
    _DEV = None
    _RESTART_UIAUTOMATOR = False


def _serial_cached() -> str:
    """懒解析并缓存 device serial——首次用到设备时才跑 adb devices,而非 import 时。"""
    global _SERIAL
    if _SERIAL is None:
        _SERIAL = _serial()
    return _SERIAL


def _dev() -> u2.Device:
    global _DEV, _RESTART_UIAUTOMATOR
    if _DEV is None:
        device = u2.connect(_serial_cached())
        if _RESTART_UIAUTOMATOR:
            device.reset_uiautomator()
            _RESTART_UIAUTOMATOR = False
        _DEV = device
    return _DEV


def _adb(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["adb", "-s", _serial_cached(), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def _parse_wm_size(output: str) -> tuple[int, int]:
    """优先取 Override size；没有 override 才用 Physical size。"""
    physical = override = fallback = None
    for line in (output or "").splitlines():
        m = re.search(r"(\d+)x(\d+)", line)
        if not m:
            continue
        size = (int(m.group(1)), int(m.group(2)))
        fallback = size
        label = line.split(":", 1)[0].lower()
        if "override" in label:
            override = size
        elif "physical" in label:
            physical = size
    return override or physical or fallback or (0, 0)


def _wm_size() -> tuple[int, int]:
    return _parse_wm_size(_adb("shell", "wm", "size").stdout)


def _short_class(c: str) -> str:
    return c.rsplit(".", 1)[-1] if c else ""


def _dump_hierarchy(retries: int = 4) -> str:
    """拉未压缩 hierarchy，对 atx-agent 偶发断连(RemoteDisconnected/连接错)重试+重连。"""
    global _DEV
    last: Exception | None = None
    for i in range(retries):
        try:
            return _dev().dump_hierarchy(compressed=False)
        except Exception as e:  # RemoteDisconnected / ConnectionError / http 等
            last = e
            _DEV = None  # 强制下次重连
            time.sleep(0.5 * (i + 1))
    raise last if last else RuntimeError("dump_hierarchy 失败")


def _webview_surface(elements: list[dict]) -> bool:
    """webview/canvas 面判据（app 无关）：a11y 里出现 web 页节点——content_desc 含
    网页路径/URL（.html / http），或 WebView 类节点——说明当前是 webview 渲染面，
    a11y 很可能是背景层/滞后层，点击与核验应以截图像素为准。
    （不用 bounds==整屏 判据：真机 webview 容器多为 [0,0,w,h-导航栏]，塌不到整屏、会漏判。）"""
    for e in elements:
        desc = (e.get("content_desc") or "").lower()
        if ".html" in desc or "http://" in desc or "https://" in desc:
            return True
        if "webview" in (e.get("class") or "").lower():
            return True
    return False


def _screen_elements(filter_useful: bool = True, max_elements: int = 120) -> tuple[int, int, list[dict]]:
    """内部：拉【未压缩】hierarchy，解析成扁平元素列表。返回 (w, h, elements)。"""
    w, h = _wm_size()
    xml = _dump_hierarchy()
    elements: list[dict] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return w, h, elements

    for node in root.iter("node"):
        a = node.attrib
        text = a.get("text", "")
        rid = a.get("resource-id", "")
        desc = a.get("content-desc", "")
        clickable = a.get("clickable") == "true"
        scrollable = a.get("scrollable") == "true"
        if filter_useful and not (text or rid or desc or clickable or scrollable):
            continue
        m = _BOUNDS_RE.search(a.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        unreliable = (x1, y1, x2, y2) == (0, 0, w, h) and w and h
        elements.append(
            {
                "text": text,
                "resource_id": rid,
                "content_desc": desc,
                "class": _short_class(a.get("class", "")),
                "clickable": clickable,
                "scrollable": scrollable,
                "bounds": [x1, y1, x2, y2],
                "center": [(x1 + x2) // 2, (y1 + y2) // 2],
                "bounds_unreliable": bool(unreliable),
            }
        )
        if len(elements) >= max_elements:
            break
    return w, h, elements


def _calibrated_digit_keymap(focused_app: str, width: int, height: int) -> tuple[dict[str, list[int]], str]:
    """读取由真机成功样本标定的数字键坐标；必须精确匹配窗口和分辨率。"""
    key = f"{focused_app}|{width}x{height}"
    try:
        with open(_KEYPAD_CALIBRATIONS, encoding="utf-8") as f:
            raw = json.load(f)
        candidate = raw.get(key, {}).get("keys", {})
        if set(candidate) != set("0123456789"):
            return {}, key
        keymap = {digit: [int(xy[0]), int(xy[1])] for digit, xy in candidate.items()}
        if any(x < 0 or y < 0 or x >= width or y >= height for x, y in keymap.values()):
            return {}, key
        return keymap, key
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}, key


@mcp.tool()
def get_screen(filter_useful: bool = True, max_elements: int = 120) -> dict:
    """
    取当前屏幕的无障碍(a11y)元素树 + 当前前台应用。

    用【未压缩】hierarchy，bounds 准确（修复 webview 元素 bounds 塌缩成整屏的问题）。
    每个元素含: text, resource_id, content_desc, class, clickable, scrollable,
    bounds [x1,y1,x2,y2], center [x,y], 以及 bounds_unreliable（bounds 等于整屏时为 true，
    提示该元素坐标不可信、应改用 resource_id/text 选择器或视觉定位）。

    filter_useful=True 时只返回有 text/resource_id/content_desc 或可点/可滚的元素（省 token）。
    不返回截图；需要看图用 save_screenshot。
    """
    w, h, elements = _screen_elements(filter_useful, max_elements)
    return {
        "focused_app": get_focused_app(),
        "width": w,
        "height": h,
        "element_count": len(elements),
        "elements": elements,
    }


@mcp.tool()
def find(text: str = "", resource_id: str = "", clickable_only: bool = False,
         final_assertion: bool = False, visual_on_miss: bool = False) -> dict:
    """
    定向查找元素（token 轻量：只回匹配项，不回整棵树）。
    text 为子串匹配，resource_id 为后缀匹配。返回每个匹配的 center/bounds/clickable/bounds_unreliable。
    用于"点之前先确认元素在不在、坐标可不可信"，替代拉整屏再人肉筛。
    final_assertion 只表示“本次命中足以完整证明调用方的最终观测点”；命中时返回
    final_assertion_confirmed，由上层生成结构化证据并阻止同一步冗余截图。
    visual_on_miss 请求上层在未命中时于同一次工具结果附当前画面；server 只记录该决定，
    截图和多模态结果仍由 agent/tools.py 统一生成。
    """
    _, _, elements = _screen_elements(filter_useful=True, max_elements=300)
    matches = []
    for e in elements:
        if text and text not in e["text"] and text not in e["content_desc"]:
            continue
        if resource_id and not e["resource_id"].endswith(resource_id):
            continue
        if clickable_only and not e["clickable"]:
            continue
        matches.append(
            {
                "text": e["text"],
                "resource_id": e["resource_id"],
                "center": e["center"],
                "bounds": e["bounds"],
                "clickable": e["clickable"],
                "bounds_unreliable": e["bounds_unreliable"],
            }
        )
    result = {
        "query": {"text": text, "resource_id": resource_id},
        "count": len(matches), "matches": matches,
    }
    if not matches and visual_on_miss:
        result["visual_fallback_requested"] = True
    if not matches and _webview_surface(elements):
        result["webview_surface"] = True
        result["hint"] = (
            "当前是 webview/canvas 面，a11y 未命中不代表目标不在画面；"
            "上层会尽量在本次结果中直接附当前画面，请转视觉判断且不要重复相同 find/wait。"
        )
    if final_assertion and matches:
        result["final_assertion_confirmed"] = True
    return result


def wait_for(text: str = "", resource_id: str = "", timeout_s: float = 10.0,
             poll_s: float = 0.6, webview_timeout_s: float = 3.0,
             final_assertion: bool = False) -> dict:
    """
    轮询等待某文本/元素出现（页面稳定后再判断），替代固定 sleep + 反复重拉。
    text 子串匹配，resource_id 后缀匹配。出现即返回 {found:true, waited_s, center, focused_app}；
    超时返回 {found:false, waited_s}。专治 webview 渲染滞后导致的"读太早→读到旧页/空"。

    【webview 自动短超时】当前若是 webview/canvas 面（预期文本本就永不进 a11y），最多等
    min(timeout_s, webview_timeout_s)≈3s 就带 webview_surface 提示返回，不白等满 timeout_s。
    原生页仍按完整 timeout_s。超时非致命（返回 found:false，调用方 fallback 截图即可），
    故此上限对原生页零风险、只掐 webview 空转。

    final_assertion 只表示“本次命中足以完整证明调用方的最终观测点”；命中时返回
    final_assertion_confirmed。未命中仍保留原有截图兜底语义。
    """
    import time as _t

    start = _t.monotonic()
    while True:
        _, _, elements = _screen_elements(filter_useful=True, max_elements=300)
        for e in elements:
            if text and (text in e["text"] or text in e["content_desc"]):
                result = {"found": True, "waited_s": round(_t.monotonic() - start, 2),
                          "center": e["center"], "bounds_unreliable": e["bounds_unreliable"],
                          "focused_app": get_focused_app()}
                if final_assertion:
                    result["final_assertion_confirmed"] = True
                return result
            if resource_id and e["resource_id"].endswith(resource_id):
                result = {"found": True, "waited_s": round(_t.monotonic() - start, 2),
                          "center": e["center"], "bounds_unreliable": e["bounds_unreliable"],
                          "focused_app": get_focused_app()}
                if final_assertion:
                    result["final_assertion_confirmed"] = True
                return result
        elapsed = _t.monotonic() - start
        is_webview = _webview_surface(elements)
        cap = min(timeout_s, webview_timeout_s) if is_webview else timeout_s
        if elapsed >= cap:
            r = {"found": False, "waited_s": round(elapsed, 2),
                 "focused_app": get_focused_app()}
            if is_webview:
                r["webview_surface"] = True
                r["hint"] = ("当前是 webview/canvas 面，a11y 可能是背景层/滞后层——预期文本没出现≠没到页。"
                             "请 screenshot 以像素为准，别重试点击、别据此判失败。")
            return r
        _t.sleep(poll_s)


@mcp.tool()
def get_focused_app() -> str:
    """返回当前前台窗口(mCurrentFocus)，形如 包名/Activity。"""
    out = _adb("shell", "dumpsys", "window").stdout or ""
    for line in out.splitlines():
        if "mCurrentFocus" in line:
            m = re.search(r"mCurrentFocus=\S+ \S+ (\S+)}", line)
            return m.group(1) if m else line.strip()
    return ""


@mcp.tool()
def save_screenshot(path: str) -> dict:
    """
    截图存到 path(PNG)。返回 {path, bytes, secure}。
    bytes==0 即 FLAG_SECURE 安全窗口(如微信支付密码页)截不到图，此时应靠 get_screen 的 a11y 取证。
    """
    # 二进制取 PNG（text 模式会损坏二进制）
    result = subprocess.run(
        ["adb", "-s", _serial_cached(), "exec-out", "screencap", "-p"], capture_output=True
    )
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"设备截图失败: {detail or 'ADB 连接异常'}")
    raw = result.stdout
    with open(path, "wb") as f:
        f.write(raw)
    return {"path": path, "bytes": len(raw), "secure": len(raw) == 0}


def tap(x: int, y: int) -> dict:
    """在坐标 (x,y) 点击。不判定是否成功——请随后 get_screen 自行核验界面变化。"""
    _adb("shell", "input", "tap", str(x), str(y))
    return {"action": "tap", "x": x, "y": y}


def _tap_wait_tap_impl(tap1_x: int, tap1_y: int, focus_contains: str,
                       tap2_x: int, tap2_y: int, timeout_s: float = 10.0,
                       poll_ms: int = 200, settle_ms: int = 500,
                       pre_tap2_ms: int = 0, tap1_target: str = "",
                       tap2_target: str = "", tap2_fallback: str = "",
                       should_stop=None) -> dict:
    """
    时序关键动作原子化：点(tap1) → 紧轮询前台窗口(focused_app)直到含 focus_contains 子串（触发页出现）
    → 【等 pre_tap2_ms 让目标按钮渲染好】→ 反复点(tap2)【直到前台离开触发页】才停。
    全程在本调用内、中间【不回大脑(LLM)】，消除"感知→决策→动作"往返延迟。
    专治转瞬即逝、会超时的浮层/原生控件（如刷脸相机页 FaceFlashUI：点"下一步"调起它、必须点左上×取消）。

    为何反复点 tap2：焦点切到触发页 ≠ 目标按钮已可交互（相机页焦点 0.8s 就到、但×要等渲染）。
    单点一次常踩在"×还没活"的空档而落空。故点完回读焦点，没离开触发页就每隔 settle_ms 再点，直到离开或超时。
    pre_tap2_ms：触发页出现后、首次点 tap2 前先等这么多毫秒（默认 0）。嫌"太快、×还没活"就调大它，
    让第一下就落在可交互的按钮上（比靠 settle_ms 反复补点更省、更稳）。

    tap1 传 (-1,-1) 可跳过第一次点击（目标页已被上一步调起、只需"等+点"）。
    返回 {matched, dismissed, taps, waited_s, focus_before, focus_at_trigger, focus_after}：
      matched=false → timeout_s 内触发页始终没出现（tap2 未点，focus_last 给最后所见）；
      matched=true 且 dismissed=false → 触发页出现了但点了 taps 次仍没离开（×坐标可能不对/该页需别的退出方式）。
    app 无关：触发子串、坐标、各等待值都是参数，不含任何具体 app 逻辑。
    """
    import time as _t
    if _stop_requested(should_stop):
        return {"matched": False, "stopped": True, "taps": 0}
    focus_before = get_focused_app()
    # tap1 优先用选择器（tap1_target，写法同 run_ui_steps：默认文字 / rid:xxx / xy:x,y）。
    # 只读【触发页出现之前】的这一屏——被禁止的是在触发页(如相机页)上读屏，这里读是安全的。
    # 不传 tap1_target 就完全走老路（坐标），行为逐字节不变。
    tap1_via = "none"
    if tap1_target:
        kind, val = _resolve_target(tap1_target)
        if kind == "xy":
            _x, _y = _point(val)
            _adb("shell", "input", "tap", str(_x), str(_y))
            tap1_via = "xy"
        else:
            selector1 = {}
            if kind == "rid":
                selector1["resource_id"] = val
            elif kind == "desc":
                selector1["content_desc"] = val
            else:
                selector1["text"] = val
                selector1["contains"] = kind == "contains"
            r1 = tap_element(**selector1)
            if not r1.get("ok"):
                return {"matched": False, "tap1_via": kind, "tap1_ok": False,
                        "error": f"tap1 选择器定位不到 {tap1_target!r}: {r1.get('error')}",
                        "focus_before": focus_before, "focus_last": get_focused_app()}
            tap1_via = kind
    elif tap1_x >= 0 and tap1_y >= 0:
        _adb("shell", "input", "tap", str(tap1_x), str(tap1_y))
        tap1_via = "xy"
    start = _t.monotonic()
    poll_s = max(poll_ms, 1) / 1000.0

    # 阶段1：等触发页出现；判断复用统一 postcondition evaluator。
    trigger_condition = _evaluate_postcondition({
        "mode": "all", "settle_ms": 0, "timeout_s": timeout_s,
        "checks": [{"type": "focus", "value": focus_contains, "state": "present"}],
    }, poll_s=poll_s, should_stop=should_stop)
    trigger_fa = str(trigger_condition.get("focused_app") or "")
    if trigger_condition.get("stopped"):
        return {"matched": False, "stopped": True, "tap1_via": tap1_via,
                "focus_before": focus_before, "focus_last": trigger_fa,
                "trigger_postcondition": trigger_condition}
    if not trigger_condition["satisfied"]:
        return {"matched": False, "tap1_via": tap1_via, "waited_s": round(_t.monotonic() - start, 2),
                "focus_before": focus_before, "focus_last": trigger_fa,
                "trigger_postcondition": trigger_condition}

    # 触发页出现后、点 tap2 前先等一会（让目标按钮渲染成可交互，治"太快点空"）
    if pre_tap2_ms > 0 and not _interruptible_sleep(
            pre_tap2_ms / 1000.0, should_stop):
        return {"matched": True, "stopped": True, "tap1_via": tap1_via,
                "dismissed": False, "taps": 0, "focus_before": focus_before,
                "focus_at_trigger": trigger_fa}

    # 阶段2：点 tap2 直到前台离开触发页（治"×还没可交互"→单点落空）
    # tap2 定位在此解析【一次】。三种写法：
    #   选择器("取消" / "~:片段" / "rid:xxx")→ 在触发页读【一次】屏取 center（实测相机页 dump 仅 ~281ms）；
    #   "pct:5,5.73" → 按屏幕百分比换算（只读 wm size，换分辨率不废）；"54,134" → 绝对像素。
    # 【为何读一次屏是安全的】烧掉相机页窗口的是"AI 每摸一次都走一个 LLM 往返(≈5s)"，不是这一次 dump。
    # 选择器没命中且给了 tap2_fallback 时退回坐标，并把实际走的路径回报出来（tap2_via），不静默兜底。
    kind2, val2 = _resolve_target(tap2_target) if tap2_target else ("xy", f"{tap2_x},{tap2_y}")
    tap2_via = kind2
    if kind2 == "xy":
        t2x, t2y = _point(val2)
    else:
        _, _, _els2 = _screen_elements(filter_useful=True, max_elements=300)
        hit = None
        for e in _els2:
            if kind2 == "rid" and e["resource_id"].endswith(val2):
                hit = e; break
            if kind2 == "desc" and e["content_desc"] == val2:
                hit = e; break
            if kind2 == "text" and val2 in (e["text"], e["content_desc"]):
                hit = e; break
            if kind2 == "contains" and (val2 in e["text"] or val2 in e["content_desc"]):
                hit = e; break
        if hit is not None:
            t2x, t2y = hit["center"]
        elif tap2_fallback:
            t2x, t2y = _point(_resolve_target(tap2_fallback)[1])
            tap2_via = "fallback"
        else:
            return {"matched": True, "tap1_via": tap1_via, "tap2_via": kind2, "tap2_ok": False,
                    "dismissed": False, "taps": 0,
                    "error": f"触发页上定位不到 tap2 目标 {tap2_target!r}，且未给 tap2_fallback",
                    "waited_s": round(_t.monotonic() - start, 2),
                    "focus_before": focus_before, "focus_at_trigger": trigger_fa,
                    "focus_after": get_focused_app()}
    taps = 0
    fa = trigger_fa
    leave_condition = {
        "satisfied": False, "mode": "all", "checks": [], "waited_ms": 0,
        "focused_app": fa,
    }
    while _t.monotonic() - start < timeout_s:
        if _stop_requested(should_stop):
            return {"matched": True, "stopped": True, "tap1_via": tap1_via,
                    "tap2_via": tap2_via, "dismissed": False, "taps": taps,
                    "focus_before": focus_before, "focus_at_trigger": trigger_fa,
                    "focus_after": fa}
        _adb("shell", "input", "tap", str(t2x), str(t2y))
        taps += 1
        leave_condition = _evaluate_postcondition({
            "mode": "all", "settle_ms": max(settle_ms, 1), "timeout_s": 0,
            "checks": [{"type": "focus", "value": focus_contains, "state": "absent"}],
        }, poll_s=poll_s, should_stop=should_stop)
        fa = str(leave_condition.get("focused_app") or "")
        if leave_condition.get("stopped"):
            return {"matched": True, "stopped": True, "tap1_via": tap1_via,
                    "tap2_via": tap2_via, "dismissed": False, "taps": taps,
                    "focus_before": focus_before, "focus_at_trigger": trigger_fa,
                    "focus_after": fa, "leave_postcondition": leave_condition}
        if leave_condition["satisfied"]:                  # 已离开触发页=取消生效
            return {"matched": True, "tap1_via": tap1_via, "tap2_via": tap2_via, "tap2_xy": [t2x, t2y], "dismissed": True, "taps": taps,
                    "waited_s": round(_t.monotonic() - start, 2),
                    "focus_before": focus_before, "focus_at_trigger": trigger_fa, "focus_after": fa,
                    "trigger_postcondition": trigger_condition,
                    "leave_postcondition": leave_condition}
    return {"matched": True, "tap1_via": tap1_via, "tap2_via": tap2_via, "tap2_xy": [t2x, t2y], "dismissed": False, "taps": taps,
            "waited_s": round(_t.monotonic() - start, 2),
            "focus_before": focus_before, "focus_at_trigger": trigger_fa, "focus_after": fa,
            "trigger_postcondition": trigger_condition,
            "leave_postcondition": leave_condition}


@mcp.tool()
def tap_wait_tap(tap1_x: int, tap1_y: int, focus_contains: str,
                 tap2_x: int, tap2_y: int, timeout_s: float = 10.0,
                 poll_ms: int = 200, settle_ms: int = 500, pre_tap2_ms: int = 0,
                 tap1_target: str = "", tap2_target: str = "",
                 tap2_fallback: str = "") -> dict:
    """点击、等待触发页、再点击直至离开触发页。"""
    return _tap_wait_tap_impl(
        tap1_x, tap1_y, focus_contains, tap2_x, tap2_y, timeout_s, poll_ms,
        settle_ms, pre_tap2_ms, tap1_target, tap2_target, tap2_fallback,
    )


def _wait_digits_leave_impl(digits: str, page_text: str = "",
                            page_focus_contains: str = "", timeout_s: float = 10.0,
                            leave_timeout_s: float = 8.0, poll_ms: int = 400,
                            tap_confirm: bool = False, confirm_text: str = "确定",
                            after_screenshot_path: str = "", should_stop=None) -> dict:
    """
    「数字输入页」原子化：等目标页就绪 → 在【app 自绘的屏上数字键盘】逐位输入 → 等前台离开该页 → 报落到哪。
    全程在本调用内、中间【不回大脑(LLM)】，把"看页→输入→等结果"三个往返压成一次。

    适用：任何"出现一个需要输入 N 位数字的页、输完就跳走"的场景。到页判据用参数给：
      page_text            —— 该页上应出现的文本（a11y 子串匹配）
      page_focus_contains  —— 该页的前台窗口名子串
    两者至少给一个；都给则【都满足】才算到页（更稳）。离开判据是它的反面（不再同时满足）——
    比"等下一页的预期文本"可靠得多：下一页常是 webview 自绘面，预期文本根本进不了 a11y。

    三条硬纪律（防谎报、防闯祸）：
      1. 没确认到页 → 一位数字都不点，返回 page_matched=false，交回大脑。
      2. 键盘不在 a11y 且也没有精确真机标定 → 不点，返回 typed=false + available_keys，交回大脑判断。
      3. 输完没走掉 → 如实返回 left=false，【绝不重输】。密码类重复输入有锁定风险，交回大脑决定。

    返回 {page_matched, typed, digits_n, left, focus_before, focus_at_page, focus_after,
          waited_s, leave_waited_s}；typed=false 时附 tap_digits 的错误信息。
    app 无关：到页判据、数字、各超时都是参数，不含任何具体 app 的逻辑、文案或坐标。
    """
    import time as _t

    if _stop_requested(should_stop):
        return {"page_matched": False, "typed": False, "left": False,
                "stopped": True}
    if not digits or not digits.isdigit():
        return {"ok": False, "error": "digits 必须是只含 0-9 的字符串"}
    if not page_text and not page_focus_contains:
        return {"ok": False, "error": "须给 page_text 或 page_focus_contains 之一（用于确认已到目标输入页）"}

    focus_before = get_focused_app()
    poll_s = max(poll_ms, 1) / 1000.0
    page_checks = []
    if page_text:
        page_checks.append({
            "type": "a11y", "selector": "text_or_content_desc", "value": page_text,
            "match": "contains", "state": "present",
        })
    if page_focus_contains:
        page_checks.append({
            "type": "focus", "value": page_focus_contains, "state": "present",
        })

    # 阶段1：等目标输入页就绪（没到页就一位都不点）
    arrival = _evaluate_postcondition({
        "mode": "all", "settle_ms": 0, "timeout_s": timeout_s, "checks": page_checks,
    }, poll_s=poll_s, should_stop=should_stop)
    waited_s = round(arrival.get("waited_ms", 0) / 1000.0, 2)
    fa = str(arrival.get("focused_app") or "")
    if arrival.get("stopped"):
        return {"page_matched": False, "typed": False, "left": False,
                "stopped": True, "waited_s": waited_s, "focus_before": focus_before,
                "focus_last": fa, "arrival_postcondition": arrival}
    if not arrival["satisfied"]:
        return {"page_matched": False, "typed": False, "left": False, "waited_s": waited_s,
                "focus_before": focus_before, "focus_last": fa,
                "arrival_postcondition": arrival,
                "hint": "未确认到目标输入页，未输入任何数字。请自行观察当前页面后再决定。"}

    # 阶段2：逐位输入（键盘读不到就不点，如实回报）
    focus_at_page = fa
    if should_stop is None:
        res = tap_digits(
            digits, tap_confirm=tap_confirm, confirm_text=confirm_text,
        )
    else:
        res = _tap_digits_impl(
            digits, tap_confirm=tap_confirm, confirm_text=confirm_text,
            should_stop=should_stop,
        )
    if res.get("stopped"):
        return {"page_matched": True, "typed": False, "left": False,
                "stopped": True, "waited_s": waited_s,
                "focus_before": focus_before, "focus_at_page": focus_at_page,
                "digit_result": res}
    if not res.get("ok"):
        out = {"page_matched": True, "typed": False, "left": False, "waited_s": waited_s,
               "focus_before": focus_before, "focus_at_page": focus_at_page,
               "hint": "已到页但 a11y 与精确真机标定都定位不到数字键，未输入。截图可见才做视觉兜底；FLAG_SECURE 截不到时禁止盲点。"}
        for k in ("error", "available_keys"):
            if k in res:
                out[k] = res[k]
        return out

    # 阶段3：等前台离开该页（离开=不再满足到页判据）。没走掉如实报，绝不重输
    leave_checks = [{**check, "state": "absent"} for check in page_checks]
    leave = _evaluate_postcondition({
        "mode": "any", "settle_ms": 0, "timeout_s": leave_timeout_s,
        "checks": leave_checks,
    }, poll_s=poll_s, should_stop=should_stop)
    fa = str(leave.get("focused_app") or "")
    left = bool(leave["satisfied"])
    out = {"page_matched": True, "typed": True, "digits_n": len(digits), "left": left,
           "waited_s": waited_s, "leave_waited_s": round(leave.get("waited_ms", 0) / 1000.0, 2),
           "focus_before": focus_before, "focus_at_page": focus_at_page, "focus_after": fa}
    out["arrival_postcondition"] = arrival
    out["leave_postcondition"] = leave
    if leave.get("stopped"):
        out["stopped"] = True
        out["hint"] = "数字已输入，Stop 后未继续等待或执行任何后续动作；请从真实页面恢复。"
        return out
    if not left:
        out["hint"] = ("已输完但页面没走掉——可能输入有误、或该键盘还需点确认键。"
                       "请 screenshot 看页面提示后再决定，【不要重复输入】。")
    else:
        # 结果 toast/半屏浮层可能只活一两秒；必须在检测到离页的同一原子调用内抢首帧，
        # 不能等 LLM 下一轮再截图。路径由 runner 按当前 case 注入，普通 MCP 调用可留空。
        if after_screenshot_path:
            out["after_screenshot"] = save_screenshot(after_screenshot_path)
        try:
            _, _, after_elements = _screen_elements(filter_useful=True, max_elements=160)
            texts = []
            for e in after_elements:
                for value in (e.get("text", ""), e.get("content_desc", "")):
                    if value and value not in texts:
                        texts.append(value)
            out["after_texts"] = texts[:40]
        except Exception as e:
            out["after_capture_warn"] = str(e)
    return out


@mcp.tool()
def wait_digits_leave(digits: str, page_text: str = "", page_focus_contains: str = "",
                      timeout_s: float = 10.0, leave_timeout_s: float = 8.0,
                      poll_ms: int = 400, tap_confirm: bool = False,
                      confirm_text: str = "确定", after_screenshot_path: str = "") -> dict:
    """等待数字输入页、输入一次并等待离开；不会重复输入。"""
    return _wait_digits_leave_impl(
        digits, page_text, page_focus_contains, timeout_s, leave_timeout_s,
        poll_ms, tap_confirm, confirm_text, after_screenshot_path,
    )


_STEP_ACTIONS = ("tap_text", "tap_text_retry", "tap_xy", "input", "clear_input", "swipe_up", "swipe_down",
                 "press_key", "wait", "ok_if_text", "launch_app", "stop_app")


def _resolve_target(t: str) -> tuple[str, str]:
    """目标写法：默认按文字；rid:xxx 按 resource_id 后缀；desc:xxx 按 content-desc 精确匹配；
    row:文字 按文字定位纵坐标后点击整行中部（用于文字节点本身不可点击的列表行）；
    xy:540,1746 绝对像素；pct:5,5.73 按屏幕百分比。
    返回 (kind, 值)。pct 保留前缀，交给 _point 换算。"""
    s = (t or "").strip()
    if s.startswith("rid:"):
        return "rid", s[4:].strip()
    if s.startswith("desc:"):
        return "desc", s[5:].strip()
    if s.startswith("row:"):
        return "row", s[4:].strip()
    if s.startswith("xy:"):
        return "xy", s[3:].strip()
    if s.startswith("pct:"):
        return "xy", s                      # 前缀留着，_point 认它
    if s.startswith("~:"):
        return "contains", s[2:].strip()    # 子串匹配（目标只是某长节点的一部分时用）
    return "text", s


def _point(spec: str) -> tuple[int, int]:
    """把坐标写法换成设备像素。
      '540,1746'    → 绝对像素（绑定当前分辨率，换手机就废）
      'pct:5,5.73'  → 屏幕宽/高的百分比 → 换算成像素（换分辨率仍可用，推荐用于必须写坐标的场合）
    只读 wm size，不碰无障碍树——所以在"禁止读屏"的页（相机页等）上也安全。"""
    t = spec.strip()
    if t.startswith("pct:"):
        a, _, b = t[4:].partition(",")
        w, h = _wm_size()
        return int(round(w * float(a) / 100)), int(round(h * float(b) / 100))
    a, _, b = t.partition(",")
    return int(float(a)), int(float(b))


def _await_expect(expect: str, timeout_s: float, poll_s: float,
                  should_stop=None) -> tuple[bool, str]:
    """固定 Excel 的 expect 兼容入口；实际判断统一委托给 postcondition evaluator。"""
    result = _evaluate_postcondition(
        _legacy_expect_postcondition(expect, timeout_s), poll_s=poll_s,
        should_stop=should_stop,
    )
    return bool(result["satisfied"]), str(result.get("focused_app") or "")


def _legacy_expect_postcondition(expect: str, timeout_s: float) -> dict | None:
    """把固定脚本的旧 expect 规范化；动态工具不接受该输入。"""
    value = str(expect or "").strip()
    if not value:
        return None
    if value.startswith("focus:"):
        check = {"type": "focus", "value": value[6:].strip(), "state": "present"}
    else:
        check = {
            "type": "a11y", "selector": "text_or_content_desc", "value": value,
            "match": "contains", "state": "present",
        }
    return {"mode": "all", "settle_ms": 0, "timeout_s": timeout_s, "checks": [check]}


def _match_a11y_value(actual: str, expected: str, match: str) -> bool:
    if match == "exact":
        return actual == expected
    if match == "suffix":
        return actual.endswith(expected)
    return expected in actual


def _capture_postcondition_screen() -> dict:
    fd, path = tempfile.mkstemp(prefix="miniapp-pilot-postcondition-", suffix=".png")
    os.close(fd)
    try:
        capture = save_screenshot(path)
    except Exception as exc:
        return {"path": path, "bytes": 0, "secure": False, "error": str(exc)}
    return {**capture, "path": path}


def _postcondition_screen_diff(before_path: str, after_path: str) -> float | None:
    try:
        from PIL import Image, ImageChops
        before = Image.open(before_path).convert("L").resize((90, 195))
        after = Image.open(after_path).convert("L").resize((90, 195))
        pixels = list(ImageChops.difference(before, after).getdata())
        return round(sum(pixels) / len(pixels), 2)
    except Exception:
        return None


def _discard_condition_capture(capture: dict | None) -> None:
    path = str((capture or {}).get("path") or "")
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _capture_step_after(step: dict) -> dict | None:
    """Persist an action-after frame only when the owning Run supplied a trace path."""
    path = str(step.get("_after_image_path") or "")
    if not path:
        return None
    try:
        return save_screenshot(path)
    except Exception as exc:
        return {"path": path, "bytes": 0, "secure": False, "error": str(exc)}


def _stop_requested(should_stop=None) -> bool:
    try:
        return bool(should_stop is not None and should_stop())
    except Exception:
        return False


def _interruptible_sleep(seconds: float, should_stop=None,
                         check_interval_s: float = 0.1) -> bool:
    """Sleep in short slices; return False as soon as Stop is requested."""
    import time as _t
    if should_stop is None:
        _t.sleep(max(0.0, float(seconds)))
        return True
    deadline = _t.monotonic() + max(0.0, float(seconds))
    while True:
        if _stop_requested(should_stop):
            return False
        remaining = deadline - _t.monotonic()
        if remaining <= 0:
            return True
        _t.sleep(min(max(0.001, check_interval_s), remaining))


def _evaluate_postcondition(postcondition: dict | None, poll_s: float = 0.4,
                            screen_before: dict | None = None,
                            webview_quick_probe: bool = False,
                            prefer_positive_semantic: bool = False,
                            semantic_focus_before: str = "",
                            should_stop=None) -> dict:
    """Evaluate one flat postcondition group with one timeout and one settle policy."""
    import time as _t

    if postcondition is None:
        return {
            "satisfied": True, "mode": "all", "checks": [], "waited_ms": 0,
            "focused_app": get_focused_app(),
        }
    mode = str(postcondition.get("mode") or "all")
    checks = list(postcondition.get("checks") or [])
    settle_ms = max(0, int(postcondition.get("settle_ms") or 0))
    timeout_s = max(0.0, float(postcondition.get("timeout_s", 5.0)))
    if settle_ms and not _interruptible_sleep(settle_ms / 1000.0, should_stop):
        _discard_condition_capture(screen_before)
        return {
            "satisfied": False, "stopped": True, "mode": mode, "checks": [],
            "waited_ms": 0, "focused_app": "",
        }
    start = _t.monotonic()
    if not checks:
        return {
            "satisfied": True, "mode": mode, "checks": [], "waited_ms": settle_ms,
            "focused_app": get_focused_app(), "settle_only": True,
        }
    latest: list[dict] = []
    weak_success: list[dict] | None = None
    weak_success_at_s: float | None = None
    latched_screen_changes: dict[int, dict] = {}
    focused_app = ""
    webview_surface = False
    has_positive_semantic = any(
        str(check.get("state") or "present") == "present"
        and str(check.get("type") or "") in {"a11y", "focus"}
        for check in checks
    )
    has_weak_fallback = any(
        str(check.get("state") or "present") != "present"
        or str(check.get("type") or "") not in {"a11y", "focus"}
        for check in checks
    )
    prefer_semantic = bool(
        prefer_positive_semantic and mode == "any"
        and has_positive_semantic and has_weak_fallback
    )
    while True:
        if _stop_requested(should_stop):
            _discard_condition_capture(screen_before)
            return {
                "satisfied": False, "stopped": True, "mode": mode,
                "checks": latest, "waited_ms": int(
                    (_t.monotonic() - start + settle_ms / 1000.0) * 1000
                ), "focused_app": focused_app,
            }
        need_a11y = any(check.get("type") == "a11y" for check in checks)
        need_focus = need_a11y or any(check.get("type") == "focus" for check in checks)
        focused_app = get_focused_app() if need_focus else ""
        elements: list[dict] = []
        if need_a11y:
            _, _, elements = _screen_elements(filter_useful=True, max_elements=300)
            webview_surface = _webview_surface(elements)
        latest = []
        for check_index, check in enumerate(checks):
            check_type = str(check.get("type") or "")
            state = str(check.get("state") or "present")
            if (prefer_semantic and check_type == "screen_change"
                    and check_index in latched_screen_changes):
                latest.append(dict(latched_screen_changes[check_index]))
                continue
            observation = {"type": check_type, "state": state}
            present = False
            if check_type == "a11y":
                selector = str(check.get("selector") or "text")
                expected = str(check.get("value") or "")
                match = str(check.get("match") or "contains")
                matches = []
                for element in elements:
                    if selector == "resource_id":
                        values = [str(element.get("resource_id") or "")]
                    elif selector == "content_desc":
                        values = [str(element.get("content_desc") or "")]
                    elif selector == "text_or_content_desc":
                        values = [str(element.get("text") or ""),
                                  str(element.get("content_desc") or "")]
                    else:
                        values = [str(element.get("text") or "")]
                    if any(_match_a11y_value(value, expected, match) for value in values):
                        matches.append(element)
                present = bool(matches)
                observation.update({
                    "selector": "text" if selector == "text_or_content_desc" else selector,
                    "value": expected, "match": match, "matched_count": len(matches),
                })
                if matches:
                    hit = matches[0]
                    observation["matched"] = {
                        "text": hit.get("text", ""),
                        "resource_id": hit.get("resource_id", ""),
                        "content_desc": hit.get("content_desc", ""),
                        "center": hit.get("center"), "bounds": hit.get("bounds"),
                    }
            elif check_type == "focus":
                expected = str(check.get("value") or "")
                present = expected in focused_app
                observation.update({"value": expected, "observed": focused_app})
            elif check_type == "screen_change":
                threshold = float(check.get("diff_threshold", 3.0))
                after = _capture_postcondition_screen()
                try:
                    if (not screen_before or screen_before.get("secure")
                            or after.get("secure") or not screen_before.get("bytes")
                            or not after.get("bytes")):
                        observation.update({
                            "satisfied": False, "diff": None, "threshold": threshold,
                            "unavailable": "secure_screen" if (
                                (screen_before or {}).get("secure") or after.get("secure")
                            ) else "screen_capture_failed",
                        })
                        latest.append(observation)
                        continue
                    diff = _postcondition_screen_diff(
                        str(screen_before.get("path") or ""), str(after.get("path") or ""),
                    )
                    present = diff is not None and diff > threshold
                    observation.update({"diff": diff, "threshold": threshold})
                finally:
                    _discard_condition_capture(after)
            else:
                observation.update({"satisfied": False, "unavailable": "unknown_check_type"})
                latest.append(observation)
                continue
            observation["satisfied"] = present if state == "present" else not present
            if (prefer_semantic and check_type == "screen_change"
                    and observation["satisfied"] is True):
                # A frame comparison is already a stable proof that this action
                # changed the screen. Keep that first proof while waiting for a
                # delayed semantic node instead of recapturing every poll.
                latched_screen_changes[check_index] = dict(observation)
            latest.append(observation)

        values = [bool(item.get("satisfied")) for item in latest]
        satisfied = all(values) if mode == "all" else any(values)
        semantic_satisfied = any(
            item.get("satisfied") is True
            and str(item.get("state") or "present") == "present"
            and (
                (item.get("type") == "a11y" and int(item.get("matched_count") or 0) > 0)
                or (
                    item.get("type") == "focus"
                    and bool(semantic_focus_before)
                    and str(item.get("observed") or "") != semantic_focus_before
                )
            )
            for item in latest
        )
        elapsed = _t.monotonic() - start
        if prefer_semantic and satisfied and not semantic_satisfied:
            # A weak check proves that the action may proceed, but a delayed
            # semantic hit can still close the request without another model
            # turn. Preserve the first weak proof and allow a short semantic
            # grace period instead of consuming the caller's whole deadline.
            if weak_success_at_s is None:
                weak_success_at_s = elapsed
                weak_success = [dict(item) for item in latest]
        effective_timeout_s = timeout_s
        if (webview_quick_probe and webview_surface
                and all(check.get("type") == "a11y" for check in checks)):
            effective_timeout_s = min(timeout_s, _WEBVIEW_POSTCONDITION_TIMEOUT_S)
        semantic_grace_done = bool(
            prefer_semantic
            and weak_success_at_s is not None
            and elapsed >= min(
                effective_timeout_s,
                weak_success_at_s + _FINAL_SEMANTIC_GRACE_S,
            )
        )
        semantic_preference_done = bool(
            not prefer_semantic or semantic_satisfied or semantic_grace_done
            or elapsed >= effective_timeout_s
        )
        if ((satisfied and semantic_preference_done) or semantic_grace_done
                or elapsed >= effective_timeout_s):
            if prefer_semantic and not semantic_satisfied and weak_success is not None:
                latest = weak_success
                satisfied = True
            result = {
                "satisfied": satisfied, "mode": mode, "checks": latest,
                "waited_ms": int((elapsed + settle_ms / 1000.0) * 1000),
                "focused_app": focused_app,
            }
            if webview_surface and not satisfied:
                result["webview_surface"] = True
                result["effective_timeout_s"] = effective_timeout_s
            _discard_condition_capture(screen_before)
            return result
        if not _interruptible_sleep(max(poll_s, 0.001), should_stop):
            _discard_condition_capture(screen_before)
            return {
                "satisfied": False, "stopped": True, "mode": mode,
                "checks": latest, "waited_ms": int(
                    (_t.monotonic() - start + settle_ms / 1000.0) * 1000
                ), "focused_app": focused_app,
            }


def _await_step_target(kind: str, value: str, timeout_s: float, poll_s: float,
                       should_stop=None) -> tuple[bool, str]:
    """动作前等待声明式目标进入 a11y；只等待，不点击，避免把轮询变成重复动作。"""
    import time as _t
    start = _t.monotonic()
    while True:
        if _stop_requested(should_stop):
            return False, ""
        fa = get_focused_app()
        _, _, els = _screen_elements(filter_useful=True, max_elements=300)
        if kind == "rid":
            found = any(e.get("resource_id", "").endswith(value) for e in els)
        elif kind == "desc":
            found = any(e.get("content_desc", "") == value for e in els)
        elif kind == "contains":
            found = any(value in e.get("text", "") or value in e.get("content_desc", "") for e in els)
        else:
            found = any(value in (e.get("text", ""), e.get("content_desc", "")) for e in els)
        if found:
            return True, fa
        if _t.monotonic() - start >= timeout_s:
            return False, fa
        if not _interruptible_sleep(poll_s, should_stop):
            return False, fa


def _tap_step_target(kind: str, value: str, declared_target: str,
                     timeout_s: float, poll_s: float,
                     row_via_selector: bool = False, index: int = 0,
                     should_stop=None) -> tuple[str, dict | None]:
    """等待并点击一个声明式目标；返回可审计失败原因与底层动作结果。"""
    if kind == "xy":
        x, y = _point(value)
        _adb("shell", "input", "tap", str(x), str(y))
        return "", {"action": "tap", "x": x, "y": y}

    ready, _ = _await_step_target(kind, value, timeout_s, poll_s, should_stop)
    if not ready:
        return f"等待目标 {declared_target!r} 超时 {timeout_s}s，未执行点击", None
    if kind == "row":
        if row_via_selector:
            result = tap_element(text=value, index=index)
            if not result.get("ok"):
                return f"定位不到行文字目标: {result.get('error')}", result
            return "", result
        width, _, elements = _screen_elements(filter_useful=True, max_elements=300)
        hit = next((element for element in elements
                    if value in (element.get("text", ""), element.get("content_desc", ""))), None)
        center = hit.get("center") if hit else None
        if not width or not center or len(center) < 2:
            return f"目标 {declared_target!r} 缺少可靠行坐标，未执行点击", None
        x, y = round(width * 0.5), int(center[1])
        _adb("shell", "input", "tap", str(x), str(y))
        return "", {"action": "tap", "x": x, "y": y, "via": "row"}

    selector = {}
    if kind == "rid":
        selector["resource_id"] = value
    elif kind == "desc":
        selector["content_desc"] = value
    else:
        selector["text"] = value
        selector["contains"] = kind == "contains"
    result = tap_element(**selector, index=index)
    if not result.get("ok"):
        return f"定位不到目标: {result.get('error')}", result
    return "", result


def execute_ui_steps(steps: list[dict], default_timeout_s: float = 5.0, poll_ms: int = 400,
                     settle_ms: int = 500, should_stop=None) -> dict:
    """
    通用 UI 步骤执行器：按给定步骤序列逐步执行 + 逐步校验，全程在本调用内、不回大脑。
    固定 Excel 脚本和当前页面临时生成的短动作批次共用此实现。

    steps: 步骤列表，每步 {action, target, expect, timeout_s}（后三个可省）：
      action ∈ tap_text | tap_text_retry | tap_xy | input | clear_input | swipe_up | swipe_down | press_key | wait
               | ok_if_text | launch_app | stop_app
      target 默认按文字【精确】定位；"~:某片段" 按文字子串（目标只是长节点的一部分时用）；
             "row:某文字" 按精确文字确定纵坐标后点击屏幕中线上的整行区域；
             "rid:xxx" 按 resource_id 后缀；"xy:540,1746" 绝对像素；"pct:5,5.73" 屏幕百分比。
             input 的 target 是要输入的文本；press_key 的 target 是 wake/back/home/enter/delete；
             launch_app/stop_app 的 target 是包名。
      expect 本步做完后应出现的东西：留空=不校验；"focus:xxx"=等前台窗口含该串；否则=等该文字出现。
      timeout_s 本步校验的等待上限，省略则用 default_timeout_s。

    【settle_ms = 动作前沉降】每个碰设备的动作执行前先等这么久（默认 500ms，纯等待/判断步不等）。
    治"上一步的 expect 刚满足（元素刚进 a11y）就立刻点 → 界面还在动画、点击落空"。
    应用启动后必须等待页面稳定，再定位并执行后续动作。

    【ok_if_text = 条件短路】target 的文字在 timeout_s 内出现 → 判定目标已达成，
    整条脚本【立即以 ok=true 结束】、后续步骤一步不做（返回 short_circuit=true 与命中步号）；
    没出现就继续往下走。用它表达"先检查现状，已经满足就收工、不满足才执行修复流程"这类脚本，
    无需在执行器里引入变量与跳转。注意它按【子串】匹配：若两个可能值互为子串会误判，取值须互不为子串。

    【tap_text_retry = 受反馈约束的双击尝试】只用于已知首击可能被动画吞掉的语义目标：
    首击后先等 expect；只有 expect 未出现且原 target 仍在当前页时才再点一次，最多两次。
    row: 目标的首击使用 ADB 整行中点，第二击改用无障碍文字选择器，避免重复同一无效注入方式。
    target 消失或页面已变化时绝不补点；该动作必须声明 expect，且不支持 xy:/pct: 坐标目标。

    【失败即停，绝不硬闯】任一步动作做不了或 expect 超时未满足，立即停止并返回：
      {ok:false, failed_at:{i,action,target,expect,reason}, done_steps, total_steps, focused_app, log, hint}
      —— 走到第几步、卡在哪一步的什么校验、当前在哪个窗口，全给大脑，由它看屏自行处置。
      执行器本身【不重试、不跳过、不继续】：中途停下时设备多半停在流程中间态，盲目重跑整串会二次伤害。

    app 无关：动作类型是通用 UI 原语，具体点什么/等什么全在 steps 里，代码不含任何流程知识。
    """
    import time as _t
    poll_s = max(poll_ms, 1) / 1000.0
    log: list[dict] = []
    active_screen_before = None

    def _safe_target(step):
        target = str(step.get("target", "") or "")
        return f"[REDACTED:{len(target)}]" if step.get("action") == "input" else target[:40]

    def _fail(i, st, reason, action_executed=False, postcondition=None, after_image=None):
        _discard_condition_capture(active_screen_before)
        failed_at = {"i": i + 1, "action": st.get("action"), "target": _safe_target(st),
                     "expect": st.get("expect", ""), "reason": reason,
                     "action_executed": bool(action_executed)}
        if action_executed:
            after_image = after_image or _capture_step_after(st)
            if after_image is not None:
                failed_at["after_image"] = after_image
        if postcondition is not None:
            failed_at["postcondition_satisfied"] = bool(postcondition.get("satisfied"))
            failed_at["postcondition"] = postcondition
        return {"ok": False, "done_steps": i, "total_steps": len(steps),
                "failed_at": failed_at,
                "focused_app": get_focused_app(), "log": log,
                "hint": ("脚本已中断，设备多半停在流程中间态。请先观察当前页面确认真实状态，"
                         "再决定下一步；不要盲目从头重跑整串步骤。")}

    def _stopped(i, st, action_executed=False, postcondition=None, after_image=None):
        result = _fail(
            i, st, "用户已停止当前执行；不会执行后续动作", action_executed,
            postcondition, after_image,
        )
        result["stopped"] = True
        result["reason"] = "用户已停止当前执行；已执行前缀保留，后续动作未执行"
        return result

    for i, st in enumerate(steps):
        active_screen_before = None
        if _stop_requested(should_stop):
            return {
                "ok": False, "stopped": True, "done_steps": i,
                "total_steps": len(steps), "focused_app": get_focused_app(), "log": log,
                "reason": "用户已停止当前执行；未开始下一动作",
            }
        step_t0 = _t.monotonic()
        act = (st.get("action") or "").strip()
        tgt = st.get("target", "") or ""
        expect = (st.get("expect") or "").strip()
        tmo = float(st.get("timeout_s") or default_timeout_s)
        declared_postcondition = st.get("postcondition")
        postcondition = declared_postcondition
        webview_quick_probe = bool(
            act == "wait"
            and isinstance(declared_postcondition, dict)
            and "timeout_s" not in declared_postcondition
        )
        if postcondition is None:
            postcondition = _legacy_expect_postcondition(expect, tmo)
        else:
            postcondition = dict(postcondition)
            postcondition.setdefault("timeout_s", tmo)
        attempts = None
        action_executed = False
        action_result = None
        if act not in _STEP_ACTIONS:
            return _fail(i, st, f"未知动作 {act!r}；可用: {', '.join(_STEP_ACTIONS)}")
        if act not in ("wait", "ok_if_text") and settle_ms > 0:
            if not _interruptible_sleep(settle_ms / 1000.0, should_stop):
                return _stopped(i, st)
        if postcondition and any(
                check.get("type") == "screen_change"
                for check in postcondition.get("checks", [])):
            active_screen_before = _capture_postcondition_screen()
        final_focus_before = ""
        if postcondition and any(
                check.get("type") == "focus"
                and str(check.get("state") or "present") == "present"
                for check in postcondition.get("checks", [])):
            # Private correlation used by the Native wrapper to distinguish a real
            # Activity transition from a same-window focus match. It is removed
            # before the action log or public tool result is persisted.
            final_focus_before = get_focused_app()
        try:
            if act in ("tap_text", "tap_text_retry"):
                kind, val = _resolve_target(tgt)
                target_index = int(st.get("index") or 0)
                if act == "tap_text_retry" and postcondition is None:
                    return _fail(i, st, "tap_text_retry 必须声明 postcondition/固定 expect，未执行点击")
                if act == "tap_text_retry" and kind == "xy":
                    return _fail(i, st, "tap_text_retry 不支持 xy:/pct: 坐标目标，未执行点击")
                reason, action_result = _tap_step_target(
                    kind, val, tgt, tmo, poll_s, index=target_index,
                    should_stop=should_stop,
                )
                if reason:
                    if _stop_requested(should_stop):
                        return _stopped(i, st)
                    return _fail(i, st, reason)
                action_executed = True
                attempts = 1
                if act == "tap_text_retry":
                    condition_timeout = float(postcondition.get("timeout_s") or tmo)
                    first_wait_s = min(2.0, max(0.001, condition_timeout / 2.0))
                    first_postcondition = {
                        **postcondition, "settle_ms": postcondition.get("settle_ms", 0),
                        "timeout_s": first_wait_s,
                    }
                    first_result = _evaluate_postcondition(
                        first_postcondition, poll_s=poll_s,
                        screen_before=active_screen_before,
                        prefer_positive_semantic=bool(
                            st.get("_prefer_positive_semantic")
                        ),
                        semantic_focus_before=final_focus_before,
                        should_stop=should_stop,
                    )
                    if first_result.get("stopped"):
                        return _stopped(
                            i, st, action_executed=True, postcondition=first_result,
                        )
                    ok = bool(first_result["satisfied"])
                    fa = str(first_result.get("focused_app") or "")
                    if ok:
                        entry = {"i": i + 1, "action": act, "target": _safe_target(st),
                                 "expect": expect, "ok": True, "attempts": attempts,
                                 "ms": int((_t.monotonic() - step_t0) * 1000),
                                 "focused_app": fa, "action_executed": True,
                                 "postcondition_satisfied": True,
                                 "postcondition": first_result}
                        if final_focus_before:
                            entry["_final_focus_before"] = final_focus_before
                        after_image = _capture_step_after(st)
                        if after_image is not None:
                            entry["after_image"] = after_image
                        log.append(entry)
                        continue
                    target_still_present, fa = _await_step_target(
                        kind, val, 0.0, poll_s, should_stop,
                    )
                    if _stop_requested(should_stop):
                        return _stopped(
                            i, st, action_executed=True, postcondition=first_result,
                        )
                    if not target_still_present:
                        entry = {"i": i + 1, "action": act, "target": _safe_target(st),
                                 "expect": expect, "ok": False, "attempts": attempts,
                                 "ms": int((_t.monotonic() - step_t0) * 1000),
                                 "focused_app": fa, "action_executed": True,
                                 "postcondition_satisfied": False,
                                 "postcondition": first_result}
                        after_image = _capture_step_after(st)
                        if after_image is not None:
                            entry["after_image"] = after_image
                        log.append(entry)
                        return _fail(
                            i, st,
                            f"首击后 postcondition 未满足，且目标已不在当前页（{tgt!r}）；未执行第二次点击",
                            action_executed=True, postcondition=first_result,
                            after_image=after_image,
                        )
                    if any(check.get("type") == "screen_change"
                           for check in postcondition.get("checks", [])):
                        active_screen_before = _capture_postcondition_screen()
                    reason, action_result = _tap_step_target(
                        kind, val, tgt, 0.0, poll_s, row_via_selector=(kind == "row"),
                        index=target_index, should_stop=should_stop,
                    )
                    if reason:
                        if _stop_requested(should_stop):
                            return _stopped(
                                i, st, action_executed=True, postcondition=first_result,
                            )
                        return _fail(i, st, f"第二次点击前目标状态变化: {reason}",
                                     action_executed=True, postcondition=first_result)
                    attempts = 2
                    postcondition = {
                        **postcondition,
                        "timeout_s": max(0.001, condition_timeout - first_wait_s),
                    }
            elif act == "tap_xy":
                x, y = _point(_resolve_target(tgt)[1])
                _adb("shell", "input", "tap", str(x), str(y))
                action_executed = True
                action_result = {"action": "tap", "x": x, "y": y}
            elif act == "input":
                action_executed = True
                r = input_text(tgt)
                action_result = r
                if not r.get("ok"):
                    return _fail(i, st, f"输入失败: {r}", action_executed=True)
            elif act == "clear_input":
                kind, val = _resolve_target(tgt)
                action_executed = True
                r = clear_input(resource_id=val if kind == "rid" else "",
                                text=val if kind in ("text", "contains") else "",
                                max_chars=int(st.get("max_chars") or 40))
                action_result = r
                if not r.get("ok"):
                    return _fail(i, st, f"清空失败(残留 {r.get('after_len')} 字符)",
                                 action_executed=True)
            elif act == "swipe_up":
                width, height = _wm_size()
                if not width or not height:
                    return _fail(i, st, "无法读取当前设备 viewport，未执行滑动")
                action_result = swipe(round(width * 0.5), round(height * 0.77),
                                      round(width * 0.5), round(height * 0.26), 400)
                action_executed = True
            elif act == "swipe_down":
                width, height = _wm_size()
                if not width or not height:
                    return _fail(i, st, "无法读取当前设备 viewport，未执行滑动")
                action_result = swipe(round(width * 0.5), round(height * 0.26),
                                      round(width * 0.5), round(height * 0.77), 400)
                action_executed = True
            elif act == "press_key":
                action_executed = True
                r = press_key(tgt or "back")
                action_result = r
                if not r.get("ok"):
                    return _fail(i, st, f"按键失败: {r}", action_executed=True)
            elif act == "launch_app":
                if should_stop is None and str(tgt).startswith("app:"):
                    r = launch_app(app_name=str(tgt)[4:].strip(), timeout_s=tmo)
                elif should_stop is None:
                    r = launch_app(package=tgt, timeout_s=tmo)
                elif str(tgt).startswith("app:"):
                    r = _launch_app_impl(
                        app_name=str(tgt)[4:].strip(), timeout_s=tmo,
                        should_stop=should_stop,
                    )
                else:
                    r = _launch_app_impl(
                        package=tgt, timeout_s=tmo, should_stop=should_stop,
                    )
                action_result = r
                if r.get("stopped"):
                    action_executed = bool(r.get("action_executed"))
                    return _stopped(i, st, action_executed=action_executed)
                action_executed = bool(r.get("ok")) or r.get("reason") not in {
                    "invalid_args", "invalid_package", "not_installed", "no_launcher_activity",
                }
                if not r.get("ok"):
                    return _fail(i, st, f"启动失败: {r.get('reason', 'unknown')} | {r}",
                                 action_executed=action_executed)
            elif act == "stop_app":
                action_result = stop_app(tgt)
                action_executed = True
            elif act == "ok_if_text":
                # 条件短路：命中即判定目标已达成，整条脚本成功结束；没命中就继续下一步
                hit, fa = _await_expect(
                    _resolve_target(tgt)[1], tmo, poll_s, should_stop,
                )
                if _stop_requested(should_stop):
                    return _stopped(i, st)
                entry = {"i": i + 1, "action": act, "target": _safe_target(st),
                         "expect": "", "ok": hit,
                         "ms": int((_t.monotonic() - step_t0) * 1000),
                         "focused_app": fa, "action_executed": False}
                after_image = _capture_step_after(st)
                if after_image is not None:
                    entry["after_image"] = after_image
                log.append(entry)
                if hit:
                    return {"ok": True, "short_circuit": True, "stopped_at_step": i + 1,
                            "done_steps": i + 1, "total_steps": len(steps), "focused_app": fa,
                            "reason": f"第{i + 1}步命中 {tgt!r} → 目标已达成，后续步骤无需执行",
                            "log": log}
                continue
            # act == "wait" 不做动作，只走下面的 expect 等待
        except Exception as e:
            return _fail(i, st, f"执行异常: {e}", action_executed=action_executed)

        condition_result = _evaluate_postcondition(
            postcondition, poll_s=poll_s, screen_before=active_screen_before,
            webview_quick_probe=webview_quick_probe,
            prefer_positive_semantic=bool(st.get("_prefer_positive_semantic")),
            semantic_focus_before=final_focus_before,
            should_stop=should_stop,
        )
        if condition_result.get("stopped"):
            return _stopped(
                i, st, action_executed=action_executed,
                postcondition=condition_result,
            )
        ok = bool(condition_result["satisfied"])
        fa = str(condition_result.get("focused_app") or "")
        entry = {"i": i + 1, "action": act, "target": _safe_target(st),
                 "expect": expect, "ok": ok, "ms": int((_t.monotonic() - step_t0) * 1000),
                 "focused_app": fa, "action_executed": action_executed,
                 "postcondition_satisfied": ok, "postcondition": condition_result}
        if final_focus_before:
            entry["_final_focus_before"] = final_focus_before
        if attempts is not None:
            entry["attempts"] = attempts
        if action_result is not None:
            entry["action_result"] = action_result
        after_image = _capture_step_after(st)
        if after_image is not None:
            entry["after_image"] = after_image
        log.append(entry)
        if not ok:
            return _fail(i, st, "校验超时：动作后条件在统一期限内未满足", action_executed,
                         condition_result, after_image=after_image)

    return {"ok": True, "done_steps": len(steps), "total_steps": len(steps),
            "focused_app": get_focused_app(), "log": log}


@mcp.tool()
def run_ui_steps(steps: list[dict], default_timeout_s: float = 5.0, poll_ms: int = 400,
                 settle_ms: int = 500) -> dict:
    """Execute a caller-provided fixed UI step sequence."""
    return execute_ui_steps(steps, default_timeout_s, poll_ms, settle_ms)


def _flow_observation() -> tuple[str, list[dict]]:
    """一次读取前台和 a11y，供同一阶段的所有规则共享，避免逐条重复 dump。"""
    focus = get_focused_app()
    _, _, elements = _screen_elements(filter_useful=True, max_elements=300)
    return focus, elements


def _flow_rule_matches(rule: str, focus: str, elements: list[dict]) -> bool:
    """通用流程匹配：always / focus: / text: / desc: / rid:。"""
    raw = (rule or "").strip()
    if raw.lower() == "always":
        return True
    prefix, sep, value = raw.partition(":")
    kind = prefix.strip().lower() if sep else "text"
    needle = value.strip() if sep else raw
    if kind == "focus":
        return needle in focus
    if kind == "rid":
        return any(e.get("resource_id", "").endswith(needle) for e in elements)
    if kind == "desc":
        return any(e.get("content_desc", "") == needle for e in elements)
    if kind == "text":
        return any(needle in e.get("text", "") or needle in e.get("content_desc", "")
                   for e in elements)
    return False


def run_ui_flow(routes: list[dict], scripts: dict[str, list[dict]], start_stage: str = "开始",
                default_timeout_s: float = 5.0, poll_ms: int = 400, settle_ms: int = 500,
                route_timeout_s: float = 8.0, route_settle_ms: int = 2000,
                max_routes: int = 20, should_stop=None) -> dict:
    """按声明式流程映射选择并执行步骤脚本；失败如实返回，由调用方交给一次 LLM 处理。

    同阶段按 ``i`` 决定优先级。低优先级规则先命中时会沉降 route_settle_ms，给异步出现的
    高优先级页面一次抢占机会；最高优先级规则与 always 立即执行。代码只认通用规则。
    """
    import time as _t

    stage = (start_stage or "开始").strip()
    poll_s = max(int(poll_ms), 1) / 1000.0
    route_log: list[dict] = []
    last_focus = ""
    last_elements: list[dict] = []

    for _ in range(max(1, int(max_routes))):
        if should_stop is not None and should_stop():
            return {"ok": False, "stopped": True, "stage": stage,
                    "done_routes": len(route_log), "focused_app": get_focused_app(),
                    "route_log": route_log}
        choices = sorted(
            [route for route in routes if str(route.get("stage", "")).strip() == stage],
            key=lambda route: float(route.get("i") or 0),
        )
        if not choices:
            return {"ok": False, "reason": "stage_has_no_routes", "stage": stage,
                    "focused_app": get_focused_app(), "route_log": route_log}

        deadline = _t.monotonic() + max(0.0, float(route_timeout_s))
        candidate = None
        candidate_since = 0.0
        selected = None
        while True:
            if _stop_requested(should_stop):
                return {"ok": False, "stopped": True, "stage": stage,
                        "done_routes": len(route_log), "focused_app": last_focus,
                        "route_log": route_log}
            try:
                last_focus, last_elements = _flow_observation()
            except Exception as exc:
                return {"ok": False, "reason": "observe_failed", "stage": stage,
                        "error": str(exc), "focused_app": get_focused_app(),
                        "route_log": route_log}
            matched_index = None
            for index, route in enumerate(choices):
                if _flow_rule_matches(str(route.get("match", "")), last_focus, last_elements):
                    matched_index = index
                    break
            now = _t.monotonic()
            if matched_index is not None:
                route = choices[matched_index]
                identity = (matched_index, str(route.get("match", "")), str(route.get("script", "")))
                if matched_index == 0 or str(route.get("match", "")).strip().lower() == "always":
                    selected = route
                    break
                if identity != candidate:
                    candidate = identity
                    candidate_since = now
                elif (now - candidate_since) * 1000 >= max(0, int(route_settle_ms)):
                    selected = route
                    break
            else:
                candidate = None
                candidate_since = 0.0
            if now >= deadline:
                break
            if not _interruptible_sleep(poll_s, should_stop):
                return {"ok": False, "stopped": True, "stage": stage,
                        "done_routes": len(route_log), "focused_app": last_focus,
                        "route_log": route_log}

        if selected is None:
            visible = []
            for element in last_elements:
                for value in (element.get("text", ""), element.get("content_desc", "")):
                    if value and value not in visible:
                        visible.append(value)
            return {"ok": False, "reason": "no_route_matched", "stage": stage,
                    "focused_app": last_focus, "visible_texts": visible[:40],
                    "route_log": route_log}

        script_name = str(selected.get("script", "")).strip()
        if script_name and script_name not in scripts:
            return {"ok": False, "reason": "script_not_found", "stage": stage,
                    "script": script_name, "focused_app": last_focus,
                    "route_log": route_log}
        result = execute_ui_steps(
            scripts.get(script_name, []), default_timeout_s=default_timeout_s,
            poll_ms=poll_ms, settle_ms=settle_ms, should_stop=should_stop,
        )
        step_log = result.pop("log", None)
        record = {
            "stage": stage,
            "match": str(selected.get("match", "")),
            "script": script_name,
            "ok": bool(result.get("ok")),
            "done_steps": result.get("done_steps", 0),
            "total_steps": result.get("total_steps", 0),
            "focused_app": result.get("focused_app", last_focus),
        }
        if step_log:
            record["steps"] = step_log
        route_log.append(record)
        if not result.get("ok") and not bool(selected.get("continue_on_fail")):
            return {"ok": False, "reason": "script_failed", "stage": stage,
                    "script": script_name, "script_result": result,
                    "focused_app": result.get("focused_app", last_focus),
                    "route_log": route_log}

        next_stage = str(selected.get("next_stage", "")).strip()
        if not next_stage:
            return {"ok": True, "done_routes": len(route_log),
                    "focused_app": result.get("focused_app", last_focus),
                    "route_log": route_log}
        stage = next_stage

    return {"ok": False, "reason": "max_routes_exceeded", "stage": stage,
            "focused_app": get_focused_app(), "route_log": route_log}


def tap_element(resource_id: str = "", text: str = "", index: int = 0,
                contains: bool = False, content_desc: str = "") -> dict:
    """
    用 resource_id、text 或 content_desc 选择器点击元素（原生控件比坐标可靠，webview bounds 不可信时首选）。
    index 选第几个匹配(默认0)。返回是否找到并点击。

    ⚠️ text 默认是【精确】匹配（u2 语义），而 wait_for/find 是【子串】匹配——两者标准不同！
    所以会出现"wait_for 等到了、tap_element 却点不中"：目标文字只是某个长节点的一部分时就是这种情况
    （实测：刷脸协议勾选项在 a11y 里是一整段"您知悉并同意…"，没有独立的"同意"节点）。
    这种情况传 contains=True 改用子串匹配（u2 textContains）。默认 False，老行为不变。
    """
    d = _dev()
    try:
        if resource_id:
            sel = d(resourceId=resource_id)
        elif content_desc:
            sel = d(description=content_desc)
        elif text:
            sel = d(textContains=text) if contains else d(text=text)
        else:
            return {"ok": False, "error": "需要 resource_id、text 或 content_desc"}
        if sel.count == 0:
            return {"ok": False, "error": "未找到匹配元素"}
        sel[index].click()
        return {"ok": True, "matched": sel.count, "clicked_index": index}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> dict:
    """从 (x1,y1) 滑到 (x2,y2)，duration_ms 毫秒。用于滚动/翻页。滑后请 get_screen 核验。"""
    _adb("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms))
    return {"action": "swipe", "from": [x1, y1], "to": [x2, y2], "duration_ms": duration_ms}


def _repeat_swipe_impl(count: int, x1: int | None = None, y1: int | None = None,
                       x2: int | None = None, y2: int | None = None,
                       duration_ms: int = 250, interval_ms: int = 80,
                       should_stop=None) -> dict:
    """一次调用连续执行 count 次普通滑动，完成后再统一核验。

    仅用于任务明确要求“快速连续滑动/重复滚动”的普通可滚动页面；不得用于滑块、
    拖拽控件或必须持续按住的交互。坐标全部省略时按当前 viewport 取内容区的通用
    比例坐标；显式坐标必须来自本次页面观测，四个坐标要么全部提供、要么全部省略。
    """
    count = int(count)
    if count < 1 or count > 20:
        return {"ok": False, "reason": "invalid_count", "allowed": [1, 20]}
    duration_ms = int(duration_ms)
    interval_ms = int(interval_ms)
    if duration_ms < 1 or duration_ms > 5000:
        return {"ok": False, "reason": "invalid_duration_ms", "allowed": [1, 5000]}
    if interval_ms < 0 or interval_ms > 5000:
        return {"ok": False, "reason": "invalid_interval_ms", "allowed": [0, 5000]}

    coords = (x1, y1, x2, y2)
    supplied = [value is not None for value in coords]
    if any(supplied) and not all(supplied):
        return {"ok": False, "reason": "partial_coordinates",
                "note": "x1/y1/x2/y2 必须全部提供或全部省略"}
    if all(supplied):
        x1, y1, x2, y2 = (int(value) for value in coords)
        if min(x1, y1, x2, y2) < 0:
            return {"ok": False, "reason": "invalid_coordinates"}
        coordinate_source = "explicit"
    else:
        width, height = _wm_size()
        if not width or not height:
            return {"ok": False, "reason": "viewport_unavailable"}
        x1 = x2 = round(width * 0.5)
        y1 = round(height * 0.70)
        y2 = round(height * 0.30)
        coordinate_source = "viewport_default"

    completed_count = 0
    for index in range(count):
        if _stop_requested(should_stop):
            return {
                "ok": False, "stopped": True, "action": "repeat_swipe",
                "requested_count": count, "completed_count": completed_count,
                "from": [x1, y1], "to": [x2, y2],
            }
        _adb("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2),
             str(duration_ms))
        completed_count += 1
        if interval_ms and index + 1 < count:
            if not _interruptible_sleep(interval_ms / 1000, should_stop):
                return {
                    "ok": False, "stopped": True, "action": "repeat_swipe",
                    "requested_count": count, "completed_count": completed_count,
                    "from": [x1, y1], "to": [x2, y2],
                }
    return {
        "ok": True,
        "action": "repeat_swipe",
        "requested_count": count,
        "completed_count": completed_count,
        "from": [x1, y1],
        "to": [x2, y2],
        "duration_ms": duration_ms,
        "interval_ms": interval_ms,
        "coordinates": coordinate_source,
    }


@mcp.tool()
def repeat_swipe(count: int, x1: int | None = None, y1: int | None = None,
                 x2: int | None = None, y2: int | None = None,
                 duration_ms: int = 250, interval_ms: int = 80) -> dict:
    """一次调用连续执行 count 次普通滑动，完成后再统一核验。"""
    return _repeat_swipe_impl(
        count, x1, y1, x2, y2, duration_ms, interval_ms,
    )


def _drag_impl(x1: int, y1: int, x2: int, y2: int, hold_ms: int = 500,
               duration_ms: int = 1200, steps: int = 12,
               should_stop=None) -> dict:
    """真实长拖拽：按住 hold_ms，再分段慢拖 duration_ms，最后释放。

    用于普通 adb swipe 不生效、需要持续触屏手势的界面（如下拉面板、拖拽控件）。
    起点不要贴屏幕顶边，否则 Android 可能截获为系统通知栏手势。
    """
    hold_ms = max(0, min(int(hold_ms), 5000))
    duration_ms = max(100, min(int(duration_ms), 10000))
    steps = max(2, min(int(steps), 60))
    d = _dev()
    d.touch.down(int(x1), int(y1))
    completed_steps = 0
    stopped = False
    try:
        if hold_ms and not _interruptible_sleep(hold_ms / 1000, should_stop):
            stopped = True
        delay = duration_ms / steps / 1000
        if not stopped:
            for i in range(1, steps + 1):
                if _stop_requested(should_stop):
                    stopped = True
                    break
                x = round(x1 + (x2 - x1) * i / steps)
                y = round(y1 + (y2 - y1) * i / steps)
                d.touch.move(x, y)
                completed_steps = i
                if not _interruptible_sleep(delay, should_stop):
                    stopped = True
                    break
    finally:
        d.touch.up(int(x2), int(y2))
    return {"ok": not stopped, "stopped": stopped, "action": "drag",
            "from": [x1, y1], "to": [x2, y2], "hold_ms": hold_ms,
            "duration_ms": duration_ms, "steps": steps,
            "completed_steps": completed_steps}


@mcp.tool()
def drag(x1: int, y1: int, x2: int, y2: int, hold_ms: int = 500,
         duration_ms: int = 1200, steps: int = 12) -> dict:
    """真实长拖拽：按住、分段移动并释放。"""
    return _drag_impl(x1, y1, x2, y2, hold_ms, duration_ms, steps)


def clear_input(resource_id: str = "", text: str = "", max_chars: int = 40) -> dict:
    """
    一次清空输入框（原生 EditText）。给了 resource_id/text 就先按选择器定位，否则清当前聚焦框。
    先走 uiautomator2 的 clear_text；失败则回退"光标移到末尾 + 连发 max_chars 次退格"（一条 adb 命令发完，不逐个回大脑）。
    治"输入框有残留字符 → 逐次 press_key(delete) 每次都过一趟大脑"的空转。
    返回 {ok, via, before_len, after_len}；after_len>0 说明没清干净（可能不是标准输入框），交回大脑处理。
    app 无关：选择器与上限都是参数。
    """
    def _len_of() -> int | None:
        try:
            _, _, els = _screen_elements(filter_useful=True, max_elements=300)
            for e in els:
                if resource_id and e["resource_id"].endswith(resource_id):
                    return len(e["text"] or "")
                if text and text in e["text"]:
                    return len(e["text"] or "")
            return None
        except Exception:
            return None

    before = _len_of()
    try:
        d = _dev()
        if resource_id:
            sel = d(resourceId=resource_id)
        elif text:
            sel = d(text=text)
        else:
            sel = d(focused=True)
        if sel.count > 0:
            sel.clear_text()
            return {"ok": True, "via": "clear_text", "before_len": before, "after_len": _len_of()}
    except Exception:
        pass
    # 回退：MOVE_END(123) 后连发 DEL(67)。input keyevent 支持一次多个键码 → 只一条 adb 命令
    n = max(1, min(max_chars, (before or max_chars) + 5))
    _adb("shell", "input", "keyevent", "123", *(["67"] * n))
    after = _len_of()
    return {"ok": (after in (0, None)), "via": f"keyevent×{n}", "before_len": before, "after_len": after}


def input_text(text: str) -> dict:
    """向当前聚焦的输入框输入文本（需先点中输入框）。"""
    try:
        _dev().send_keys(text, clear=False)
        return {"ok": True, "text": text}
    except Exception as e:
        # 回退到 adb（仅 ASCII）
        _adb("shell", "input", "text", text.replace(" ", "%s"))
        return {"ok": True, "text": text, "via": "adb_fallback", "warn": str(e)}


def _tap_digits_impl(digits: str, tap_confirm: bool = False, confirm_text: str = "确定",
                     should_stop=None) -> dict:
    """
    在【app 自绘的屏上数字键盘】上逐位点击输入数字（input_text 灌不进这种键盘时用）。
    通用：任意有数字键(0-9)标签的屏上键盘都适用，不限某个 app。
    先确保键盘已弹出（通常需先 tap 输入框聚焦）。读当前屏幕的数字键坐标再逐位点，布局无关。
    digits: 只含 0-9 的字符串。tap_confirm=True 时输完点确认键(confirm_text)收键盘。
    返回每位是否点到；某位找不到对应键则报错（多半键盘没弹出/被遮挡）。
    """
    if not digits or not digits.isdigit():
        return {"ok": False, "error": "digits 必须是只含 0-9 的字符串"}
    w, h, elements = _screen_elements(filter_useful=True, max_elements=300)
    keymap: dict[str, list[int]] = {}
    for e in elements:
        if e["text"] in list("0123456789") and e["clickable"] and not e["bounds_unreliable"]:
            keymap.setdefault(e["text"], e["center"])
    via = "a11y"
    calibration_key = ""
    missing = [c for c in digits if c not in keymap]
    # FLAG_SECURE 页面有时会把整个 app 内容从 a11y 隐掉。此时截图同样为黑/0 字节，
    # 视觉兜底事实上不可用；仅在【一个数字键都没暴露】且窗口+分辨率精确命中真机标定时使用坐标。
    # 不做比例猜测，不拿其它 Activity/分辨率的标定冒险，也不覆盖部分可见的异常键盘。
    if missing and not keymap:
        focused = get_focused_app()
        calibrated, calibration_key = _calibrated_digit_keymap(focused, w, h)
        if calibrated:
            keymap = calibrated
            missing = [c for c in digits if c not in keymap]
            via = "calibrated"
    if missing:
        return {"ok": False, "error": f"数字键盘未就绪/找不到键: {set(missing)}",
                "available_keys": sorted(keymap), "calibration_key": calibration_key}
    tapped = []
    for c in digits:
        if _stop_requested(should_stop):
            return {"ok": False, "stopped": True, "digits_n": len(digits),
                    "completed_count": len(tapped), "tapped": tapped, "via": via}
        x, y = keymap[c]
        _adb("shell", "input", "tap", str(x), str(y))
        tapped.append({"digit": c, "at": [x, y]})
        if not _interruptible_sleep(0.35, should_stop):
            return {"ok": False, "stopped": True, "digits_n": len(digits),
                    "completed_count": len(tapped), "tapped": tapped, "via": via}
    result = {"ok": True, "digits": digits, "tapped": tapped, "via": via}
    if via == "calibrated":
        result["calibration_key"] = calibration_key
    if tap_confirm:
        conf = [e for e in elements if e["text"] == confirm_text and e["clickable"]]
        if conf:
            x, y = conf[0]["center"]
            _adb("shell", "input", "tap", str(x), str(y))
            result["confirmed"] = [x, y]
        else:
            result["confirm_warn"] = f"未找到确认键 {confirm_text}"
    return result


@mcp.tool()
def tap_digits(digits: str, tap_confirm: bool = False, confirm_text: str = "确定") -> dict:
    """在当前屏上数字键盘中逐位点击数字，可选点击确认键。"""
    return _tap_digits_impl(digits, tap_confirm, confirm_text)


def press_key(key: str) -> dict:
    """按系统键: wake / back / home / enter / delete。wake 只唤醒，不会把亮屏设备按灭。"""
    code = {"wake": 224, "back": 4, "home": 3, "enter": 66, "delete": 67}.get(key.lower())
    if code is None:
        return {"ok": False, "error": f"不支持的键: {key}"}
    _adb("shell", "input", "keyevent", str(code))
    return {"ok": True, "key": key}


@mcp.tool()
def long_press(x: int, y: int, duration_ms: int = 800) -> dict:
    """长按 (x,y)。"""
    _adb("shell", "input", "swipe", str(x), str(y), str(x), str(y), str(duration_ms))
    return {"action": "long_press", "x": x, "y": y, "duration_ms": duration_ms}


def _launch_app_impl(package: str = "", app_name: str = "", timeout_s: float = 5.0,
                     should_stop=None) -> dict:
    """按包名可靠启动，或按桌面当前可见的应用名精确点击；启动后必须核验前台。"""
    package = (package or "").strip()
    app_name = (app_name or "").strip()
    if not package and not app_name:
        return {"ok": False, "reason": "invalid_args",
                "error": "package 与 app_name 至少提供一个"}
    # 部分模型会把 schema 中两个可选字段一起填上。package 是不可歧义的系统标识，
    # 两者同时出现时安全地优先 package，避免模型围绕同一参数错误空转多轮。
    if package and app_name:
        app_name = ""

    def _focus_package(focused: str) -> str:
        # 兼容 "pkg/Activity" 与 dumpsys 的整行格式；只取最后一个包/Activity 结构。
        hits = re.findall(r"([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/[A-Za-z0-9_.$]+", focused or "")
        return hits[-1] if hits else ""

    def _wait_target(expected_package: str = "",
                     previous_package: str = "") -> tuple[str, str, bool]:
        deadline = time.monotonic() + max(float(timeout_s), 0.0)
        while True:
            if _stop_requested(should_stop):
                return "", "", True
            focused = get_focused_app()
            current = _focus_package(focused)
            reached = current == expected_package if expected_package else bool(current and current != previous_package)
            if reached:
                return focused, current, False
            if time.monotonic() >= deadline:
                return focused, current, False
            if not _interruptible_sleep(0.2, should_stop):
                return focused, current, True

    if _stop_requested(should_stop):
        return {"ok": False, "stopped": True, "action_executed": False}

    if app_name:
        # 系统 Launcher 的 a11y 标签就是显示名解析器：先查桌面；未命中时只做一次
        # 通用“上滑打开应用列表”再精确查找。全程不猜包名、不用视觉坐标，也不保存映射。
        home = press_key("home")
        if not home.get("ok"):
            return {"ok": False, "reason": "home_failed", "app_name": app_name, "detail": home}
        before_focus = get_focused_app()
        before_package = _focus_package(before_focus)
        launch_via = "launcher_a11y"
        label_source = ""

        def _tap_exact_label() -> dict:
            nonlocal label_source
            tapped_by_text = tap_element(text=app_name, contains=False)
            if tapped_by_text.get("ok"):
                label_source = "text"
                return tapped_by_text
            tapped_by_desc = tap_element(content_desc=app_name)
            if tapped_by_desc.get("ok"):
                label_source = "content_desc"
            return tapped_by_desc

        if _stop_requested(should_stop):
            return {"ok": False, "stopped": True, "action_executed": True}
        tapped = _tap_exact_label()
        if not tapped.get("ok"):
            if _stop_requested(should_stop):
                return {"ok": False, "stopped": True, "action_executed": True}
            width, height = _wm_size()
            if width and height:
                swipe(
                    round(width * 0.5), round(height * 0.8),
                    round(width * 0.5), round(height * 0.2), 300,
                )
                if not _interruptible_sleep(0.5, should_stop):
                    return {"ok": False, "stopped": True, "action_executed": True}
                tapped = _tap_exact_label()
                launch_via = "launcher_app_drawer"
            if not tapped.get("ok"):
                return {
                    "ok": False, "reason": "app_name_not_found", "app_name": app_name,
                    "searched": ["home", "app_drawer"],
                    "error": "桌面和一次通用应用列表查找均未发现精确显示名；不要猜包名或截图盲点",
                }
        focused, resolved, stopped = _wait_target(previous_package=before_package)
        if stopped:
            return {"ok": False, "stopped": True, "action_executed": True,
                    "focused_app": focused}
        if not resolved or resolved == before_package:
            return {"ok": False, "reason": "foreground_unchanged", "app_name": app_name,
                    "focused_app": focused}
        return {"ok": True, "app_name": app_name, "package": resolved,
                "focused_app": focused, "via": launch_via,
                "label_source": label_source}

    if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package):
        return {"ok": False, "reason": "invalid_package", "package": package}
    installed = _adb("shell", "pm", "path", package)
    if installed.returncode != 0 or "package:" not in (installed.stdout or ""):
        return {"ok": False, "reason": "not_installed", "package": package}

    resolved = _adb("shell", "cmd", "package", "resolve-activity", "--brief", package)
    components = re.findall(
        r"([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+/[A-Za-z0-9_.$]+)",
        (resolved.stdout or "") + "\n" + (resolved.stderr or ""),
    )
    activity = components[-1] if components else ""
    if resolved.returncode != 0 or not activity:
        return {"ok": False, "reason": "no_launcher_activity", "package": package}

    if _stop_requested(should_stop):
        return {"ok": False, "stopped": True, "action_executed": False}
    started = _adb("shell", "am", "start", "-W", "-n", activity)
    start_text = (started.stdout or "") + "\n" + (started.stderr or "")
    if started.returncode != 0 or re.search(r"\b(?:Error|Exception)\b", start_text, re.I):
        return {"ok": False, "reason": "start_failed", "package": package,
                "activity": activity, "error": start_text.strip()[:300]}
    focused, current, stopped = _wait_target(expected_package=package)
    if stopped:
        return {"ok": False, "stopped": True, "action_executed": True,
                "package": package, "activity": activity, "focused_app": focused}
    if current != package:
        return {"ok": False, "reason": "foreground_mismatch", "package": package,
                "activity": activity, "focused_app": focused}
    return {"ok": True, "package": package, "activity": activity,
            "focused_app": focused, "via": "package_manager"}


def launch_app(package: str = "", app_name: str = "", timeout_s: float = 5.0) -> dict:
    return _launch_app_impl(package, app_name, timeout_s)


def stop_app(package: str) -> dict:
    """强停应用(包名)。"""
    _adb("shell", "am", "force-stop", package)
    return {"ok": True, "package": package}


if __name__ == "__main__":
    mcp.run()
