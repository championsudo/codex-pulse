"""Synthetic regression tests for explicit speed, scope and date semantics."""
from __future__ import annotations

import csv
import importlib.util
import io
import json
import sqlite3
import sys
import threading
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from unittest.mock import patch

from test_usage import (MODULE_PATH, NOW, RolloutTestCase, complete, meta,
                        modern, parse_rollout, row, tokens, usage_module)


def context(turn="turn-1", model="model-a", at="2026-10-22T02:00:00Z"):
    return row("turn_context", {"turn_id": turn, "model": model}, at)


class TurnFixture(RolloutTestCase):
    def write_turn(self, sid, *, end="2026-10-22T02:00:10Z", milliseconds=10000,
                   output=300, model="model-a", root="main", source=None,
                   ttft=None, turn="turn-1"):
        fields = {"source": source} if source is not None else {}
        finished = complete(turn, milliseconds, at=end)
        if ttft is not None:
            finished["payload"]["time_to_first_token_ms"] = ttft
        return self.write(sid + ".jsonl", [
            meta(sid, **fields), context(turn, model),
            modern(sid + "-response", tokens(100, output), sid=sid, turn=turn,
                   root=root, at=end), finished,
        ])

    def parallel_pair(self, second_end="2026-10-22T02:00:10Z"):
        self.write_turn("main")
        self.write_turn("agent", end=second_end,
                        source={"subagent": {"thread_spawn": {}}})


