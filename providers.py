"""Read-only local agent adapters. Retain only usage, identifiers and timing."""
from __future__ import annotations
import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROVIDERS = {"codex", "claude", "opencode", "zcode"}

def timestamp(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result = float(value) / 1000 if value > 100000000000 else float(value)
        else:
            result = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        # Keep enough headroom for timezone conversion near datetime's upper bound.
        return result if math.isfinite(result) and 0 <= result <= 253402214399 else None
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        return None

def json_object(value):
    if not isinstance(value, str): return {}
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError): return {}

def finite_token(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool): return False
    try: return math.isfinite(value) and value >= 0
    except (OverflowError, ValueError): return False

def number(value):
    return int(value) if finite_token(value) else 0

def identity(provider, native):
    return native if provider == "codex" else f"{provider}:{native}"

def record(provider, native, parent=None):
    sid = identity(provider, native)
    return {"id": sid, "meta": {"provider": provider, "native_session_id": native,
        "parent_thread_id": identity(provider, parent) if parent else None},
        "events": [], "completed": [], "live_turns": [], "is_subagent": bool(parent), "malformed": 0, "resets": 0}

def event(provider, native, key, ts, model, tid, inp, out, cached=0, reasoning=0, known=True):
    inp, out = number(inp), number(out)
    return {"key": (provider, native, key), "thread_id": identity(provider, native), "root_id": None,
        "provider": provider, "turn_id": tid, "timestamp": ts, "model": model or "unknown",
        "input_tokens": inp, "output_tokens": out, "cached_input_tokens": min(inp, number(cached)),
        "reasoning_output_tokens": min(out, number(reasoning)), "reasoning_known": known, "total_tokens": inp + out}

def start_turn(result, tid, ts):
    live = {"turn_id": tid, "started_at": ts, "start_event_at": ts,
            "activities": [ts], "model_contexts": [], "terminal_at": []}
    result["live_turns"].append(live)
    return live

def end_turn(result, live, ts, update=False):
    if live is None or ts is None or (live["terminal_at"] and not update): return
    if update:
        result["completed"] = [c for c in result["completed"] if c["turn_id"] != live["turn_id"]]
        live["terminal_at"] = [ts]
    else: live["terminal_at"].append(ts)
    if ts > live["started_at"]:
        result["completed"].append({"turn_id": live["turn_id"], "timestamp": ts,
            "seconds": ts - live["started_at"], "interval_start": live["started_at"],
            "started_at": live["started_at"], "ttft_ms": None})

