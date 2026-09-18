"""离线聚合器：从 legacy run-web-* 或 Companion UUID Run 派生时间/token/行为指标。
用法: cd device-mcp/agent && ../.venv/bin/python analyze_runs.py [最近N个run|runs目录]
指标定义与决策映射见 观测与优化.md。旧 run 缺新流时对应指标自动标"无数据"。"""
import glob
import json
import os
import sys
from collections import Counter, defaultdict

from runtime_paths import data_root
BASE = str(data_root())


def _jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path, encoding="utf-8"):
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def _discover(base, n):
    legacy = glob.glob(os.path.join(base, "run-web-*"))
    companion = glob.glob(os.path.join(base, "*", "attempts", "*"))
    if os.path.basename(base) == "attempts":
        companion += glob.glob(os.path.join(base, "*"))
    elif os.path.isfile(os.path.join(base, "manifest.json")):
        companion += glob.glob(os.path.join(base, "attempts", "*"))
    return sorted({d for d in legacy + companion if os.path.isdir(d)}, reverse=True)[:n]


def _run_name(path):
    if os.path.basename(os.path.dirname(path)) == "attempts":
        return f"{os.path.basename(os.path.dirname(os.path.dirname(path)))}/{os.path.basename(path)}"
    return os.path.basename(path).removeprefix("run-web-")


def _percentile(values, fraction):
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * fraction))]