class MetricsV2Tests(TurnFixture):
    def test_two_concurrent_30_tps_turns_average_30_and_combine_60(self):
        self.parallel_pair()
        snapshot = self.snapshot()
        for bucket in (snapshot["summary"], snapshot["sessions"][0],
                       snapshot["daily"][-1], snapshot["models"][0]):
            with self.subTest(bucket=bucket.get("id", bucket.get("date", "aggregate"))):
                self.assertEqual(bucket["average_turn_tps"], 30)
                self.assertEqual(bucket["combined_tps"], 60)
                self.assertEqual(bucket["speed_output_tokens"], 600)
                self.assertEqual(bucket["speed_samples"], 2)
                self.assertEqual(bucket["active_seconds"], 20)
                self.assertEqual(bucket["active_wall_seconds"], 10)
                self.assertEqual(bucket["parallelism"], 2)
        self.assertEqual(len(snapshot["sessions"]), 1)
        for thread in snapshot["threads"]:
            self.assertEqual(thread["average_turn_tps"], 30)
            self.assertEqual(thread["combined_tps"], 30)
            self.assertEqual(thread["parallelism"], 1)

    def test_partly_overlapping_turns_use_fifteen_seconds(self):
        self.parallel_pair("2026-10-22T02:00:15Z")
        summary = self.snapshot()["summary"]
        self.assertEqual(summary["active_seconds"], 20)
        self.assertEqual(summary["active_wall_seconds"], 15)
        self.assertEqual(summary["average_turn_tps"], 30)
        self.assertEqual(summary["combined_tps"], 40)
        self.assertAlmostEqual(summary["parallelism"], 4 / 3, delta=0.01)

    def test_separate_turns_do_not_count_idle_gap_in_wall_time(self):
        self.parallel_pair("2026-10-22T02:00:30Z")
        summary = self.snapshot()["summary"]
        self.assertEqual(summary["active_seconds"], 20)
        self.assertEqual(summary["active_wall_seconds"], 20)
        self.assertEqual(summary["combined_tps"], 30)
        self.assertEqual(summary["parallelism"], 1)

    def test_nested_and_chained_intervals_merge_without_double_counting(self):
        self.write_turn("outer", end="2026-10-22T02:00:20Z", milliseconds=20000,
                        output=600)
        self.write_turn("inner", end="2026-10-22T02:00:10Z", milliseconds=5000,
                        output=150)
        self.write_turn("chain", end="2026-10-22T02:00:30Z", milliseconds=15000,
                        output=450)
        summary = self.snapshot()["summary"]
        self.assertEqual(summary["active_seconds"], 40)
        self.assertEqual(summary["active_wall_seconds"], 30)
        self.assertEqual(summary["average_turn_tps"], 30)
        self.assertEqual(summary["combined_tps"], 40)

    def test_no_valid_speed_samples_return_null_instead_of_zero_or_infinity(self):
        self.write("unfinished.jsonl", [meta(), context(),
                   modern("unfinished", tokens(100, 20))])
        snapshot = self.snapshot()
        for bucket in (snapshot["summary"], snapshot["sessions"][0],
                       snapshot["threads"][0], snapshot["daily"][-1],
                       snapshot["models"][0]):
            self.assertIsNone(bucket["average_turn_tps"])
            self.assertIsNone(bucket["combined_tps"])
            self.assertIsNone(bucket["ttft_median_ms"])
            self.assertEqual(bucket["speed_samples"], 0)
            self.assertEqual(bucket["active_wall_seconds"], 0)
        self.assertEqual(snapshot["summary"]["observed_turn_count"], 1)
        self.assertEqual(snapshot["summary"]["excluded_turn_count"], 1)
        json.dumps(snapshot, allow_nan=False)

    def test_zero_output_with_usage_is_a_real_zero_speed_sample(self):
        self.write("zero-output.jsonl", [
            meta(), context("zero"),
            modern("zero", tokens(100, 0), turn="zero"),
            complete("zero", 10000),
            complete("no-usage", 10000),
        ])
        summary = self.snapshot()["summary"]
        self.assertEqual(summary["speed_samples"], 1)
        self.assertEqual(summary["speed_output_tokens"], 0)
        self.assertEqual(summary["active_seconds"], 10)
        self.assertEqual(summary["active_wall_seconds"], 10)
        self.assertEqual(summary["average_turn_tps"], 0)
        self.assertEqual(summary["combined_tps"], 0)
        self.assertEqual(summary["observed_turn_count"], 1)

    def test_invalid_durations_do_not_pollute_valid_speeds(self):
        rows = [meta()]
        invalid = [float("inf"), float("-inf"), float("nan"), 0, -1000, "1000"]
        for number, duration in enumerate(invalid):
            turn = "invalid-" + str(number)
            rows.extend([context(turn), modern(turn, tokens(10, 30), turn=turn),
                         complete(turn, duration)])
        rows.extend([context("valid"), modern("valid", tokens(10, 300), turn="valid"),
                     complete("valid", 10000)])
        path = self.write("duration-validation.jsonl", rows)
        parsed = parse_rollout(path)
        self.assertEqual(len(parsed["completed"]), 1)
        summary = self.snapshot()["summary"]
        self.assertEqual(summary["speed_samples"], 1)
        self.assertEqual(summary["active_seconds"], 10)
        self.assertEqual(summary["average_turn_tps"], 30)
        self.assertEqual(summary["combined_tps"], 30)
        json.dumps(summary, allow_nan=False)

    def test_ttft_is_median_of_valid_observed_values(self):
        self.write_turn("a", ttft=100)
        self.write_turn("b", ttft=300)
        self.write_turn("c", ttft=float("inf"))
        self.write_turn("d", ttft=-10)
        self.write_turn("e")
        summary = self.snapshot()["summary"]
        self.assertEqual(summary["ttft_median_ms"], 200)
        self.assertEqual(summary["speed_samples"], 5)
        json.dumps(summary, allow_nan=False)

    def test_default_task_scope_excludes_review_and_explicit_scopes_are_disjoint(self):
        self.write_turn("main", output=20)
        self.write_turn("agent", output=10, source={"subagent": {"thread_spawn": {}}})
        self.write_turn("guardian", output=5, model="codex-auto-review",
                        source={"subagent": {"other": "guardian"}})
        expected = {"task": (230, {"main", "agent"}),
                    "all": (335, {"main", "agent", "guardian"}),
                    "main": (120, {"main"}), "agent": (110, {"agent"}),
                    "review": (105, {"guardian"})}
        self.assertEqual(self.snapshot()["summary"]["total_tokens"], 230)
        for scope, (total, ids) in expected.items():
            with self.subTest(scope=scope):
                snapshot = self.snapshot(scope=scope)
                self.assertEqual(snapshot["summary"]["total_tokens"], total)
                self.assertEqual({t["id"] for t in snapshot["threads"]}, ids)
                self.assertEqual(snapshot["summary"]["speed_samples"], len(ids))
                self.assertEqual(snapshot["sessions"][0]["id"], "main")
        self.assertEqual(self.snapshot()["summary"]["review_tokens"], 0)

    def test_model_filter_keeps_event_models_and_excludes_partial_mixed_turn(self):
        self.write("mixed-model.jsonl", [
            meta(), context("mixed", "model-a"),
            modern("mixed-a", tokens(10, 30), turn="mixed"),
            context("mixed", "model-b"),
            modern("mixed-b", tokens(20, 90), turn="mixed"),
            complete("mixed", 10000),
            context("single", "model-a"),
            modern("single-a", tokens(30, 60), turn="single"),
            complete("single", 10000),
        ])
        all_models = self.snapshot()
        self.assertEqual(all_models["summary"]["total_tokens"], 240)
        self.assertEqual(all_models["summary"]["speed_samples"], 2)
        self.assertEqual(all_models["summary"]["average_turn_tps"], 9)
        filtered = self.snapshot(model="model-a")
        summary = filtered["summary"]
        self.assertEqual(summary["total_tokens"], 130)
        self.assertEqual(summary["output_tokens"], 90)
        self.assertEqual(summary["speed_output_tokens"], 60)
        self.assertEqual(summary["speed_samples"], 1)
        self.assertEqual(summary["active_seconds"], 10)
        self.assertEqual(summary["average_turn_tps"], 6)
        self.assertEqual(summary["mixed_model_excluded_turn_count"], 1)
        self.assertEqual({m["model"] for m in filtered["models"]}, {"model-a"})
        model_b = self.snapshot(model="model-b")["summary"]
        self.assertEqual(model_b["total_tokens"], 110)
        self.assertEqual(model_b["speed_samples"], 0)
        self.assertIsNone(model_b["average_turn_tps"])

    def test_completion_outside_range_keeps_usage_and_daily_has_own_boundary(self):
        self.write("completion-next-day.jsonl", [
            meta(), context(),
            modern("cross-day", tokens(200, 100), at="2026-10-21T15:59:55Z"),
            complete("turn-1", 10000, at="2026-10-21T16:00:05Z"),
        ])
        one_day = self.snapshot("custom", start="2026-10-21", end="2026-10-21")
        for bucket in (one_day["summary"], one_day["sessions"][0],
                       one_day["threads"][0], one_day["daily"][0], one_day["models"][0]):
            self.assertEqual(bucket["total_tokens"], 300)
            self.assertEqual(bucket["observed_turn_count"], 1)
            self.assertEqual(bucket["outside_range_turn_count"], 1)
            self.assertEqual(bucket["excluded_turn_count"], 0)
            self.assertEqual(bucket["speed_samples"], 0)
            self.assertIsNone(bucket["average_turn_tps"])
            self.assertIsNone(bucket["combined_tps"])
        two_days = self.snapshot("custom", start="2026-10-21", end="2026-10-22")
        for bucket in (two_days["summary"], two_days["sessions"][0],
                       two_days["threads"][0], two_days["models"][0]):
            self.assertEqual(bucket["total_tokens"], 300)
            self.assertEqual(bucket["observed_turn_count"], 1)
            self.assertEqual(bucket["outside_range_turn_count"], 0)
            self.assertEqual(bucket["excluded_turn_count"], 0)
            self.assertEqual(bucket["speed_samples"], 1)
            self.assertEqual(bucket["average_turn_tps"], 10)
        usage_day, completion_day = two_days["daily"]
        self.assertEqual(usage_day["total_tokens"], 300)
        self.assertEqual(usage_day["outside_range_turn_count"], 1)
        self.assertEqual(usage_day["speed_samples"], 0)
        self.assertEqual(completion_day["total_tokens"], 0)
        self.assertEqual(completion_day["outside_range_turn_count"], 0)
        self.assertEqual(completion_day["speed_samples"], 1)
        self.assertEqual(completion_day["average_turn_tps"], 10)

    def test_future_completion_is_missing_timing_with_usage_preserved(self):
        self.write("future-completion.jsonl", [
            meta(), context(),
            modern("future", tokens(100, 20), at="2026-10-22T11:59:59Z"),
            complete("turn-1", 10000, at="2026-10-22T12:00:01Z"),
        ])
        snapshot = self.snapshot("1")
        for bucket in (snapshot["summary"], snapshot["sessions"][0],
                       snapshot["threads"][0], snapshot["daily"][0], snapshot["models"][0]):
            self.assertEqual(bucket["total_tokens"], 120)
            self.assertEqual(bucket["observed_turn_count"], 1)
            self.assertEqual(bucket["excluded_turn_count"], 1)
            self.assertEqual(bucket["outside_range_turn_count"], 0)
            self.assertEqual(bucket["speed_samples"], 0)
            self.assertEqual(bucket["active_seconds"], 0)
            self.assertEqual(bucket["active_wall_seconds"], 0)
            self.assertIsNone(bucket["average_turn_tps"])
            self.assertIsNone(bucket["combined_tps"])
        self.assertEqual(snapshot["sessions"][0]["turns"], [])

    def test_custom_dates_use_local_inclusive_boundaries(self):
        self.write("custom.jsonl", [
            meta(), context(),
            modern("before", tokens(10, 1), at="2026-10-20T15:59:59Z"),
            modern("first", tokens(20, 2), at="2026-10-20T16:00:00Z"),
            modern("last", tokens(30, 3), at="2026-10-21T15:59:59Z"),
            modern("after", tokens(40, 4), at="2026-10-21T16:00:00Z"),
        ])
        snapshot = self.snapshot("custom", start="2026-10-21", end="2026-10-21")
        self.assertEqual(snapshot["range"]["start"], "2026-10-21")
        self.assertEqual(snapshot["range"]["end"], "2026-10-21")
        self.assertEqual(snapshot["summary"]["total_tokens"], 55)
        self.assertEqual(len(snapshot["daily"]), 1)
        self.assertEqual(snapshot["daily"][0]["total_tokens"], 55)

    def test_custom_speed_keeps_complete_turn_before_range_start(self):
        self.write("cross-boundary.jsonl", [
            meta(), context(),
            modern("before", tokens(100, 30), at="2026-10-20T15:59:59Z"),
            complete("turn-1", 3000, at="2026-10-20T16:00:01Z"),
        ])
        snapshot = self.snapshot("custom", start="2026-10-21", end="2026-10-21")
        summary = snapshot["summary"]
        self.assertEqual(summary["total_tokens"], 0)
        self.assertEqual(summary["speed_output_tokens"], 30)
        self.assertEqual(summary["active_wall_seconds"], 3)
        self.assertEqual(summary["average_turn_tps"], 10)
        self.assertEqual(summary["combined_tps"], 10)

    def test_latest_sidebar_title_wins_even_when_index_rows_are_out_of_order(self):
        self.write_turn("main")
        with sqlite3.connect(self.home / "state_5.sqlite") as conn:
            conn.execute("CREATE TABLE threads (id TEXT, title TEXT, first_user_message TEXT)")
            conn.execute("INSERT INTO threads VALUES (?, ?, ?)",
                         ("main", "Original title", "PRIVATE-BODY-SHOULD-NOT-APPEAR"))
        (self.home / "session_index.jsonl").write_text("\n".join(json.dumps(item) for item in [
            {"id": "main", "thread_name": "Latest sidebar title", "updated_at": "2026-10-22T03:00:00Z"},
            {"id": "main", "thread_name": "Older sidebar title", "updated_at": "2026-10-21T03:00:00Z"},
        ]) + "\n", encoding="utf-8")
        snapshot = self.snapshot()
        self.assertEqual(snapshot["sessions"][0]["title"], "Latest sidebar title")
        self.assertEqual(snapshot["threads"][0]["title"], "Latest sidebar title")
        self.assertNotIn("PRIVATE-BODY-SHOULD-NOT-APPEAR", json.dumps(snapshot))

    def test_one_day_range_is_today_in_shanghai(self):
        self.write("one-day.jsonl", [meta(),
                   modern("yesterday", tokens(100, 10), at="2026-10-21T15:59:59Z"),
                   modern("today", tokens(20, 2), at="2026-10-21T16:00:00Z")])
        snapshot = self.snapshot("1")
        self.assertEqual(snapshot["range"]["start"], "2026-10-22")
        self.assertEqual(snapshot["range"]["end"], "2026-10-22")
        self.assertEqual(snapshot["summary"]["total_tokens"], 22)
        self.assertEqual(len(snapshot["daily"]), 1)

    def test_invalid_filters_raise_value_error_and_366_day_limit_is_inclusive(self):
        invalid = [
            {"days": "0"}, {"days": "abc"}, {"scope": "automatic"},
            {"days": "custom"},
            {"days": "custom", "start": "2026-10-20", "end": "2026-10-19"},
            {"days": "custom", "start": "2026-02-30", "end": "2026-10-22"},
            {"days": "custom", "start": "not-a-date", "end": "2026-10-22"},
            {"days": "custom", "start": "2025-10-20", "end": "2026-10-22"},
        ]
        for query in invalid:
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.snapshot(**query)
        snapshot = self.snapshot("custom", start="2025-10-22", end="2026-10-22")
        self.assertEqual(len(snapshot["daily"]), 366)