def parse_claude(path):
    """Last valid assistant snapshot wins, including decreases after corrections."""
    path = Path(path); rows = []; malformed = 0
    with path.open("rb") as stream:
        for raw in stream:
            if not raw.endswith(b"\n"):
                continue
            try:
                row = json.loads(raw)
                if isinstance(row, dict): rows.append(row)
            except (ValueError, TypeError): malformed += 1
    native = next((r.get("sessionId") for r in rows if isinstance(r.get("sessionId"), str)), path.stem)
    agent = next((r.get("agentId") for r in rows if isinstance(r.get("agentId"), str)), None)
    # Child files use the parent's sessionId; never collapse their own ledger.
    if path.parent.name == "subagents" and (agent or path.stem.startswith("agent-")):
        parent = path.parent.parent.name
        if native != parent: parent = native
        agent = agent or path.stem.removeprefix("agent-")
        native = f"{parent}:agent:{agent}"
    else: parent = None
    result = record("claude", native, parent); result["malformed"] = malformed
    snapshots = {}; live = None; tid = None
    for row in rows:
        ts = timestamp(row.get("timestamp")); message = row.get("message") or {}
        if ts is None or not isinstance(message, dict):
            if ts is None: result["malformed"] += 1
            continue
        role = row.get("type") or message.get("role")
        content = message.get("content")
        human = isinstance(content, str) and bool(content.strip()) or isinstance(content, list) and any(isinstance(p, dict) and p.get("type") == "text" and p.get("text") for p in content)
        if role == "user" and human and not row.get("isMeta") and not row.get("isCompactSummary") and not (isinstance(content, list) and any(isinstance(p, dict) and p.get("type") == "tool_result" for p in content)):
            # A later human message starts another turn even after a missing ending.
            if live is not None and not live["terminal_at"]: live["terminal_at"].append(ts)
            tid = row.get("uuid") if isinstance(row.get("uuid"), str) else f"human-{ts}"; live = start_turn(result, tid, ts)
        if role != "assistant": continue
        mid = message.get("id")
        if not isinstance(mid, str):
            result["malformed"] += 1
            continue
        model_name = message.get("model") if isinstance(message.get("model"), str) else "unknown"
        duplicate_other_turn = mid in snapshots and snapshots[mid]["turn_id"] != tid
        if live is not None and not duplicate_other_turn:
            live["activities"].append(ts); live["model_contexts"].append((ts, model_name))
        u = message.get("usage"); mid = message.get("id")
        valid_usage = isinstance(u, dict) and all(finite_token(u.get(k)) for k in ("input_tokens", "output_tokens")) and all(finite_token(u[k]) for k in ("cache_creation_input_tokens", "cache_read_input_tokens") if k in u)
        if isinstance(u, dict) and not valid_usage: result["malformed"] += 1
        if valid_usage and isinstance(mid, str) and any(k in u for k in ("input_tokens", "output_tokens")):
            inp = number(u.get("input_tokens")) + number(u.get("cache_creation_input_tokens")) + number(u.get("cache_read_input_tokens"))
            detail = u.get("output_tokens_details") or {}
            known = isinstance(detail, dict) and finite_token(detail.get("thinking_tokens"))
            # Duplicate message snapshots retain the originally associated human turn.
            original_tid = snapshots[mid]["turn_id"] if mid in snapshots else tid
            event_ts = snapshots[mid]["timestamp"] if duplicate_other_turn else ts
            snapshots[mid] = event("claude", native, mid, event_ts, model_name, original_tid,
                inp, u.get("output_tokens"), u.get("cache_read_input_tokens"), detail.get("thinking_tokens") if isinstance(detail, dict) else 0, known)
        if message.get("stop_reason") == "end_turn" and not duplicate_other_turn and valid_usage: end_turn(result, live, ts, update=bool(live and live["terminal_at"]))
    result["events"] = [e for e in snapshots.values() if e["total_tokens"]]
    return result

def paseo_links(home):
    links = {}; warnings = []
    base = Path(home) / "agents"
    if not base.exists(): return links, warnings
    try:
        paths = list(base.rglob("*.json"))
    except OSError:
        return links, ["Paseo 关联目录无法读取。"]
    for path in paths:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            persistence = row.get("persistence") or {}
            top, provider = row.get("provider"), persistence.get("provider")
            if top and provider and top != provider:
                warnings.append("Paseo 记录的代理工具字段冲突，已跳过关联。")
                continue
            provider = provider or top; native = persistence.get("sessionId")
            if provider in PROVIDERS and isinstance(native, str):
                title = row.get("title")
                links[(provider, native)] = {"title": title[:120] if isinstance(title, str) else None}
        except (OSError, ValueError, TypeError, AttributeError):
            warnings.append("部分 Paseo 关联记录无法读取，下次刷新会重试。")
    return links, list(dict.fromkeys(warnings))

def opencode_tokens(tokens):
    if not isinstance(tokens, dict) or not all(k in tokens for k in ("input", "output", "reasoning", "cache")) or not isinstance(tokens.get("cache"), dict): return None
    cache = tokens["cache"]
    if not all(k in cache for k in ("read", "write")): return None
    if not all(finite_token(v) for v in (tokens["input"], tokens["output"], tokens["reasoning"], cache["read"], cache["write"])): return None
    # Current OpenCode reports five mutually exclusive token buckets.
    return (number(tokens["input"]) + number(cache["read"]) + number(cache["write"]),
        number(tokens["output"]) + number(tokens["reasoning"]), number(cache["read"]), number(tokens["reasoning"]))