def _operator_performance(records):
    """Derive direct-operator latency without reading prompts or response bodies."""
    groups = defaultdict(list)
    for record in records:
        if record.get("scope") != "device_operator":
            continue
        key = (
            record.get("suite_id") or "",
            record.get("operator_id") or "",
            record.get("origin_ts") or "",
        )
        groups[key].append(record)

    metrics = {
        "operator_count": 0,
        "connect_ms": [], "first_assistant_ms": [], "first_tool_ms": [],
        "tool_roundtrip_ms": [], "tool_dispatch_ms": [], "tool_and_hook_ms": [],
        "tool_result_delivery_ms": [], "model_gap_ms": [], "tail_ms": [],
        "observer_cpu_during_gap_ms": [], "cli_cpu_during_gap_ms": [],
        "system_busy_during_gap_pct": [],
        "prompt_chars": [], "current_step_chars": [],
        "result_wall_ms": [], "result_api_ms": [],
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }

    def elapsed(record):
        value = record.get("elapsed_ms")
        return float(value) if isinstance(value, (int, float)) else None

    def append_duration(target, start, end):
        left, right = elapsed(start), elapsed(end)
        if left is not None and right is not None and right >= left:
            target.append(round(right - left, 3))

    def add_resource_delta(target, start, end, field):
        left, right = start.get(field), end.get(field)
        if all(isinstance(value, (int, float)) for value in (left, right)) and right >= left:
            target.append(round(right - left, 3))

    for items in groups.values():
        ordered = sorted(items, key=lambda item: elapsed(item) or 0)
        first = {}
        for item in ordered:
            first.setdefault(item.get("phase"), item)
        started = first.get("operator_started")
        if not started:
            continue
        metrics["operator_count"] += 1
        for field in ("prompt_chars", "current_step_chars"):
            value = started.get(field)
            if isinstance(value, (int, float)):
                metrics[field].append(value)

        if first.get("operator_connect_started") and first.get("operator_connect_completed"):
            append_duration(
                metrics["connect_ms"], first["operator_connect_started"],
                first["operator_connect_completed"],
            )
        query = first.get("operator_query_submitted") or first.get("operator_query_started")
        if query and first.get("model_first_assistant_message"):
            append_duration(metrics["first_assistant_ms"], query,
                            first["model_first_assistant_message"])
        if query and first.get("operator_tool_use_emitted"):
            append_duration(metrics["first_tool_ms"], query,
                            first["operator_tool_use_emitted"])

        emitted = {
            item.get("tool_use_id"): item for item in ordered
            if item.get("phase") == "operator_tool_use_emitted" and item.get("tool_use_id")
        }
        hook_pre = {
            item.get("tool_use_id"): item for item in ordered
            if item.get("phase") == "operator_hook_pre_received" and item.get("tool_use_id")
        }
        hook_post = {
            item.get("tool_use_id"): item for item in ordered
            if item.get("phase") in {
                "operator_hook_post_received", "operator_hook_failure_received",
            } and item.get("tool_use_id")
        }
        tool_results = [
            item for item in ordered if item.get("phase") == "operator_tool_result_received"
        ]
        for result in tool_results:
            use = emitted.get(result.get("tool_use_id"))
            if use:
                append_duration(metrics["tool_roundtrip_ms"], use, result)
            pre = hook_pre.get(result.get("tool_use_id"))
            post = hook_post.get(result.get("tool_use_id"))
            if use and pre:
                append_duration(metrics["tool_dispatch_ms"], use, pre)
            if pre and post:
                append_duration(metrics["tool_and_hook_ms"], pre, post)
            if post:
                append_duration(metrics["tool_result_delivery_ms"], post, result)

        pending_result = None
        for item in ordered:
            phase = item.get("phase")
            if phase == "operator_tool_result_received":
                pending_result = item
            elif phase == "operator_tool_use_emitted" and pending_result is not None:
                append_duration(metrics["model_gap_ms"], pending_result, item)
                add_resource_delta(
                    metrics["observer_cpu_during_gap_ms"], pending_result, item,
                    "observer_cpu_ms",
                )
                add_resource_delta(
                    metrics["cli_cpu_during_gap_ms"], pending_result, item,
                    "cli_process_cpu_ms",
                )
                idle_a, idle_b = pending_result.get("system_idle_ms"), item.get("system_idle_ms")
                total_a = (
                    pending_result.get("system_kernel_ms", 0)
                    + pending_result.get("system_user_ms", 0)
                )
                total_b = item.get("system_kernel_ms", 0) + item.get("system_user_ms", 0)
                if all(isinstance(value, (int, float)) for value in (
                        idle_a, idle_b, total_a, total_b)):
                    total_delta = total_b - total_a
                    idle_delta = idle_b - idle_a
                    if total_delta > 0 and 0 <= idle_delta <= total_delta:
                        metrics["system_busy_during_gap_pct"].append(round(
                            100 * (total_delta - idle_delta) / total_delta, 1,
                        ))
                pending_result = None

        result = first.get("operator_result_received")
        if result:
            if tool_results:
                append_duration(metrics["tail_ms"], tool_results[-1], result)
            for source, target in (
                    ("duration_ms", "result_wall_ms"), ("api_ms", "result_api_ms")):
                value = result.get(source)
                if isinstance(value, (int, float)):
                    metrics[target].append(value)
            for field in (
                    "input_tokens", "output_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens"):
                value = result.get(field)
                if isinstance(value, (int, float)):
                    metrics[field] += value
    return metrics


