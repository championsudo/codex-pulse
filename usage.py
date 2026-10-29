"""Read-only Codex rollout analytics. No prompts or responses leave the parser."""
from __future__ import annotations

import json
import math
import os
import re
import statistics
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from providers import PROVIDERS, identity, parse_claude, parse_opencode, parse_zcode, parse_zcode_live, paseo_links

FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
SPEED_NOTE = ("平均轮次速度 = 已完成轮次的输出 token 总和 ÷ 轮次耗时之和；合计吞吐 = 同一批轮次的输出 token 总和 ÷ 时间区间并集秒数。"
              "时间重叠只计一次、空闲间隙不计；两者均为历史统计，包含思考、工具执行和等待，不是实时速度或纯模型解码速度。"
              "token 按用量事件日期统计，速度按轮次完成日期统计，跨日期或筛选边界的轮次保留完整输出及耗时。"
              "缺少完成记录的轮次不计速度；已有完成记录但完成日期在范围外的轮次只计范围内用量。"
              "首 Token 等待取日志中的轮次级记录，不等同于每次请求的延迟。")
SCOPES = {"task": "主对话与子代理（不含自动审批）", "all": "全部线程（含自动审批）",
          "main": "仅主对话", "agent": "仅子代理", "review": "仅自动审批"}


def finite_number(value, minimum=None):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and (minimum is None or value >= minimum))


def stamp(value):
    try:
        result = float(value) if isinstance(value, (float, int)) else datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        return result if finite_number(result) else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def usage(value):
    value = value if isinstance(value, dict) else {}
    result = {k: max(0, int(value.get(k, 0) or 0)) for k in FIELDS}
    # Cached and reasoning tokens are subsets, never additional tokens.
    result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    result["cached_input_tokens"] = min(result["input_tokens"], result["cached_input_tokens"])
    result["reasoning_output_tokens"] = min(result["output_tokens"], result["reasoning_output_tokens"])
    return result


def signature(value):
    return tuple(value[k] for k in FIELDS)


def parse_rollout(path):
    """Keep only metadata, usage and timing. Tolerate a live, incomplete tail."""
    meta = {}; entries = []; contexts = {}; completed = []; starts = {}; live_turns = {}
    model = "unknown"; turn = None; malformed = 0
    def activity(tid, ts, model_name=None):
        live = live_turns.get(tid)
        if live is not None and ts is not None:
            live["activities"].append(ts)
            if model_name:
                live["model_contexts"].append((ts, model_name))
    with Path(path).open("rb") as stream:
        for raw in stream:
            # Huge tool outputs and conversation text need not be deserialized.
            head = raw[:200]
            if not raw.endswith(b"\n"):
                continue
            if b'"response_item"' in head:
                match = re.search(rb'^\s*\{\s*"timestamp"\s*:\s*"([^"\\]+)"', head)
                if match:
                    activity(turn, stamp(match[1].decode("ascii", errors="ignore")))
                continue
            if not any(x in head for x in (b'"session_meta"', b'"turn_context"', b'"token_usage_record"', b'"event_msg"', b'"item_completed"')):
                continue
            try:
                e = json.loads(raw)
                p = e.get("payload") or {}
                if not isinstance(p, dict):
                    continue
                ts = stamp(e.get("timestamp"))
                kind = e.get("type")
                if kind == "session_meta" and not meta:
                    meta = {k: p.get(k) for k in ("id", "timestamp", "source", "parent_thread_id", "forked_from_id")}
                elif kind == "turn_context":
                    turn = p.get("turn_id") or turn
                    model = p.get("model") or model
                    contexts[turn] = model
                    activity(turn, ts, model)
                elif kind == "token_usage_record" and ts is not None:
                    p["usage"] = usage(p.get("usage"))
                    entries.append(("modern", ts, p, model, p.get("turn_id") or turn))
                    if not p.get("thread_id") or p["thread_id"] == meta.get("id"):
                        activity(p.get("turn_id") or turn, ts, p.get("model") or model)
                elif kind == "item_completed":
                    activity(p.get("turn_id") or e.get("turn_id") or turn, ts)
                elif kind == "event_msg":
                    typ = p.get("type")
                    if typ == "task_started":
                        turn = p.get("turn_id") or f"legacy-{ts}"
                        starts[turn] = ts or stamp(p.get("started_at"))
                        started = stamp(p.get("started_at"))
                        if started is None:
                            started = ts
                        live_turns[turn] = {"turn_id": turn, "started_at": started, "start_event_at": ts,
                                            "activities": [], "model_contexts": [], "terminal_at": []}
                        activity(turn, ts if ts is not None else started)
                    elif typ == "task_complete":
                        tid = p.get("turn_id") or turn
                        if tid in live_turns:
                            live_turns[tid]["terminal_at"].append(ts or stamp(p.get("completed_at")) or 0)
                        end = ts or stamp(p.get("completed_at"))
                        duration = p.get("duration_ms")
                        if duration is None and tid in starts and end is not None and starts[tid] is not None:
                            duration = (end - starts[tid]) * 1000
                        if end is not None and finite_number(duration) and duration > 0:
                            seconds = duration / 1000
                            interval_start = end - seconds
                            if finite_number(seconds) and seconds > 0 and finite_number(interval_start) and interval_start < end:
                                ttft = p.get("time_to_first_token_ms")
                                completed.append({"turn_id": tid, "timestamp": end, "seconds": seconds,
                                                  "interval_start": interval_start, "started_at": starts.get(tid),
                                                  "ttft_ms": ttft if finite_number(ttft, 0) else None})
                    elif typ == "turn_aborted":
                        tid = p.get("turn_id") or turn
                        if tid in live_turns:
                            live_turns[tid]["terminal_at"].append(ts or 0)
                    elif typ == "token_count" and ts is not None and isinstance(p.get("info"), dict):
                        for field in ("total_token_usage", "last_token_usage"):
                            if isinstance(p["info"].get(field), dict):
                                p["info"][field] = usage(p["info"][field])
                        entries.append(("legacy", ts, p["info"], model, turn))
                    activity(p.get("turn_id") or turn, ts)
            except (ValueError, TypeError, KeyError, OverflowError):
                malformed += 1
    sid = meta.get("id") or Path(path).stem
    # Modern records are the per-response ledger. Legacy counters in the SAME
    # file may carry a different inherited baseline, so never combine them.
    has_modern = any(entry[0] == "modern" for entry in entries)
    previous = usage({}); events = []; resets = 0
    created = stamp(meta.get("timestamp"))
    for kind, ts, p, m, t in entries:
        if kind == "modern":
            owner = p.get("thread_id") or sid
            u = usage(p.get("usage"))
            rid = p.get("response_id")
            key = ("response", rid) if rid else ("modern", owner, t, ts, signature(u))
        else:
            if has_modern:
                continue
            if not isinstance(p.get("total_token_usage"), dict):
                continue
            total = usage(p["total_token_usage"])
            if signature(total) == signature(previous):
                continue
            if total["total_tokens"] < previous["total_tokens"]:
                resets += 1
                u = usage(p.get("last_token_usage"))
            else:
                u = usage({k: max(0, total[k] - previous[k]) for k in FIELDS})
            previous = total
            # A fork can embed historical counters: use them as a baseline only.
            if meta.get("forked_from_id") and created is not None and ts < created:
                continue
            owner = sid
            key = ("legacy", ts, signature(total), signature(u))
        if not u["total_tokens"]:
            continue
        events.append({"key": key, "thread_id": owner, "root_id": p.get("session_id") if kind == "modern" else None, "turn_id": t, "timestamp": ts,
                       "model": p.get("model") or m, "provider": "codex", "reasoning_known": True, **u})
    meta.update(provider="codex", native_session_id=sid)
    source = meta.get("source")
    return {"id": sid, "meta": meta, "events": events, "completed": completed, "live_turns": list(live_turns.values()),
            "is_subagent": isinstance(source, dict) and "subagent" in source,
            "malformed": malformed, "resets": resets}