def parse_opencode(path):
    """SQLite 1.x current semantics and 2.x idle-delimited turns; never write DB."""
    records = {}; warnings = []; conn = None
    try:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        def columns(table): return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        def rows(table, fields):
            # Select only allowlisted metadata/usage JSON paths, never body/text.
            present = columns(table)
            selected = [f for f in fields if f in present]
            for row in conn.execute('SELECT ' + ','.join('"' + f + '"' for f in selected) + ' FROM "' + table + '"'):
                yield dict(zip(selected, row))
        has_v2 = {"session_v2", "session_message"} <= tables
        valid_v2 = has_v2 and {"id"} <= columns("session_v2") and {"id", "session_id", "type", "seq", "time_created", "data"} <= columns("session_message")
        if has_v2 and not valid_v2: warnings.append("OpenCode 2.x 数据结构不受支持，已跳过该来源。")
        if valid_v2:
            for s in rows("session_v2", ("id", "parent_id", "title")):
                if not isinstance(s.get("id"), str): continue
                r = record("opencode", s["id"], s.get("parent_id")); r["meta"]["title"] = s.get("title"); records[s["id"]] = r
            query = "SELECT session_id,type,seq,time_created,json_extract(data,'$.tokens'),json_extract(data,'$.model'),json_extract(data,'$.role'),json_extract(data,'$.time'),json_extract(data,'$.outcome'),json_extract(data,'$.status') FROM session_message WHERE json_valid(data) ORDER BY session_id,seq"
            if conn.execute("SELECT count(*) FROM session_message WHERE NOT json_valid(data)").fetchone()[0]: warnings.append("部分 OpenCode 记录 JSON 无效，已跳过。")
            live = {}
            for sid, typ, seq, created, token_json, model_json, role, times_json, outcome, status in conn.execute(query):
                if not isinstance(sid, str) or sid not in records: continue
                r = records[sid]; ts = timestamp(created)
                if ts is None: continue
                typ = str(typ); times = json_object(times_json); tokens = json_object(token_json)
                model = json_object(model_json)
                model_name = model.get("id") if isinstance(model.get("id"), str) else "unknown"
                if typ == "compaction" and status in ("completed", "failed") and opencode_tokens(tokens) is not None and sum(opencode_tokens(tokens)[:2]) > 0:
                    warnings.append("OpenCode 2.x 压缩请求用量缺少可信完成时间，未计入；统计不完整。")
                    if live.get(sid) is not None: live[sid]["speed_incomplete"] = True
                if token_json is not None and opencode_tokens(tokens) is None and (typ == "assistant" or (typ == "compaction" and status in ("completed", "failed"))):
                    warnings.append("部分 OpenCode 用量记录字段无效，已跳过。")
                    if live.get(sid) is not None: live[sid]["speed_incomplete"] = True
                if typ in ("user", "user-message", "message.user") or role == "user":
                    if live.get(sid) is None:
                        tid = f"seq-{seq}"; live[sid] = start_turn(r, tid, ts)
                current = live.get(sid)
                if current:
                    current["activities"].append(ts); current["model_contexts"].append((ts, model_name or "unknown"))
                u = opencode_tokens(tokens) if typ == "assistant" else None
                usage_ts = timestamp(times.get("streamed")) or timestamp(times.get("completed"))
                if u and sum(u[:2]) > 0 and usage_ts is None:
                    warnings.append("部分 OpenCode 响应用量缺少可信完成时间，已跳过。")
                    if current is not None: current["speed_incomplete"] = True
                if u and sum(u[:2]) > 0 and usage_ts is not None:
                    r["events"].append(event("opencode", sid, f"message-{seq}", usage_ts, model_name, current["turn_id"] if current else None, *u))
                if typ in ("idle", "session.idle", "status.idle"):
                    
                    if outcome == "succeeded" and not (current and current.get("speed_incomplete")): end_turn(r, current, ts)
                    elif current: current["terminal_at"].append(ts)
                    live[sid] = None
            # Missing idle does not prove the session is still running.
            for r in records.values(): r["live_turns"] = []
        has_v1 = {"session", "message", "part"} <= tables
        valid_v1 = has_v1 and {"id", "version"} <= columns("session") and {"id", "session_id", "time_created", "data"} <= columns("message") and {"id", "session_id", "message_id", "time_created", "data"} <= columns("part")
        if has_v1 and not valid_v1: warnings.append("OpenCode 1.x 数据结构不受支持，已跳过该来源。")
        if valid_v1:
            supported = set()
            for s in rows("session", ("id", "parent_id", "title", "version")):
                if s["id"] in records: continue
                version = tuple(int(n) for n in re.findall(r"\d+", str(s.get("version") or ""))[:3])
                if version < (1, 14, 46) or version[0] != 1:
                    warnings.append("OpenCode 旧版或未知版本的 token 口径未经确认，已跳过该会话。")
                    continue
                supported.add(s["id"]); r = record("opencode", s["id"], s.get("parent_id")); r["meta"]["title"] = s.get("title"); records[s["id"]] = r
            steps = set()
            if any(conn.execute(f"SELECT count(*) FROM {table} WHERE NOT json_valid(data)").fetchone()[0] for table in ("message", "part")): warnings.append("部分 OpenCode 记录 JSON 无效，已跳过。")
            query = "SELECT id,session_id,message_id,time_created,json_extract(data,'$.type'),json_extract(data,'$.tokens') FROM part WHERE json_valid(data)"
            for pid, sid, mid, created, typ, token_json in conn.execute(query):
                if sid not in supported or typ != "step-finish" or not isinstance(pid, str) or not isinstance(mid, str): continue
                u = opencode_tokens(json_object(token_json)) if token_json else None; ts = timestamp(created)
                if u and ts is not None:
                    steps.add((sid, mid))
                    if sum(u[:2]) > 0: records[sid]["events"].append(event("opencode", sid, "part-" + pid, ts, "unknown", mid, *u))
            query = "SELECT id,session_id,time_created,json_extract(data,'$.role'),json_extract(data,'$.tokens'),json_extract(data,'$.modelID'),json_extract(data,'$.time.completed') FROM message WHERE json_valid(data)"
            for mid, sid, created, role, token_json, model, completed_at in conn.execute(query):
                if sid not in supported or role != "assistant" or not isinstance(mid, str): continue
                model = model if isinstance(model, str) else "unknown"
                for e in records[sid]["events"]:
                    if e["turn_id"] == mid: e["model"] = model or "unknown"
                u = opencode_tokens(json_object(token_json)) if token_json else None; ts = timestamp(completed_at)
                if (sid, mid) not in steps and u and sum(u[:2]) > 0 and ts is None: warnings.append("部分 OpenCode 响应用量缺少可信完成时间，已跳过。")
                if (sid, mid) not in steps and u and sum(u[:2]) > 0 and ts is not None:
                    records[sid]["events"].append(event("opencode", sid, "message-" + mid, ts, model, mid, *u))
            if records: warnings.append("OpenCode 1.x 已统计用量；其 step 完成不等于整个用户轮次完成，暂不提供轮次速度或正在生成状态。")
            return list(records.values()), list(dict.fromkeys(warnings))
        if records: return list(records.values()), list(dict.fromkeys(warnings))
        return [], list(dict.fromkeys(warnings)) or ["OpenCode 数据库结构不受支持，已跳过。"]
    except (sqlite3.Error, ValueError, TypeError, OSError, KeyError):
        return list(records.values()), list(dict.fromkeys(warnings + ["OpenCode 数据库无法读取或结构不受支持，其他代理统计不受影响。"]))
    finally:
        if conn: conn.close()