def main(n=20, base=BASE):
    runs = _discover(os.path.realpath(base), n)
    if not runs:
        print(f"未找到 Run：{os.path.realpath(base)}")
        return
    print(f"聚合最近 {len(runs)} 个 run（{_run_name(runs[-1])} → {_run_name(runs[0])}）\n")

    verd = Counter()
    baseline = defaultdict(list)          # seq → [(run, verdict, turns, dur_s, api_s, cost)]
    tool_ms, tool_n = Counter(), Counter()
    out_chars = Counter()
    img_n = img_tok = 0
    wait_waste_s = 0.0
    repeats = 0                            # 连续同工具+同参数摘要(循环/原地重试信号)
    redundant_shots = 0                    # wait_for/find 命中后紧跟 screenshot(疑似冗余取证)
    tuc = []                               # tap_until_change 结果
    turn_usage = []                        # (i, input, output, cache_read, cache_create)
    fixed = []                             # (skill_chars, notes_chars, prompt_chars)
    case_time = []                         # (dur_ms, api_ms)
    query_first_message = []               # phase_timing: sdk_query_started → first_sdk_message
    query_first_content = []               # phase_timing: sdk_query_started → first_assistant_content
    operator_phase_records = []             # direct operator 的独立 SDK/工具/本地资源时间线
    # 按推理深度分组:供 effort 档位的成本/时延 A/B。
    # 2026-07-31 前 case_start 不记 effort,当时代码里是写死的 medium——故缺字段一律按 medium 读,
    # 不回改历史 run 的 turns.jsonl(日志只留当时真写下的东西,事后推断在这一层补)。
    eff_time = defaultdict(list)           # effort → [(dur_ms, api_ms)]

    for d in runs:
        rn = _run_name(d)
        sj = os.path.join(d, "summary.json")
        if os.path.exists(sj):
            try:
                s = json.load(open(sj))
                for k, v in s.get("verdicts", {}).items():
                    verd[k] += v
                for pc in s.get("per_case", []):
                    baseline[pc["seq"]].append((rn, pc.get("verdict"), pc.get("turns"),
                                                pc.get("duration_s"), pc.get("api_s"), pc.get("cost_usd")))
            except Exception:
                pass
        for cd in glob.glob(os.path.join(d, "trace", "case*")):
            # --- 流A: toolcalls ---
            evs = _jsonl(os.path.join(cd, "toolcalls.jsonl"))
            prev = None
            for e in evs:
                t = e.get("tool", "?")
                tool_ms[t] += e.get("ms", 0)
                tool_n[t] += 1
                out_chars[t] += e.get("out_chars", 0)
                if e.get("has_img") or e.get("img_tok_est"):
                    img_n += 1
                    img_tok += e.get("img_tok_est", 0)
                sig = e.get("sig", {})
                if t == "wait_for" and sig.get("found") is False:
                    wait_waste_s += float(sig.get("waited_s") or 0)
                if t == "tap_until_change":
                    tuc.append(sig)
                if prev and prev.get("tool") == t and prev.get("args") == e.get("args"):
                    repeats += 1
                if t == "screenshot" and prev and prev.get("tool") in ("wait_for", "find"):
                    ps = prev.get("sig", {})
                    if ps.get("found") is True or (ps.get("count") or 0) > 0:
                        redundant_shots += 1
                prev = e
            # --- 流B/C: turns ---
            eff = "medium"   # 缺 effort 字段=07-31 前的 run，当时写死 medium（见上方说明）
            for r in _jsonl(os.path.join(cd, "turns.jsonl")):
                if r.get("type") == "turn" and isinstance(r.get("usage"), dict):
                    u = r["usage"]
                    turn_usage.append((r.get("i", 0), u.get("input_tokens", 0), u.get("output_tokens", 0),
                                       u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0)))
                elif r.get("type") == "case_start":
                    fixed.append((r.get("skill_chars", 0), r.get("notes_chars", 0), r.get("prompt_chars", 0)))
                    eff = r.get("effort") or "medium"   # case_start 在 result 之前，故下面取得到
                elif r.get("type") == "result" and r.get("duration_ms"):
                    case_time.append((r["duration_ms"], r.get("api_ms") or 0))
                    eff_time[eff].append((r["duration_ms"], r.get("api_ms") or 0))
            groups = defaultdict(dict)
            for r in _jsonl(os.path.join(cd, "phase_timing.jsonl")):
                key = r.get("execution_id") or r.get("turn_id") or cd
                groups[key].setdefault(r.get("phase"), r.get("elapsed_ms"))
            for phases in groups.values():
                start = phases.get("sdk_query_started")
                if start is None:
                    continue
                if phases.get("first_sdk_message") is not None:
                    query_first_message.append(phases["first_sdk_message"] - start)
                if phases.get("first_assistant_content") is not None:
                    query_first_content.append(phases["first_assistant_content"] - start)
        # interactive 使用同一阶段协议，但目录不叫 caseN。
        groups = defaultdict(dict)
        interactive_phases = _jsonl(
            os.path.join(d, "trace", "interactive", "phase_timing.jsonl")
        )
        operator_phase_records.extend(interactive_phases)
        for r in interactive_phases:
            key = r.get("turn_id") or "session"
            groups[key].setdefault(r.get("phase"), r.get("elapsed_ms"))
        for phases in groups.values():
            start = phases.get("sdk_query_started")
            if start is None:
                continue
            if phases.get("first_sdk_message") is not None:
                query_first_message.append(phases["first_sdk_message"] - start)
            if phases.get("first_assistant_content") is not None:
                query_first_content.append(phases["first_assistant_content"] - start)

    tot = sum(verd.values())
    print("== 判决 ==")
    print("  " + "  ".join(f"{k}:{verd[k]}" for k in ("pass", "fail", "blocked", "needs_review", "cancelled")) + f"  计{tot}")

    print("\n== 时间三分账 ==")
    if case_time:
        dur = sum(t[0] for t in case_time)
        api = sum(t[1] for t in case_time)
        print(f"  已记 {len(case_time)} case: wall 总{dur/1000:.0f}s；SDK 汇总 api_ms {api/1000:.0f}s")
        print("  注意: SDK api_ms 可能大于 wall 且与工具阶段重叠，禁止用 wall-api 精确推导设备耗时")
    else:
        print("  (无数据——需新版 turns.jsonl 的 run)")
    if tool_ms:
        print("  工具记录耗时 top(可能与 SDK 汇总重叠): " + "  ".join(f"{t}:{tool_ms[t]/1000:.0f}s×{tool_n[t]}" for t, _ in tool_ms.most_common(6)))
        print(f"  wait_for miss 空转合计: {wait_waste_s:.0f}s")
    if query_first_message:
        print(f"  sdk_query→首SDK消息: p50={_percentile(query_first_message, .5):.0f}ms "
              f"p95={_percentile(query_first_message, .95):.0f}ms n={len(query_first_message)}")
    if query_first_content:
        print(f"  sdk_query→首内容:    p50={_percentile(query_first_content, .5):.0f}ms "
              f"p95={_percentile(query_first_content, .95):.0f}ms n={len(query_first_content)}")
    operator = _operator_performance(operator_phase_records)
    if operator["operator_count"]:
        print("  direct operator:")
        for label, key in (
                ("connect", "connect_ms"),
                ("query→首Assistant", "first_assistant_ms"),
                ("query→首工具", "first_tool_ms"),
                ("工具结果→下一工具(模型交接)", "model_gap_ms"),
                ("工具发出→结果回到SDK", "tool_roundtrip_ms"),
                ("工具发出→Hook收到", "tool_dispatch_ms"),
                ("Hook前置→工具返回", "tool_and_hook_ms"),
                ("工具返回→SDK收到结果", "tool_result_delivery_ms"),
                ("末工具结果→operator结束", "tail_ms")):
            values = operator[key]
            if values:
                print(f"    {label}: 总{sum(values)/1000:.1f}s "
                      f"p50={_percentile(values, .5):.0f}ms "
                      f"p95={_percentile(values, .95):.0f}ms n={len(values)}")
        gap_wall = sum(operator["model_gap_ms"])
        if gap_wall:
            observer_cpu = sum(operator["observer_cpu_during_gap_ms"])
            cli_cpu = sum(operator["cli_cpu_during_gap_ms"])
            print(f"    模型交接期间本地累计CPU: Python {observer_cpu:.0f}ms / "
                  f"CLI {cli_cpu:.0f}ms / 墙钟 {gap_wall:.0f}ms")
        if operator["system_busy_during_gap_pct"]:
            busy = operator["system_busy_during_gap_pct"]
            print(f"    Windows系统忙碌率(模型交接采样): "
                  f"p50={_percentile(busy, .5):.0f}% p95={_percentile(busy, .95):.0f}%")
        if operator["result_wall_ms"]:
            print(f"    operator Result: wall {sum(operator['result_wall_ms'])/1000:.1f}s / "
                  f"SDK api {sum(operator['result_api_ms'])/1000:.1f}s")
        if operator["prompt_chars"]:
            print(f"    operator prompt: p50={_percentile(operator['prompt_chars'], .5):.0f}字；"
                  f"current_step p50={_percentile(operator['current_step_chars'], .5):.0f}字")
        token_total = operator["input_tokens"] + operator["cache_read_input_tokens"]
        print(f"    operator token: input {operator['input_tokens']} / "
              f"output {operator['output_tokens']} / cache_read "
              f"{operator['cache_read_input_tokens']} / cache_write "
              f"{operator['cache_creation_input_tokens']} "
              f"(cache命中率 {100*operator['cache_read_input_tokens']/max(1, token_total):.0f}%)")
    if eff_time:
        print("  按推理深度(effort):")
        for e in ("low", "medium", "high", "xhigh", "max"):
            if e not in eff_time:
                continue
            v = eff_time[e]
            print(f"    {e:<7} {len(v):>3} case  单条均 {sum(x[0] for x in v)/len(v)/1000:6.0f}s"
                  f"  (其中 LLM {sum(x[1] for x in v)/len(v)/1000:.0f}s)")
        if set(eff_time) == {"medium"}:
            print("    (只有一档,无从对比。07-31 前的 run 不记 effort,按当时写死的 medium 计)")

    print("\n== token ==")
    if turn_usage:
        k = len(turn_usage)
        inp = sum(u[1] for u in turn_usage); outp = sum(u[2] for u in turn_usage)
        cr = sum(u[3] for u in turn_usage); cw = sum(u[4] for u in turn_usage)
        print(f"  已记 {k} 轮: input {inp} / output {outp} / cache_read {cr} / cache_write {cw}  (cache命中率 {100*cr/max(1,cr+inp):.0f}%)")
        by_i = defaultdict(list)
        for i, a, *_ in turn_usage:
            by_i[min(i, 15)].append(a)
        pts = sorted((i, sum(v)/len(v)) for i, v in by_i.items())
        if len(pts) >= 3:
            slope = (pts[-1][1] - pts[0][1]) / max(1, pts[-1][0] - pts[0][0])
            print(f"  上下文增长斜率 ≈ {slope:.0f} input-tok/轮 (陡=大返回体滞留上下文)")
    else:
        print("  (无每轮数据——需新版 turns.jsonl 的 run)")
    if img_n:
        print(f"  截图 {img_n} 张, 视觉token估算合计 {img_tok} (≈{img_tok//max(1,img_n)}/张)")
    if out_chars:
        print("  返回体字符 top: " + "  ".join(f"{t}:{c//1000}k" for t, c in out_chars.most_common(5)))
    if fixed:
        sk, no, pr = (sum(x)//len(fixed) for x in zip(*fixed))
        print(f"  每case固定注入(均): SKILL {sk}字 + notes {no}字 + prompt {pr}字")

    print("\n== 行为 ==")
    print(f"  连续同工具同参数重复: {repeats} 次 (循环/原地重试信号)")
    print(f"  命中后疑似冗余截图: {redundant_shots} 次 (wait_for/find 已命中又紧跟 screenshot)")
    if tuc:
        hits = [t for t in tuc if t.get("changed")]
        print(f"  tap_until_change: {len(tuc)} 次调用, 命中 {len(hits)}, 平均尝试 {sum(t.get('tries', 0) for t in tuc)/len(tuc):.1f}")

    print("\n== 同 seq 跨 run 基线(回归探测) ==")
    for seq in sorted(baseline):
        rows = baseline[seq][:6]
        cells = "  ".join(f"{r[0][-6:]}:{(r[1] or '?')[:4]}/{r[2]}轮/{r[3]}s" for r in rows)
        print(f"  seq{seq}: {cells}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "20"
    main(int(arg)) if arg.isdigit() else main(20, arg)