class HTTPV2Tests(TurnFixture):

    def setUp(self):
        RolloutTestCase.setUp(self)
        spec = importlib.util.spec_from_file_location("codex_pulse_http_tests", MODULE_PATH.with_name("server.py"))
        server_module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"usage": usage_module}):
            spec.loader.exec_module(server_module)
        original = self.index.snapshot
        self.index.snapshot = lambda *args, **kwargs: original(*args, now=NOW, **kwargs)
        self.server = server_module.Server(0, self.index)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_server)
        self.base = "http://127.0.0.1:" + str(self.server.server_address[1])

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.assertFalse(self.worker.is_alive())

    def get(self, endpoint="/api/stats", **query):
        target = self.base + endpoint + ("?" + urlencode(query) if query else "")
        with urlopen(target, timeout=5) as response:
            return response.status, response.read(), dict(response.headers)

    def test_http_invalid_filters_return_400_not_internal_error(self):
        invalid = [
            {"days": "2"}, {"scope": "wrong"}, {"days": "custom"},
            {"days": "custom", "start": "2026-10-23", "end": "2026-10-22"},
            {"days": "custom", "start": "2025-10-20", "end": "2026-10-22"},
            {"days": "custom", "start": "2026-02-30", "end": "2026-10-22"},
        ]
        for query in invalid:
            with self.subTest(query=query):
                with self.assertRaises(HTTPError) as error:
                    self.get(**query)
                self.assertEqual(error.exception.code, 400)
                self.assertIn("error", json.loads(error.exception.read()))
        with self.assertRaises(HTTPError) as error:
            self.get("/api/export.csv", type="not-a-table")
        self.assertEqual(error.exception.code, 400)

    def test_http_csv_exports_match_same_scope_model_and_custom_date_filters(self):
        self.write_turn("main", output=300)
        self.write_turn("agent", output=60, model="model-b", source={"subagent": {"thread_spawn": {}}})
        self.write_turn("guardian", output=9, model="codex-auto-review", source={"subagent": {"other": "guardian"}})
        query = {"days": "custom", "start": "2026-10-22", "end": "2026-10-22",
                 "scope": "main", "model": "model-a"}
        status, raw, _ = self.get(**query)
        self.assertEqual(status, 200)
        snapshot = json.loads(raw)
        self.assertEqual(snapshot["summary"]["total_tokens"], 400)
        for table in ("sessions", "daily", "turns"):
            with self.subTest(table=table):
                status, raw, headers = self.get("/api/export.csv", type=table, **query)
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith("text/csv"))
                rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
                self.assertEqual(len(rows), 1)
                self.assertEqual(sum(int(item["输出Token"]) for item in rows), 300)
                if table != "turns":
                    self.assertEqual(sum(int(item["总Token"]) for item in rows), 400)
                self.assertEqual(rows[0]["开始日期"], query["start"])
                self.assertEqual(rows[0]["结束日期"], query["end"])
                self.assertEqual(rows[0]["时区"], "Asia/Shanghai")
        _, raw, _ = self.get("/api/export.csv", type="turns", **{**query, "model": "model-b"})
        self.assertEqual(list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))), [])

    def test_turn_csv_exports_all_matching_turns_beyond_recent_view_limit(self):
        rows = [meta()]
        for number in range(121):
            turn = "turn-" + str(number)
            rows.extend([context(turn),
                         modern("response-" + str(number), tokens(10, 30), turn=turn),
                         complete(turn, 1000)])
        self.write("many-turns.jsonl", rows)
        _, raw, _ = self.get(days="all")
        snapshot = json.loads(raw)
        self.assertEqual(snapshot["summary"]["speed_samples"], 121)
        self.assertEqual(snapshot["sessions"][0]["turns_total"], 121)
        self.assertEqual(len(snapshot["sessions"][0]["turns"]), 100)
        _, raw, _ = self.get("/api/export.csv", days="all", type="turns")
        exported = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        self.assertEqual(len(exported), 121)
        self.assertEqual(sum(int(item["输出Token"]) for item in exported), 3630)

    def test_http_rejects_cross_origin_and_unexpected_host(self):
        for headers in ({"Origin": "https://example.org"}, {"Host": "example.org"}):
            request = Request(self.base + "/api/stats", headers=headers)
            with self.subTest(headers=headers):
                with self.assertRaises(HTTPError) as error:
                    urlopen(request, timeout=5)
                self.assertEqual(error.exception.code, 403)

