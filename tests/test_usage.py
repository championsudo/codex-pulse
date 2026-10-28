"""Synthetic rollout regressions; these tests never read the user's Codex home."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "usage.py"
SPEC = importlib.util.spec_from_file_location("codex_pulse_usage", MODULE_PATH)
usage_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = usage_module
SPEC.loader.exec_module(usage_module)
UsageIndex = usage_module.UsageIndex
parse_rollout = usage_module.parse_rollout

NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
DEFAULT_TIME = "2026-10-22T02:00:00Z"


def row(kind, payload, at=DEFAULT_TIME):
    return {"timestamp": at, "type": kind, "payload": payload}


def meta(sid="main", **fields):
    return row("session_meta", {"id": sid, "timestamp": DEFAULT_TIME, **fields})


def tokens(input_count, output_count, cached=0, reasoning=0):
    return {
        "input_tokens": input_count,
        "cached_input_tokens": cached,
        "output_tokens": output_count,
        "reasoning_output_tokens": reasoning,
        "total_tokens": input_count + output_count,
    }


def modern(rid, counts, *, sid="main", turn="turn-1", root=None, at=DEFAULT_TIME):
    payload = {
        "response_id": rid, "thread_id": sid, "turn_id": turn, "usage": counts,
    }
    if root is not None:
        payload["session_id"] = root
    return row("token_usage_record", payload, at)


def legacy(counts, *, at=DEFAULT_TIME, last=None):
    info = {"total_token_usage": counts}
    if last is not None:
        info["last_token_usage"] = last
    return row("event_msg", {"type": "token_count", "info": info}, at)


def complete(turn, milliseconds, at=DEFAULT_TIME):
    return row("event_msg", {
        "type": "task_complete", "turn_id": turn, "duration_ms": milliseconds,
    }, at)


class RolloutTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-pulse-tests-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.index = UsageIndex(self.home, tz="Asia/Shanghai", paseo_home=self.home / "paseo", claude_home=self.home / "claude", opencode_home=self.home / "opencode")

    def write(self, name, rows, folder="sessions"):
        path = self.home / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8")
        return path

    def snapshot(self, days="all", **filters):
        # Bypass only the polling throttle, retaining actual stat/cache checks.
        self.index.last_scan = 0
        return self.index.snapshot(days=days, now=NOW, **filters)


class ParserTests(RolloutTestCase):
    def test_modern_ledger_replaces_legacy_counters_with_different_baseline(self):
        path = self.write("mixed.jsonl", [
            meta(),
            legacy(tokens(10000, 1000)),
            modern("r-1", tokens(100, 20, cached=40, reasoning=10)),
            legacy(tokens(10200, 1040)),
            modern("r-2", tokens(50, 10, cached=10, reasoning=5)),
            legacy(tokens(10200, 1040)),
        ])
        parsed = parse_rollout(path)
        self.assertEqual(len(parsed["events"]), 2)
        summary = self.snapshot()["summary"]
        self.assertEqual(summary["input_tokens"], 150)
        self.assertEqual(summary["output_tokens"], 30)
        self.assertEqual(summary["total_tokens"], 180)
        self.assertEqual(summary["request_count"], 2)

    def test_first_metadata_identifies_child_despite_copied_parent_metadata(self):
        path = self.write("child.jsonl", [
            meta("child", parent_thread_id="main", source={"subagent": {"thread_spawn": {}}}),
            meta("main", source="cli"),
            modern("r-child", tokens(12, 3), sid="child"),
        ])
        parsed = parse_rollout(path)
        self.assertEqual(parsed["id"], "child")
        self.assertEqual(parsed["meta"]["parent_thread_id"], "main")
        self.assertTrue(parsed["is_subagent"])
        self.assertEqual(parsed["events"][0]["thread_id"], "child")

    def test_legacy_cumulative_differences_skip_repeated_counters(self):
        first = tokens(100, 10, cached=20, reasoning=5)
        last = tokens(150, 30, cached=35, reasoning=10)
        path = self.write("legacy.jsonl", [
            meta(), legacy(first), legacy(first), legacy(last), legacy(last),
        ])
        events = parse_rollout(path)["events"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]["input_tokens"], 50)
        self.assertEqual(events[1]["cached_input_tokens"], 15)
        self.assertEqual(events[1]["output_tokens"], 20)
        self.assertEqual(events[1]["reasoning_output_tokens"], 5)
        summary = self.snapshot()["summary"]
        self.assertEqual(summary["total_tokens"], 180)
        self.assertEqual(summary["request_count"], 2)

    def test_legacy_reset_uses_last_usage_and_exposes_warning(self):
        path = self.write("reset.jsonl", [
            meta(), legacy(tokens(100, 10)),
            legacy(tokens(20, 5), last=tokens(8, 3)),
        ])
        parsed = parse_rollout(path)
        self.assertEqual(parsed["resets"], 1)
        self.assertEqual(parsed["events"][-1]["total_tokens"], 11)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["summary"]["total_tokens"], 121)
        self.assertTrue(snapshot["source"]["warnings"])

    def test_fork_historical_counter_is_baseline_for_later_legacy_increment(self):
        path = self.write("fork.jsonl", [
            meta("fork", forked_from_id="main"),
            legacy(tokens(100, 10), at="2026-10-22T01:59:00Z"),
            legacy(tokens(130, 15), at="2026-10-22T02:01:00Z"),
        ])
        events = parse_rollout(path)["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["total_tokens"], 35)

    def test_cached_input_and_reasoning_are_subsets_of_total(self):
        counts = tokens(100, 20, cached=60, reasoning=15)
        counts["total_tokens"] = 999  # Recompute from inclusive input/output.
        self.write("subsets.jsonl", [meta(), modern("r-1", counts)])
        summary = self.snapshot()["summary"]
        self.assertEqual(summary["total_tokens"], 120)
        self.assertEqual(summary["uncached_input_tokens"], 40)
        self.assertEqual(summary["cache_hit_rate"], 60)
        self.assertEqual(summary["reasoning_output_tokens"], 15)
        self.assertEqual(usage_module.usage(tokens(10, 2, cached=99, reasoning=99)), tokens(10, 2, cached=10, reasoning=2))

    def test_malformed_line_is_skipped_and_unterminated_tail_is_reread(self):
        path = self.write("growing.jsonl", [meta(), modern("r-1", tokens(10, 2))])
        with path.open("ab") as stream:
            stream.write(b'{"type":"event_msg", invalid}\n')
            stream.write(json.dumps(modern("r-2", tokens(20, 4))).encode("utf-8"))
        parsed = parse_rollout(path)
        self.assertEqual(parsed["malformed"], 1)
        self.assertEqual(len(parsed["events"]), 1)
        before = self.snapshot()
        self.assertEqual(before["summary"]["total_tokens"], 12)
        self.assertTrue(before["source"]["warnings"])
        with path.open("ab") as stream:
            stream.write(b"\n")
        after = self.snapshot()
        self.assertEqual(after["summary"]["total_tokens"], 36)
        self.assertEqual(after["summary"]["request_count"], 2)
        self.assertEqual(self.snapshot()["summary"]["total_tokens"], 36)

    def test_invalid_numeric_usage_does_not_discard_valid_records_in_file(self):
        path = self.write("invalid-usage.jsonl", [
            meta(), modern("valid", tokens(100, 20)),
            modern("invalid", {"input_tokens": "unknown", "output_tokens": 3}),
            modern("valid-after", tokens(20, 5)),
        ])
        parsed = parse_rollout(path)
        self.assertEqual(len(parsed["events"]), 2)
        self.assertEqual(parsed["malformed"], 1)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["summary"]["total_tokens"], 145)
        self.assertEqual(snapshot["source"]["files"], 1)
        self.assertTrue(snapshot["source"]["warnings"])


class AggregateTests(RolloutTestCase):
    def test_response_id_deduplicates_copied_history_across_files(self):
        original = modern("shared-response", tokens(100, 50))
        self.write("parent.jsonl", [meta(), original])
        self.write("child.jsonl", [
            meta("child", parent_thread_id="main", source={"subagent": {"thread_spawn": {}}}),
            original,
            modern("child-response", tokens(20, 5), sid="child"),
        ])
        self.write("archived-copy.jsonl", [meta(), original], folder="archived_sessions")
        summary = self.snapshot()["summary"]
        self.assertEqual(summary["total_tokens"], 175)
        self.assertEqual(summary["request_count"], 2)
        self.assertEqual(summary["input_tokens"], 120)
        self.assertEqual(summary["output_tokens"], 55)

    def test_daily_totals_follow_shanghai_midnight_not_utc_midnight(self):
        self.write("midnight.jsonl", [
            meta(),
            modern("before", tokens(10, 2), at="2026-10-21T15:59:59Z"),
            modern("after", tokens(20, 4), at="2026-10-21T16:00:00Z"),
        ])
        snapshot = self.snapshot()
        daily = {day["date"]: day for day in snapshot["daily"]}
        self.assertEqual(daily["2026-10-21"]["total_tokens"], 12)
        self.assertEqual(daily["2026-10-22"]["total_tokens"], 24)
        self.assertEqual(snapshot["summary"]["today_tokens"], 24)

    def test_speed_is_duration_weighted_and_excludes_unfinished_turn(self):
        self.write("speed.jsonl", [
            meta(),
            row("turn_context", {"turn_id": "fast", "model": "test-model"}),
            modern("fast-response", tokens(50, 100), turn="fast"),
            complete("fast", 10000),
            row("turn_context", {"turn_id": "slow", "model": "test-model"}),
            modern("slow-response", tokens(50, 100), turn="slow"),
            complete("slow", 100000),
            modern("unfinished-response", tokens(50, 1000), turn="unfinished"),
        ])
        snapshot = self.snapshot()
        summary = snapshot["summary"]
        self.assertEqual(summary["output_tokens"], 1200)
        self.assertEqual(summary["speed_output_tokens"], 200)
        self.assertEqual(summary["active_seconds"], 110)
        self.assertEqual(summary["speed_samples"], 2)
        self.assertEqual(summary["output_tps"], 1.82)
        self.assertEqual(snapshot["sessions"][0]["output_tps"], 1.82)

    def test_cross_midnight_speed_uses_completion_day_token_usage_uses_event_day(self):
        self.write("overnight.jsonl", [
            meta(),
            modern("r-1", tokens(100, 40), at="2026-10-21T15:59:50Z"),
            complete("turn-1", 20000, at="2026-10-21T16:00:10Z"),
        ])
        daily = {day["date"]: day for day in self.snapshot()["daily"]}
        self.assertEqual(daily["2026-10-21"]["total_tokens"], 140)
        self.assertIsNone(daily["2026-10-21"]["output_tps"])
        self.assertEqual(daily["2026-10-22"]["total_tokens"], 0)
        self.assertEqual(daily["2026-10-22"]["output_tps"], 2)

    def test_speed_completion_inside_range_counts_tokens_from_before_range(self):
        self.write("range-boundary.jsonl", [
            meta(),
            modern("r-1", tokens(100, 20), at="2026-10-15T15:59:59Z"),
            complete("turn-1", 2000, at="2026-10-15T16:00:01Z"),
        ])
        snapshot = self.snapshot("7")
        self.assertEqual(snapshot["range"]["start"], "2026-10-16")
        self.assertEqual(snapshot["summary"]["total_tokens"], 0)
        self.assertEqual(snapshot["summary"]["speed_samples"], 1)
        self.assertEqual(snapshot["summary"]["speed_output_tokens"], 20)
        self.assertEqual(snapshot["summary"]["output_tps"], 10)
        self.assertEqual(snapshot["daily"][0]["output_tps"], 10)

    def test_root_session_id_groups_agents_and_separates_guardian_usage(self):
        self.write("main.jsonl", [
            meta(), row("turn_context", {"turn_id": "turn-1", "model": "test-model"}),
            modern("main-response", tokens(100, 20), root="main"),
        ])
        self.write("agent.jsonl", [
            meta("agent", source={"subagent": {"thread_spawn": {}}}),
            row("turn_context", {"turn_id": "turn-1", "model": "test-model"}),
            modern("agent-response", tokens(50, 10), sid="agent", root="main"),
        ])
        self.write("guardian.jsonl", [
            meta("guardian", source={"subagent": {"other": "guardian"}}),
            row("turn_context", {"turn_id": "turn-1", "model": "codex-auto-review"}),
            modern("review-response", tokens(30, 5), sid="guardian", root="main"),
        ])
        snapshot = self.snapshot(scope="all")
        summary = snapshot["summary"]
        self.assertEqual(summary["session_count"], 1)
        self.assertEqual(summary["thread_count"], 3)
        self.assertEqual(summary["total_tokens"], 215)
        self.assertEqual(summary["main_tokens"], 120)
        self.assertEqual(summary["agent_tokens"], 60)
        self.assertEqual(summary["review_tokens"], 35)
        self.assertEqual(len(snapshot["sessions"]), 1)
        session = snapshot["sessions"][0]
        self.assertEqual(session["id"], "main")
        self.assertEqual(session["child_count"], 2)
        self.assertEqual(session["total_tokens"], 215)
        self.assertEqual(session["model"], "test-model")
        self.assertEqual(snapshot["daily"][-1]["session_count"], 1)
        threads = {thread["id"]: thread for thread in snapshot["threads"]}
        self.assertEqual(threads["main"]["category"], "main_tokens")
        self.assertEqual(threads["agent"]["category"], "agent_tokens")
        self.assertEqual(threads["guardian"]["category"], "review_tokens")

    def test_nested_parent_metadata_groups_legacy_agents_under_main(self):
        self.write("main.jsonl", [meta(), legacy(tokens(100, 20))])
        self.write("child.jsonl", [
            meta("child", parent_thread_id="main", source={"subagent": {"thread_spawn": {}}}),
            legacy(tokens(30, 5)),
        ])
        self.write("grandchild.jsonl", [
            meta("grandchild", parent_thread_id="child", source={"subagent": {"thread_spawn": {}}}),
            legacy(tokens(20, 5)),
        ])
        snapshot = self.snapshot()
        self.assertEqual(snapshot["summary"]["session_count"], 1)
        self.assertEqual(snapshot["summary"]["main_tokens"], 120)
        self.assertEqual(snapshot["summary"]["agent_tokens"], 60)
        self.assertEqual(snapshot["sessions"][0]["id"], "main")
        self.assertEqual(snapshot["sessions"][0]["child_count"], 2)

    def test_empty_home_returns_zero_totals_without_fake_speed(self):
        snapshot = self.snapshot()
        self.assertEqual(snapshot["summary"]["total_tokens"], 0)
        self.assertIsNone(snapshot["summary"]["output_tps"])
        self.assertEqual(snapshot["sessions"], [])
        self.assertTrue(snapshot["source"]["warnings"])


if __name__ == "__main__":
    unittest.main()