def parse_zcode(path):
    """Read only the canonical request ledger and completed turn boundaries."""
    records = {}; warnings = []; conn = None
    try:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
        required = {
            "session": {"id", "parent_id"},
            "model_usage": {"id", "session_id", "turn_id", "model_id", "status", "completed_at", "input_tokens", "output_tokens", "reasoning_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "provider_total_tokens", "computed_total_tokens"},
            "turn_usage": {"session_id", "turn_id", "status", "started_at", "completed_at", "model_request_count", "output_tokens"},
        }
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if any(table not in tables or not fields <= {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')} for table, fields in required.items()):
            return [], ["Z Code 数据库结构不受支持，已跳过该来源。"]
        for sid, parent in conn.execute("SELECT id,parent_id FROM session"):
            if isinstance(sid, str) and (parent is None or isinstance(parent, str)):
                records[sid] = record("zcode", sid, parent)
        counts = {}; outputs = {}; times = {}; incomplete = set()
        query = "SELECT id,session_id,turn_id,model_id,status,completed_at,input_tokens,output_tokens,reasoning_tokens,cache_creation_input_tokens,cache_read_input_tokens,provider_total_tokens,computed_total_tokens FROM model_usage"
        for rid, sid, tid, model, status, finished, inp, out, reasoning, creation, cached, provider_total, computed_total in conn.execute(query):
            if sid not in records or not isinstance(rid, str) or (tid is not None and not isinstance(tid, str)): continue
            key = (sid, tid)
            values = (inp, out, reasoning, creation, cached)
            ts = timestamp(finished)
            if not all(finite_token(v) for v in values) or ts is None or status not in ("completed", "error", "cancelled"):
                if tid is not None: incomplete.add(key)
                if any(number(v) for v in values): warnings.append("部分 Z Code 用量缺少有效字段或完成时间，已跳过。")
                continue
            inp, out, reasoning, creation, cached = map(number, values)
            cache = creation + cached
            total = provider_total if provider_total is not None else computed_total
            if inp <= 0: inp = cache
            elif cache > 0 and finite_token(total) and total > 0 and abs(total - (inp + cache + out)) < abs(total - (inp + out)): inp += cache
            if inp + out:
                turn_id = tid if tid is not None else "request:" + rid
                model = model if isinstance(model, str) else "unknown"
                records[sid]["events"].append(event("zcode", sid, rid, ts, model, turn_id, inp, out, cached, reasoning))
            if tid is not None:
                counts[key] = counts.get(key, 0) + 1
                outputs[key] = outputs.get(key, 0) + out
                times.setdefault(key, []).append(ts)
        for sid, tid, status, started, finished, count, output in conn.execute("SELECT session_id,turn_id,status,started_at,completed_at,model_request_count,output_tokens FROM turn_usage"):
            if sid in records and isinstance(tid, str) and status in ("completed", "error", "failed", "cancelled"):
                records[sid].setdefault("_terminal_turns", []).append((tid, timestamp(finished)))
            key = (sid, tid); start = timestamp(started); end = timestamp(finished)
            if sid not in records or not isinstance(tid, str) or status != "completed" or start is None or end is None or end <= start: continue
            if key in incomplete or not finite_token(count) or not finite_token(output) or counts.get(key, 0) != count or outputs.get(key, 0) != output or any(ts < start or ts > end for ts in times.get(key, [])): continue
            live = start_turn(records[sid], tid, start); end_turn(records[sid], live, end)
        for result in records.values(): result["live_turns"] = []
        return list(records.values()), list(dict.fromkeys(warnings))
    except (sqlite3.Error, ValueError, TypeError, OSError, KeyError):
        return [], ["Z Code 数据库无法读取或结构不受支持，其他代理统计不受影响。"]
    finally:
        if conn: conn.close()


def parse_zcode_live(log_dir, now, cache):
    """Read recent lifecycle metadata only; log entries never become usage."""
    today = now.astimezone(timezone.utc).date()
    turns = {}; present = set()
    activity = {"turn.phase.started", "turn.phase.completed", "model.request.started",
                "model.request.completed", "model.request.failed", "tool.call.started",
                "tool.call.completed", "tool.call.failed", "session.event.persistence.completed"}
    terminal = {"turn.completed", "turn.failed"}
    for offset in (-2, -1, 0, 1):
        path = Path(log_dir) / f"zcode-{today + timedelta(days=offset)}.jsonl"
        try:
            stat = path.stat(); version = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
            present.add(path)
            if path not in cache or cache[path][0] != version:
                entries = []
                with path.open("rb") as stream:
                    for raw in stream:
                        if not raw.endswith(b"\n"): continue
                        try: row = json.loads(raw)
                        except (ValueError, TypeError): continue
                        if not isinstance(row, dict): continue
                        kind = row.get("event"); sid = row.get("sessionId"); tid = row.get("turnId")
                        ts = timestamp(row.get("timestamp"))
                        if kind not in activity | terminal | {"turn.started"} or not isinstance(sid, str) or not sid or not isinstance(tid, str) or not tid or ts is None: continue
                        context = row.get("context")
                        model = context.get("modelId") if isinstance(context, dict) else None
                        if kind == "session.event.persistence.completed" and (not isinstance(context, dict) or context.get("sessionEventType") != "model_request"): continue
                        entries.append((sid, tid, ts, kind, model if isinstance(model, str) and model else None))
                cache[path] = (version, entries)
            for sid, tid, ts, kind, model in cache[path][1]:
                if ts <= now.timestamp(): turns.setdefault((sid, tid), []).append((ts, kind, model))
        except OSError:
            cache.pop(path, None)
    for path in set(cache) - present: del cache[path]
    records = {}
    for (sid, tid), entries in turns.items():
        entries.sort(key=lambda entry: entry[0])
        starts = [ts for ts, kind, _ in entries if kind == "turn.started"]
        if not starts: continue
        start = min(starts)
        result = records.setdefault(sid, record("zcode", sid))
        live = start_turn(result, tid, start)
        for ts, kind, model in entries:
            if ts < start: continue
            if kind in terminal: live["terminal_at"].append(ts)
            elif kind in activity:
                live["activities"].append(ts)
                if model: live["model_contexts"].append((ts, model))
    return list(records.values())