def blank():
    return {**usage({}), "request_count": 0, "active_seconds": 0.0,
            "speed_output_tokens": 0, "speed_samples": 0, "reasoning_known_requests": 0, "reasoning_unknown_requests": 0,
            "_intervals": [], "_ttfts": [], "_observed": set(), "_excluded": set(), "_mixed": set(), "_outside": set()}


def add(target, event):
    for k in FIELDS:
        target[k] += event[k]
    target["request_count"] += 1
    target["reasoning_known_requests" if event.get("reasoning_known", True) else "reasoning_unknown_requests"] += 1


def union_seconds(intervals):
    """Wall time covered by at least one completed turn; gaps do not count."""
    total = 0.0; current_start = None; current_end = None
    for start, end in sorted(intervals):
        if current_start is None:
            current_start, current_end = start, end
        elif start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    if current_start is not None:
        total += current_end - current_start
    return total


def add_speed(target, completion, output):
    target["active_seconds"] += completion["seconds"]
    target["speed_output_tokens"] += output
    target["speed_samples"] += 1
    target["_intervals"].append((completion["interval_start"], completion["timestamp"]))
    if completion["ttft_ms"] is not None:
        target["_ttfts"].append(completion["ttft_ms"])


def merge(target, source):
    for field in (*FIELDS, "request_count", "active_seconds", "speed_output_tokens", "speed_samples", "reasoning_known_requests", "reasoning_unknown_requests"):
        target[field] += source[field]
    for field in ("_intervals", "_ttfts"):
        target[field].extend(source[field])
    for field in ("_observed", "_excluded", "_mixed", "_outside"):
        target[field].update(source[field])


def finish(target, turn_limit=100):
    target["reasoning_coverage"] = ("partial" if target["reasoning_unknown_requests"] else "full") if target["reasoning_known_requests"] else "none"
    target["uncached_input_tokens"] = target["input_tokens"] - target["cached_input_tokens"]
    target["cache_hit_rate"] = round(target["cached_input_tokens"] / target["input_tokens"] * 100, 2) if target["input_tokens"] else 0
    wall = union_seconds(target.pop("_intervals"))
    target["average_turn_tps"] = round(target["speed_output_tokens"] / target["active_seconds"], 2) if target["active_seconds"] else None
    target["output_tps"] = target["average_turn_tps"]  # v1 compatibility
    target["combined_tps"] = round(target["speed_output_tokens"] / wall, 2) if wall else None
    target["parallelism"] = round(target["active_seconds"] / wall, 3) if wall else None
    target["active_wall_seconds"] = round(wall, 3)
    target["active_seconds"] = round(target["active_seconds"], 3)
    ttfts = target.pop("_ttfts")
    target["ttft_median_ms"] = round(statistics.median(ttfts), 2) if ttfts else None
    target["ttft_samples"] = len(ttfts)
    target["observed_turn_count"] = len(target.pop("_observed"))
    target["excluded_turn_count"] = len(target.pop("_excluded"))
    target["mixed_model_excluded_turn_count"] = len(target.pop("_mixed"))
    target["outside_range_turn_count"] = len(target.pop("_outside"))
    if "_turns" in target:
        turns = sorted(target.pop("_turns"), key=lambda t: (t["completed_at"], t["thread_id"], str(t["id"])), reverse=True)
        target["turns_total"] = len(turns)
        target["turns"] = turns if turn_limit is None else turns[:turn_limit]
    return target


class UsageIndex:
    def __init__(self, home, tz="Asia/Shanghai", *, paseo_home=None, claude_home=None, opencode_home=None, opencode_db=None, zcode_home=None, zcode_db=None):
        self.home = Path(home).expanduser().resolve()
        self.tz = ZoneInfo(tz)
        self.paseo_home = Path(paseo_home or os.environ.get("PASEO_HOME") or Path.home() / ".paseo").expanduser().resolve()
        self.claude_home = Path(claude_home or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser().resolve()
        self.opencode_home = Path(opencode_home or Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "opencode").expanduser().resolve()
        self.opencode_db = Path(opencode_db).expanduser().resolve() if opencode_db else self.opencode_home / "opencode.db"
        self.zcode_home = Path(zcode_home or Path.home() / ".zcode").expanduser().resolve()
        self.zcode_db = Path(zcode_db).expanduser().resolve() if zcode_db else self.zcode_home / "cli" / "db" / "db.sqlite"
        self.zcode_log_dir = (self.zcode_db.parent.parent / "log" if self.zcode_db.parent.name == "db" and self.zcode_db.parent.parent.name == "cli" else None) if zcode_db else self.zcode_home / "cli" / "log"
        self.paseo_associations = {}
        self.cache = {}
        self.zcode_live_cache = {}
        self.lock = threading.Lock()
        self.last_scan = 0
        self.scan_warnings = []

    def scan(self):
        if time.monotonic() - self.last_scan < 1:
            return
        files = []; warnings = []
        try:
            for folder in ("sessions", "archived_sessions"):
                base = self.home / folder
                if base.exists(): files.extend((path, "codex") for path in base.rglob("*.jsonl"))
            base = self.claude_home / "projects"
            if base.exists(): files.extend((path, "claude") for path in base.rglob("*.jsonl"))
        except OSError:
            warnings.append("部分代理日志目录无法读取。")
        present = set()
        for path, provider in files:
            present.add(path)
            try:
                stat = path.stat(); version = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
                if path not in self.cache or self.cache[path][0] != version:
                    self.cache[path] = (version, parse_rollout(path) if provider == "codex" else parse_claude(path))
            except (OSError, ValueError, TypeError) as exc:
                self.cache.pop(path, None)
                warnings.append(f"有代理日志无法读取（{type(exc).__name__}），统计可能不完整。")
        self.cache = {p: v for p, v in self.cache.items() if p in present}
        # Reopen read-only every scan so WAL updates and replacements remain visible.
        if self.opencode_db.exists():
            records, notices = parse_opencode(self.opencode_db); warnings.extend(notices)
            for i, rec in enumerate(records): self.cache[("opencode", i)] = (None, rec)
        if self.zcode_db.exists():
            records, notices = parse_zcode(self.zcode_db); warnings.extend(notices)
            for i, rec in enumerate(records): self.cache[("zcode", i)] = (None, rec)
        self.paseo_associations, notices = paseo_links(self.paseo_home); warnings.extend(notices)
        for _, rec in self.cache.values():
            m = rec["meta"]; linked = self.paseo_associations.get((m.get("provider", "codex"), m.get("native_session_id", rec["id"])), {})
            if linked.get("title"): m["paseo_title"] = linked["title"]
            else: m.pop("paseo_title", None)
        self.scan_warnings = list(dict.fromkeys(warnings))
        self.last_scan = time.monotonic()

    def record_context(self, records):
        meta = {r["id"]: r for r in records}
        parents = {}; roots = {}
        for owning_only in (True, False):
            for r in records:
                for event in r["events"]:
                    owner = event["thread_id"]; candidate = event.get("root_id")
                    if candidate and candidate != owner and (not owning_only or owner == r["id"]): roots.setdefault(owner, candidate)
        for sid, r in meta.items():
            m = r["meta"]; source = m.get("source") or {}; sub = source.get("subagent") if isinstance(source, dict) else None
            spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
            parent = m.get("parent_thread_id") or (spawn.get("parent_thread_id") if isinstance(spawn, dict) else None)
            if not parent:
                parent = roots.get(sid)
            if parent and parent != sid: parents[sid] = parent
        for sid, parent in roots.items(): parents.setdefault(sid, parent)
        def root_for(sid):
            current = sid; seen = set()
            while current not in seen:
                seen.add(current)
                if current not in parents: return current
                current = parents[current]
            return sid
        def provider_for(sid): return meta.get(sid, {}).get("meta", {}).get("provider", "codex")
        def origin_for(sid):
            current = sid; seen = set()
            while current not in seen:
                seen.add(current); m = meta.get(current, {}).get("meta", {})
                provider = m.get("provider", provider_for(sid)); native = m.get("native_session_id", current.removeprefix(provider + ":") if provider != "codex" else current)
                if (provider, native) in self.paseo_associations: return "paseo"
                if current not in parents: break
                current = parents[current]
            return "other"
        return meta, root_for, provider_for, origin_for

    @staticmethod
    def validate_sources(provider, origin):
        if provider not in {"all", *PROVIDERS}: raise ValueError("代理工具筛选无效")
        if origin not in {"all", "paseo", "other"}: raise ValueError("Paseo 关联筛选无效")

    def metadata(self):
        result = {}
        # Metadata only: never select first_user_message, preview, auth, or config.
        databases = sorted(self.home.glob("state_*.sqlite"), key=lambda p: int(p.stem.split('_')[-1]) if p.stem.split('_')[-1].isdigit() else 0, reverse=True)
        for path in databases[:1]:
            conn = None
            try:
                conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
                columns = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
                selected = [c for c in ("id", "title", "model", "agent_path", "agent_nickname", "archived") if c in columns]
                for row in conn.execute("SELECT " + ",".join(selected) + " FROM threads"):
                    d = dict(zip(selected, row)); result[d["id"]] = d
            except sqlite3.Error:
                pass
            finally:
                if conn:
                    conn.close()
        # The app's short sidebar titles supersede SQLite's original prompt title.
        latest_titles = {}
        index_path = self.home / "session_index.jsonl"
        if index_path.exists():
            try:
                with index_path.open("r", encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            record = json.loads(line)
                            sid = record.get("id"); title = record.get("thread_name")
                            updated = stamp(record.get("updated_at"))
                            if isinstance(sid, str) and isinstance(title, str) and title.strip() and updated is not None:
                                if sid not in latest_titles or updated >= latest_titles[sid][0]:
                                    latest_titles[sid] = (updated, title.strip())
                        except (ValueError, TypeError, AttributeError):
                            continue
                for sid, (_, title) in latest_titles.items():
                    result.setdefault(sid, {"id": sid})["title"] = title
            except OSError:
                pass
        return result

    def snapshot(self, days="7", now=None, scope="task", model="all", start=None, end=None, turn_limit=100, provider="all", origin="all"):
        self.validate_sources(provider, origin)
        if days not in ("1", "3", "7", "30", "90", "all", "custom"):
            raise ValueError("日期范围必须为 1、3、7、30、90、all 或 custom")
        self.validate_sources(provider, origin)
        if scope not in SCOPES:
            raise ValueError("统计范围必须为 task、all、main、agent 或 review")
        if not isinstance(model, str) or not model or len(model) > 200:
            raise ValueError("模型筛选无效")
        now = now or datetime.now(timezone.utc)
        today = now.astimezone(self.tz).date()
        if days == "custom":
            try:
                if not all(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in (start, end)):
                    raise ValueError
                start_date = date.fromisoformat(start); end_date = date.fromisoformat(end)
            except ValueError:
                raise ValueError("自定义日期需填写 YYYY-MM-DD 格式的开始和结束日期") from None
            if start_date > end_date:
                raise ValueError("开始日期不能晚于结束日期")
            if end_date > today:
                raise ValueError("自定义日期不能晚于今天")
            if (end_date - start_date).days >= 366:
                raise ValueError("自定义日期最多支持 366 天")
        else:
            start_date = end_date = None
        with self.lock:
            self.scan()
            return self.aggregate(days, now, scope, model, start_date, end_date, turn_limit, provider, origin)

    def terminal_turns(self):
        """Read only status keys; old inProgress rows are not proof of activity."""
        result = set()
        paths = sorted(self.home.glob("thread_history_*.sqlite"),
                       key=lambda p: int(p.stem.split("_")[-1]) if p.stem.split("_")[-1].isdigit() else 0,
                       reverse=True)
        for path in paths[:1]:
            conn = None
            try:
                conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
                columns = {row[1] for row in conn.execute("PRAGMA table_info(thread_turns)")}
                if {"thread_id", "turn_id", "status"} <= columns:
                    result.update(conn.execute("SELECT thread_id,turn_id FROM thread_turns "
                                               "WHERE status IN ('completed','failed','interrupted')"))
            except sqlite3.Error:
                pass
            finally:
                if conn:
                    conn.close()
        return result

    def live_snapshot(self, scope="task", model="all", now=None, provider="all", origin="all"):
        """Recent unclosed turns, using completed-response usage as live samples.

        A fresh log is evidence of recent activity, not a guaranteed process
        heartbeat. Missing endings age into uncertain, then hidden stale state.
        """
        self.validate_sources(provider, origin)
        if scope not in SCOPES:
            raise ValueError("统计范围必须为 task、all、main、agent 或 review")
        if not isinstance(model, str) or not model or len(model) > 200:
            raise ValueError("模型筛选无效")
        with self.lock:
            self.scan()
            now = now or datetime.now(timezone.utc)
            now_ts = now.timestamp()
            records = [value[1] for value in self.cache.values()]
            zcode_records = {record["id"]: record for record in records if record["meta"].get("provider") == "zcode"}
            for log_record in parse_zcode_live(self.zcode_log_dir, now, self.zcode_live_cache) if self.zcode_log_dir else []:
                original = zcode_records.get(log_record["id"])
                if original is None:
                    records.append(log_record)
                else:
                    merged = {**original, "live_turns": log_record["live_turns"]}
                    for turn in merged["live_turns"]:
                        for tid, ended in original.get("_terminal_turns", []):
                            if turn["turn_id"] == tid:
                                turn["terminal_at"].append(ended if ended is not None else 0)
                    records[records.index(original)] = merged
            metadata = self.metadata()
            terminal = self.terminal_turns()
            warnings = list(self.scan_warnings)

            # Only a thread's own ledger proves live output; copied parent
            # histories in forks must never make the new thread look active.
            meta = {}; events = defaultdict(list); seen = set(); latest = {}; parsed_terminal = set()
            for record in records:
                sid = record["id"]
                meta[sid] = record
                created = stamp(record["meta"].get("timestamp"))
                for event in record["events"]:
                    if event["thread_id"] != sid or event["timestamp"] > now_ts or event["key"] in seen:
                        continue
                    if created is not None and event["timestamp"] < created:
                        continue
                    seen.add(event["key"])
                    events[sid].append(event)
                for turn in record.get("live_turns", []):
                    if any(ended <= now_ts for ended in turn["terminal_at"]):
                        parsed_terminal.add((sid, turn["turn_id"]))
                    started = turn["started_at"]
                    start_event = turn.get("start_event_at")
                    identity_time = start_event if start_event is not None else started
                    if started is None or started > now_ts or (identity_time is not None and identity_time > now_ts):
                        continue
                    if created is not None and identity_time is not None and identity_time < created:
                        continue
                    previous = latest.get(sid)
                    if previous is None or started >= previous["started_at"]:
                        latest[sid] = turn
            _, root_for, provider_for, origin_for = self.record_context(records)

            def short_title(sid, category=None):
                info = metadata.get(sid, {})
                record_meta = meta.get(sid, {}).get("meta", {})
                title = ((info.get("agent_path") or info.get("agent_nickname")) if category == "agent" else None)
                title = title or info.get("title") or record_meta.get("title") or record_meta.get("paseo_title") or f"对话 {record_meta.get('native_session_id', sid)[:8]}"
                title = " ".join(str(title).split())
                return title if len(title) <= 120 else title[:119] + "…"

            rows = []; stale_count = 0
            for sid, turn in latest.items():
                if (provider != "all" and provider_for(sid) != provider) or (origin != "all" and origin_for(sid) != origin): continue
                tid = turn["turn_id"]
                if (sid, tid) in terminal or (sid, tid) in parsed_terminal:
                    continue
                started = turn["started_at"]
                ledger = [event for event in events[sid] if event["turn_id"] == tid and event["timestamp"] >= started]
                contexts = [(ts, name) for ts, name in turn["model_contexts"] if ts <= now_ts]
                # Later contexts win when their timestamps tie with task_started.
                current_model = max(enumerate(contexts), key=lambda item: (item[1][0], item[0]))[1][1] if contexts else metadata.get(sid, {}).get("model") or "unknown"
                models = {event["model"] for event in ledger}
                if provider_for(sid) == "zcode" and contexts and current_model != "unknown":
                    models.add(current_model)
                if not models:
                    models.add(current_model)
                record = meta.get(sid, {})
                source = record.get("meta", {}).get("source")
                subsource = source.get("subagent") if isinstance(source, dict) else None
                rid = root_for(sid)
                if "codex-auto-review" in models or (isinstance(subsource, dict) and subsource.get("other") == "guardian"):
                    category = "review"
                else:
                    category = "agent" if rid != sid or record.get("is_subagent", False) else "main"
                if not (scope == "all" or (scope == "task" and category != "review") or scope == category):
                    continue
                if model != "all" and model not in models:
                    continue
                activities = [ts for ts in turn["activities"] if started <= ts <= now_ts]
                last_activity = max([started, *activities, *(event["timestamp"] for event in ledger)])
                age = max(0.0, now_ts - last_activity)
                if age > 86400:
                    stale_count += 1
                    continue
                status = "active" if age <= 300 else "uncertain"
                matched = ledger if model == "all" else [event for event in ledger if event["model"] == model]
                elapsed = max(0.0, now_ts - started)
                window = min(60.0, elapsed)
                output = sum(event["output_tokens"] for event in matched)
                rolling_output = sum(event["output_tokens"] for event in matched if event["timestamp"] > now_ts - window)
                mixed = len(models) > 1
                can_measure = bool(matched) and status == "active"
                average = output / elapsed if can_measure and elapsed > 0 and not (mixed and model != "all") else None
                rolling = rolling_output / window if can_measure and window > 0 else None
                last_usage = max((event["timestamp"] for event in matched), default=None)
                iso = lambda ts: datetime.fromtimestamp(ts, self.tz).isoformat()
                rows.append({"thread_id": sid, "turn_id": tid, "root_id": rid, "provider": provider_for(sid), "origin": origin_for(sid),
                    "title": short_title(sid, category), "root_title": short_title(rid), "category": category,
                    "model": ", ".join(sorted(models)) if model == "all" else model, "models": sorted(models),
                    "status": status, "started_at": iso(started), "last_activity_at": iso(last_activity),
                    "last_usage_at": iso(last_usage) if last_usage is not None else None,
                    "elapsed_seconds": round(elapsed, 3), "activity_age_seconds": round(age, 3),
                    "output_tokens": output, "reasoning_output_tokens": sum(e["reasoning_output_tokens"] for e in matched), "request_count": len(matched),
                    "reasoning_known_requests": sum(e.get("reasoning_known", True) for e in matched), "reasoning_unknown_requests": sum(not e.get("reasoning_known", True) for e in matched),
                    "reasoning_coverage": "full" if matched and all(e.get("reasoning_known", True) for e in matched) else "partial" if any(e.get("reasoning_known", True) for e in matched) else "none",
                    "average_tps": round(average, 2) if average is not None else None,
                    "rolling_tps": round(rolling, 2) if rolling is not None else None,
                    "rolling_output_tokens": rolling_output, "window_seconds": round(window, 3), "mixed_model": mixed})
            rows.sort(key=lambda row: (row["status"] != "active", -stamp(row["last_activity_at"]), row["thread_id"]))
            if any(record["malformed"] for record in records):
                warnings.append("部分日志行无法解析，实时统计可能不完整。")
            return {"generated_at": now.astimezone(self.tz).isoformat(), "timezone": str(self.tz),
                "poll_seconds": 10, "window_seconds": 60, "fresh_seconds": 300,
                "filters": {"scope": scope, "model": model, "provider": provider, "origin": origin},
                "active_count": sum(row["status"] == "active" for row in rows),
                "uncertain_count": sum(row["status"] == "uncertain" for row in rows),
                "stale_count": stale_count, "threads": rows, "warnings": warnings}

    def aggregate(self, days, now=None, scope="task", model="all", start=None, end=None, turn_limit=100, provider="all", origin="all"):
        now = now or datetime.now(timezone.utc)
        today = now.astimezone(self.tz).date()
        records = [v[1] for v in self.cache.values()]
        metadata = self.metadata()
        meta = {}; seen = set(); events = []; turns = {}; completions = {}
        # Prefer the owning file over its copied history, then recover orphaned copies.
        for record in records:
            meta[record["id"]] = record
            for event in record["events"]:
                if event["thread_id"] == record["id"] and event["key"] not in seen:
                    seen.add(event["key"]); events.append(event)
            for completion in record["completed"]:
                completions[(record["id"], completion["turn_id"])] = completion
        for record in records:
            for event in record["events"]:
                if event["key"] not in seen:
                    seen.add(event["key"]); events.append(event)
        events = [event for event in events if event["timestamp"] <= now.timestamp()]
        _, root_for, provider_for, origin_for = self.record_context(records)

        thread_models = defaultdict(set)
        for event in events:
            thread_models[event["thread_id"]].add(event["model"])
        def category_for(sid):
            source = meta.get(sid, {}).get("meta", {}).get("source")
            subsource = source.get("subagent") if isinstance(source, dict) else None
            if "codex-auto-review" in thread_models[sid] or (isinstance(subsource, dict) and subsource.get("other") == "guardian"):
                return "review"
            return "agent" if root_for(sid) != sid or meta.get(sid, {}).get("is_subagent", False) else "main"
        categories = {event["thread_id"]: category_for(event["thread_id"]) for event in events}
        def scope_matches(sid):
            category = categories.get(sid, "main")
            return scope == "all" or (scope == "task" and category != "review") or scope == category
        scoped_events = [event for event in events if scope_matches(event["thread_id"])
            and (provider == "all" or provider_for(event["thread_id"]) == provider)
            and (origin == "all" or origin_for(event["thread_id"]) == origin)]
        available_models = sorted({event["model"] for event in scoped_events})
        matched_events = [event for event in scoped_events if model == "all" or event["model"] == model]
        coverage_dates = [datetime.fromtimestamp(event["timestamp"], self.tz).date() for event in matched_events]
        earliest = min(coverage_dates, default=today)
        start_date = start if days == "custom" else earliest if days == "all" else today - timedelta(days=int(days) - 1)
        end_date = end if days == "custom" else today
        def in_range(timestamp):
            return start_date <= datetime.fromtimestamp(timestamp, self.tz).date() <= end_date
        summary = blank(); sessions = {}; daily = {}; models = {}
        speed_all = {}; speed_models = defaultdict(dict)
        hourly = [{"hour": hour, **usage({}), "request_count": 0} for hour in range(24)]
        local_now = now.astimezone(self.tz)
        monday = today - timedelta(days=today.weekday())
        def local_boundary(value):
            # Round-trip normalizes local times skipped by a daylight-saving transition.
            candidate = value.replace(tzinfo=self.tz, fold=0)
            return datetime.fromtimestamp(candidate.timestamp(), self.tz)
        current_start = local_boundary(datetime.combine(monday, datetime.min.time()))
        previous_start = local_boundary(datetime.combine(monday - timedelta(days=7), datetime.min.time()))
        previous_end = local_boundary(local_now.replace(tzinfo=None) - timedelta(days=7))
        windows = [(current_start, local_now, blank()), (previous_start, previous_end, blank())]
        def week_target(timestamp):
            return next((target for lower, upper, target in windows
                         if lower.timestamp() <= timestamp <= upper.timestamp()), None)
        def ensure_session(sid, timestamp, model_name):
            info = metadata.get(sid, {}); record = meta.get(sid, {}); session_meta = record.get("meta", {})
            session = sessions.setdefault(sid, {"id": sid, "provider": provider_for(sid), "origin": origin_for(sid), "title": info.get("title") or session_meta.get("title") or session_meta.get("paseo_title") or f"对话 {session_meta.get('native_session_id', sid)[:8]}",
                "model": info.get("model") or model_name, "models": set(),
                "first_seen": timestamp, "last_seen": timestamp,
                "is_subagent": record.get("is_subagent", False), "parent_id": session_meta.get("parent_thread_id"),
                "archived": bool(info.get("archived")), "category": categories.get(sid, "main") + "_tokens",
                "_turns": [], **blank()})
            session["first_seen"] = min(session["first_seen"], timestamp)
            session["last_seen"] = max(session["last_seen"], timestamp)
            return session
        def ensure_model(model_name):
            return models.setdefault(model_name, {"model": model_name, **blank()})
        for i in range(max(0, (end_date - start_date).days + 1)):
            day = str(start_date + timedelta(days=i))
            daily[day] = {"date": day, **blank(), "_sessions": set()}
        # Complete turn output is assembled before applying dates or model filters.
        for event in scoped_events:
            key = (event["thread_id"], event["turn_id"])
            turn = turns.setdefault(key, {"output": 0, "models": set(), "known": 0, "unknown": 0})
            turn["output"] += event["output_tokens"]
            turn["known" if event.get("reasoning_known", True) else "unknown"] += 1
            turn["models"].add(event["model"])
        today_tokens = sum(event["total_tokens"] for event in matched_events
                           if datetime.fromtimestamp(event["timestamp"], self.tz).date() == today)
        for event in matched_events:
            week = week_target(event["timestamp"])
            if week is not None:
                add(week, event)
            if not in_range(event["timestamp"]):
                continue
            hour = hourly[datetime.fromtimestamp(event["timestamp"], self.tz).hour]
            for field in FIELDS:
                hour[field] += event[field]
            hour["request_count"] += 1
            sid = event["thread_id"]; key = (sid, event["turn_id"])
            session = ensure_session(sid, event["timestamp"], event["model"])
            session["models"].add(event["model"])
            day = daily[str(datetime.fromtimestamp(event["timestamp"], self.tz).date())]
            day["_sessions"].add(root_for(sid))
            for target in (summary, session, day, ensure_model(event["model"])):
                add(target, event)
                target["_observed"].add(key)
                completion = completions.get(key)
                if completion is None or completion["timestamp"] > now.timestamp():
                    target["_excluded"].add(key)
                elif not in_range(completion["timestamp"]):
                    target["_outside"].add(key)
                elif target is day and str(datetime.fromtimestamp(completion["timestamp"], self.tz).date()) != day["date"]:
                    target["_outside"].add(key)
        for key, completion in completions.items():
            sid, tid = key
            if not scope_matches(sid) or completion["timestamp"] > now.timestamp():
                continue
            turn = turns.get(key)
            if not turn:
                continue
            turn_models = turn["models"]
            if in_range(completion["timestamp"]):
                speed_day = str(datetime.fromtimestamp(completion["timestamp"], self.tz).date())
                add_speed(speed_all.setdefault(speed_day, blank()), completion, turn["output"])
                if len(turn_models) == 1:
                    model_days = speed_models[next(iter(turn_models))]
                    add_speed(model_days.setdefault(speed_day, blank()), completion, turn["output"])
            if model != "all" and model not in turn_models:
                continue
            week = week_target(completion["timestamp"])
            if week is not None and (model == "all" or len(turn_models) == 1):
                add_speed(week, completion, turn["output"])
            if not in_range(completion["timestamp"]):
                continue
            visible_models = turn_models if model == "all" else {model}
            session = ensure_session(sid, completion["timestamp"], next(iter(visible_models)))
            session["models"].update(visible_models)
            day = daily[str(datetime.fromtimestamp(completion["timestamp"], self.tz).date())]
            day["_sessions"].add(root_for(sid))
            targets = [summary, session, day]
            for target in targets:
                target["_observed"].add(key)
            # A model's partial output cannot be divided by a whole mixed-model turn.
            if len(turn_models) != 1:
                for model_name in visible_models:
                    model_target = ensure_model(model_name)
                    model_target["_observed"].add(key)
                    model_target["_mixed"].add(key)
                if model != "all":
                    for target in targets:
                        target["_mixed"].add(key)
                    continue
            else:
                model_target = ensure_model(next(iter(turn_models)))
                model_target["_observed"].add(key)
                targets.append(model_target)
            for target in targets:
                add_speed(target, completion, turn["output"])
            session["_turns"].append({"id": tid, "thread_id": sid, "provider": provider_for(sid), "origin": origin_for(sid), "reasoning_coverage": ("partial" if turn["unknown"] else "full") if turn["known"] else "none", "reasoning_known_requests": turn["known"], "reasoning_unknown_requests": turn["unknown"], "category": categories.get(sid, "main"),
                "model": ", ".join(sorted(turn_models)),
                "completed_at": datetime.fromtimestamp(completion["timestamp"], self.tz).isoformat(),
                "output_tokens": turn["output"], "duration_seconds": round(completion["seconds"], 3),
                "output_tps": round(turn["output"] / completion["seconds"], 2), "ttft_ms": completion["ttft_ms"]})
        grouped = {}
        breakdown = {"main_tokens": 0, "agent_tokens": 0, "review_tokens": 0}
        for sid, thread in sessions.items():
            rid = root_for(sid); info = metadata.get(rid, {})
            group = grouped.setdefault(rid, {"id": rid, "provider": provider_for(sid), "origin": origin_for(sid),
                "title": info.get("title") or (sessions.get(rid) or {}).get("title") or meta.get(rid, {}).get("meta", {}).get("title") or meta.get(rid, {}).get("meta", {}).get("paseo_title") or f"对话 {rid.removeprefix(provider_for(sid) + chr(58))[:8]}",
                "models": set(), "first_seen": thread["first_seen"], "last_seen": thread["last_seen"],
                "is_subagent": False, "parent_id": None, "child_count": 0, "origins": set(),
                "main_tokens": 0, "agent_tokens": 0, "review_tokens": 0, "_turns": [], **blank()})
            group["origins"].add(thread["origin"])
            group["models"].update(thread["models"])
            group["first_seen"] = min(group["first_seen"], thread["first_seen"])
            group["last_seen"] = max(group["last_seen"], thread["last_seen"])
            group["child_count"] += int(sid != rid)
            group["_turns"].extend(thread["_turns"])
            merge(group, thread)
            category = thread["category"]
            group[category] += thread["total_tokens"]
            breakdown[category] += thread["total_tokens"]
        for group in grouped.values():
            group["origins"] = sorted(group["origins"])
            group["origin"] = group["origins"][0] if len(group["origins"]) == 1 else "mixed"
        for collection in (sessions, grouped):
            for session in collection.values():
                session["models"] = sorted(session["models"])
                session["model"] = ", ".join(m for m in session["models"] if m != "codex-auto-review") or ", ".join(session["models"])
                session["turn_count"] = len(session["_observed"])
                for field in ("first_seen", "last_seen"):
                    session[field] = datetime.fromtimestamp(session[field], self.tz).isoformat()
                finish(session, turn_limit)
        for day in daily.values():
            day["session_count"] = len(day.pop("_sessions")); finish(day)
        for model_row in models.values():
            finish(model_row)
        def speed_series(days):
            rows = []
            for date, target in sorted(days.items()):
                finish(target)
                rows.append({"date": date, **{field: target[field] for field in
                    ("average_turn_tps", "combined_tps", "speed_samples")}})
            return rows
        speed_trends = {"all": speed_series(speed_all),
                        "models": {name: speed_series(days) for name, days in speed_models.items()}}
        summary.update(session_count=len(grouped), thread_count=len(sessions), today_tokens=today_tokens, **breakdown)
        finish(summary)
        week_rows = []
        week_fields = ("total_tokens", "output_tokens", "request_count", "speed_output_tokens",
                       "active_seconds", "speed_samples", "average_turn_tps")
        for lower, upper, target in windows:
            finish(target)
            week_rows.append({"start_at": lower.isoformat(), "end_at": upper.isoformat(),
                              **{field: target[field] for field in week_fields}})
        current_week, previous_week = week_rows
        changes = {}
        for field in ("total_tokens", "output_tokens", "average_turn_tps"):
            current_value, previous_value = current_week[field], previous_week[field]
            changes[field] = (round((current_value - previous_value) / previous_value * 100, 2)
                              if current_value is not None and previous_value is not None and previous_value > 0 else None)
        week_over_week = {"basis": "calendar_week_to_date", "date_range_applies": False,
                          "current": current_week, "previous": previous_week, "change_pct": changes}
        warnings = list(self.scan_warnings)
        if not records:
            warnings.append("未找到本机 Codex 会话日志。可以用 --codex-home 指定日志目录。")
        if any(record["malformed"] for record in records):
            warnings.append("部分日志行无法解析，已跳过；正在写入的未完成尾行会在下次刷新读取。")
        if any(record["resets"] for record in records):
            warnings.append("检测到旧版累计计数重置，重置处按 last_token_usage 计入，可能缺少日志间隙中的用量。")
        if summary["mixed_model_excluded_turn_count"]:
            warnings.append(f"有 {summary['mixed_model_excluded_turn_count']} 个完成轮次包含多个模型；筛选模型后仅计入其用量，不计入速度。")
        return {"generated_at": now.astimezone(self.tz).isoformat(), "timezone": str(self.tz),
                "range": {"start": str(start_date), "end": str(end_date), "days": days},
                "filters": {"scope": scope, "scope_label": SCOPES[scope], "model": model, "provider": provider, "origin": origin,
                            "available_models": available_models, "days": days, "start": str(start_date), "end": str(end_date)},
                "source": {"path": str(self.home), "providers": {"codex": {"files": sum(r["meta"].get("provider", "codex") == "codex" for r in records)}, "claude": {"files": sum(r["meta"].get("provider") == "claude" for r in records)}, "opencode": {"files": sum(r["meta"].get("provider") == "opencode" for r in records)}, "zcode": {"files": sum(r["meta"].get("provider") == "zcode" for r in records)}}, "files": len(records), "sessions": len(meta),
                    "warnings": warnings, "speed_note": SPEED_NOTE,
                    "coverage": {"earliest_date": str(min(coverage_dates)) if coverage_dates else None,
                                 "latest_date": str(max(coverage_dates)) if coverage_dates else None,
                                 "matched_threads": len({event["thread_id"] for event in matched_events}), "scope": scope}},
                "summary": summary, "daily": list(daily.values()), "speed_trends": speed_trends, "hourly": hourly, "week_over_week": week_over_week,
                "sessions": sorted(grouped.values(), key=lambda s: s["total_tokens"], reverse=True),
                "threads": sorted(sessions.values(), key=lambda s: s["total_tokens"], reverse=True),
                "models": sorted(models.values(), key=lambda m: m["total_tokens"], reverse=True)}
