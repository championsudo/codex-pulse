"""Synthetic live-usage regressions; never inspect the user's real Codex home."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import threading
from contextlib import closing
from datetime import timedelta
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen
from unittest.mock import patch

from test_usage import (MODULE_PATH, NOW, RolloutTestCase, complete, meta,
                        modern, row, tokens, usage_module)


def at(seconds=0):
    return (NOW + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def started(turn="live-turn", seconds=-120, **extra):
    return row("event_msg", {"type": "task_started", "turn_id": turn, **extra}, at(seconds))


def context(turn="live-turn", model="model-a", seconds=-120):
    return row("turn_context", {"turn_id": turn, "model": model}, at(seconds))


def counted(rid="live-response", output=120, seconds=-20, *, sid="main",
            turn="live-turn", root=None, input_count=100):
    return modern(rid, tokens(input_count, output), sid=sid, turn=turn,
                  root=root, at=at(seconds))


class LiveFixture(RolloutTestCase):
    def live(self, **filters):
        self.index.last_scan = 0
        return self.index.live_snapshot(now=NOW, **filters)

    def write_live(self, sid="main", *, start_seconds=-120, output=120,
                   usage_seconds=-20, model="model-a", source=None, parent=None,
                   root=None, extra=()):
        fields = {}
        if source is not None:
            fields["source"] = source
        if parent is not None:
            fields["parent_thread_id"] = parent
        created = meta(sid, **fields)
        created["payload"]["timestamp"] = at(start_seconds - 10)
        created["timestamp"] = at(start_seconds - 10)
        rows = [created, started(seconds=start_seconds),
                context(model=model, seconds=start_seconds)]
        if output is not None:
            rows.append(counted(sid + "-response", output, usage_seconds,
                                sid=sid, root=root))
        return self.write(sid + ".jsonl", [*rows, *extra])

    def turn_database(self, statuses):
        with closing(sqlite3.connect(self.home / "thread_history_1.sqlite")) as connection, connection:
            connection.execute("CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, status TEXT)")
            connection.executemany("INSERT INTO thread_turns VALUES (?, ?, ?)", statuses)


class LiveMetricsTests(LiveFixture):
    def test_empty_home_has_no_invented_active_thread_or_speed(self):
        data = self.live()
        self.assertEqual(data["threads"], [])
        self.assertEqual((data["active_count"], data["uncertain_count"], data["stale_count"]), (0, 0, 0))
        self.assertEqual(data["poll_seconds"], 10)
        self.assertEqual(data["window_seconds"], 60)
        self.assertEqual(data["fresh_seconds"], 300)

    def test_running_turn_uses_own_output_and_separate_recent_window(self):
        self.write("main.jsonl", [meta(), started(), context(),
            counted("earlier", 100, -90), counted("recent", 300, -20)])
        data = self.live()
        thread = data["threads"][0]
        self.assertEqual(data["active_count"], 1)
        self.assertEqual(thread["status"], "active")
        self.assertEqual(thread["output_tokens"], 400)
        self.assertEqual(thread["request_count"], 2)
        self.assertEqual(thread["elapsed_seconds"], 120)
        self.assertEqual(thread["average_tps"], 3.33)
        self.assertEqual(thread["rolling_output_tokens"], 300)
        self.assertEqual(thread["rolling_tps"], 5)
        self.assertEqual(thread["window_seconds"], 60)
        self.assertEqual(thread["activity_age_seconds"], 20)

    def test_completed_or_aborted_latest_turn_never_falls_back_to_old_open_turn(self):
        endings = [complete("new", 10000, at=at(-1)),
                   complete("new", 0, at=at(-1)),
                   complete("new", "invalid", at=at(-1)),
                   row("event_msg", {"type": "turn_aborted", "turn_id": "new",
                                      "reason": "interrupted"}, at(-1))]
        for ending in endings:
            with self.subTest(ending=ending["payload"]):
                self.write("main.jsonl", [meta(), started("old", -200),
                    counted("old-response", 50, -150, turn="old"),
                    started("new", -100), context("new", seconds=-100),
                    counted("new-response", 100, -20, turn="new"), ending])
                self.assertEqual(self.live()["threads"], [])

    def test_finished_previous_turn_does_not_leak_output_into_running_turn(self):
        self.write("main.jsonl", [meta(), started("old", -200),
            counted("old-response", 9000, -150, turn="old"),
            complete("old", 50000, at=at(-150)), started("new", -60),
            context("new", seconds=-60), counted("new-response", 120, -10, turn="new")])
        thread = self.live()["threads"][0]
        self.assertEqual(thread["turn_id"], "new")
        self.assertEqual(thread["output_tokens"], 120)
        self.assertEqual(thread["average_tps"], 2)

    def test_fresh_uncertain_and_stale_follow_last_activity_not_start_time(self):
        self.write_live("fresh", start_seconds=-600, usage_seconds=-300)
        self.write_live("uncertain", start_seconds=-600, usage_seconds=-301)
        self.write_live("day-boundary", start_seconds=-90000, usage_seconds=-86400)
        self.write_live("stale", start_seconds=-90000, usage_seconds=-86401)
        data = self.live()
        self.assertEqual((data["active_count"], data["uncertain_count"], data["stale_count"]), (1, 2, 1))
        threads = {item["thread_id"]: item for item in data["threads"]}
        self.assertEqual(threads["fresh"]["status"], "active")
        self.assertNotIn("stale", threads)
        for sid in ("uncertain", "day-boundary"):
            self.assertEqual(threads[sid]["status"], "uncertain")
            self.assertIsNone(threads[sid]["average_tps"])
            self.assertIsNone(threads[sid]["rolling_tps"])

    def test_first_usage_missing_is_unknown_but_real_zero_output_is_zero(self):
        self.write_live("waiting", output=None, start_seconds=-10)
        self.write_live("zero", output=0, start_seconds=-10, usage_seconds=-2)
        threads = {item["thread_id"]: item for item in self.live()["threads"]}
        self.assertEqual(threads["waiting"]["request_count"], 0)
        self.assertIsNone(threads["waiting"]["last_usage_at"])
        self.assertIsNone(threads["waiting"]["average_tps"])
        self.assertIsNone(threads["waiting"]["rolling_tps"])
        self.assertEqual(threads["zero"]["request_count"], 1)
        self.assertEqual(threads["zero"]["average_tps"], 0)
        self.assertEqual(threads["zero"]["rolling_tps"], 0)

    def test_short_turn_uses_elapsed_window_and_quiet_window_decays_to_zero(self):
        self.write_live("short", start_seconds=-10, output=100, usage_seconds=-2)
        self.write_live("quiet", start_seconds=-120, output=120, usage_seconds=-90)
        threads = {item["thread_id"]: item for item in self.live()["threads"]}
        self.assertEqual(threads["short"]["window_seconds"], 10)
        self.assertEqual(threads["short"]["rolling_tps"], 10)
        self.assertEqual(threads["quiet"]["rolling_output_tokens"], 0)
        self.assertEqual(threads["quiet"]["rolling_tps"], 0)
        self.assertEqual(threads["quiet"]["average_tps"], 1)

    def test_recent_window_excludes_left_edge_and_future_records(self):
        self.write("main.jsonl", [meta(), started(), context(),
            counted("edge", 120, -60), counted("now", 60, 0),
            counted("future", 10000, 10)])
        thread = self.live()["threads"][0]
        self.assertEqual(thread["output_tokens"], 180)
        self.assertEqual(thread["request_count"], 2)
        self.assertEqual(thread["rolling_output_tokens"], 60)
        self.assertEqual(thread["average_tps"], 1.5)
        self.assertEqual(thread["rolling_tps"], 1)
        self.assertEqual(thread["activity_age_seconds"], 0)

    def test_future_start_is_not_an_active_turn(self):
        self.write_live("future", start_seconds=5, output=None)
        self.assertEqual(self.live()["threads"], [])

    def test_response_dedup_and_modern_records_take_precedence_over_legacy(self):
        response = counted("shared", 120, -20)
        legacy = row("event_msg", {"type": "token_count", "info": {
            "total_token_usage": tokens(99999, 99999)}}, at(-10))
        self.write("main.jsonl", [meta(), started(), context(), response, response, legacy])
        self.write("copy.jsonl", [meta(), started(), context(), response], folder="archived_sessions")
        thread = self.live()["threads"][0]
        self.assertEqual(thread["request_count"], 1)
        self.assertEqual(thread["output_tokens"], 120)
        self.assertEqual(thread["average_tps"], 1)

    def test_fork_inherited_start_before_creation_is_not_running(self):
        child_meta = meta("child", parent_thread_id="main", forked_from_id="main",
                          source={"subagent": {"thread_spawn": {}}})
        child_meta["payload"]["timestamp"] = at(-60)
        child_meta["timestamp"] = at(-60)
        self.write("child.jsonl", [child_meta, meta("main"), started("inherited", -120),
            context("inherited", seconds=-120),
            counted("parent-response", 500, -90, sid="main", turn="inherited")])
        self.assertEqual(self.live()["threads"], [])

    def test_scope_filters_keep_thread_categories_and_nested_roots_clear(self):
        self.write_live("main")
        self.write_live("agent", source={"subagent": {"thread_spawn": {}}}, parent="main")
        self.write_live("nested", source={"subagent": {"thread_spawn": {}}}, parent="agent")
        self.write_live("review", source={"subagent": {"other": "guardian"}},
                        model="codex-auto-review", root="main")
        expected = {"task": {"main", "agent", "nested"}, "all": {"main", "agent", "nested", "review"},
                    "main": {"main"}, "agent": {"agent", "nested"}, "review": {"review"}}
        for scope, ids in expected.items():
            with self.subTest(scope=scope):
                data = self.live(scope=scope)
                self.assertEqual({item["thread_id"] for item in data["threads"]}, ids)
                self.assertEqual(data["filters"]["scope"], scope)
        threads = {item["thread_id"]: item for item in self.live(scope="all")["threads"]}
        self.assertEqual({item["root_id"] for item in threads.values()}, {"main"})
        self.assertEqual(threads["main"]["category"], "main")
        self.assertEqual(threads["nested"]["category"], "agent")
        self.assertEqual(threads["review"]["category"], "review")

    def test_mixed_model_filter_shows_partial_tokens_without_invented_average(self):
        self.write("main.jsonl", [meta(), started(), context(model="model-a"),
            counted("a", 120, -90), context(model="model-b", seconds=-40),
            counted("b", 300, -20)])
        all_models = self.live()["threads"][0]
        self.assertEqual(all_models["output_tokens"], 420)
        self.assertEqual(all_models["average_tps"], 3.5)
        selected = self.live(model="model-b")["threads"][0]
        self.assertTrue(selected["mixed_model"])
        self.assertEqual(set(selected["models"]), {"model-a", "model-b"})
        self.assertEqual(selected["output_tokens"], 300)
        self.assertIsNone(selected["average_tps"])
        self.assertEqual(selected["rolling_tps"], 5)
        self.assertEqual(self.live(model="absent")["threads"], [])

    def test_previous_turn_model_does_not_make_current_turn_mixed(self):
        self.write("main.jsonl", [meta(), started("old", -200),
            context("old", model="old-model", seconds=-200),
            counted("old-response", 9000, -150, turn="old"),
            complete("old", 50000, at=at(-150)), started("new", -60),
            context("new", model="new-model", seconds=-60),
            counted("new-response", 120, -10, turn="new")])
        thread = self.live(model="new-model")["threads"][0]
        self.assertFalse(thread["mixed_model"])
        self.assertEqual(thread["models"], ["new-model"])
        self.assertEqual(thread["average_tps"], 2)

    def test_integer_started_at_before_precise_creation_is_not_inherited_history(self):
        created = meta()
        created["timestamp"] = at(-59.5)
        created["payload"]["timestamp"] = at(-59.5)
        self.write("main.jsonl", [created,
            started(seconds=-59.3, started_at=int((NOW + timedelta(seconds=-60)).timestamp())),
            context(seconds=-59.2), counted(output=120, seconds=-10)])
        thread = self.live()["threads"][0]
        self.assertEqual(thread["status"], "active")
        self.assertEqual(thread["elapsed_seconds"], 60)
        self.assertEqual(thread["average_tps"], 2)

    def test_sqlite_terminal_status_closes_logs_but_inprogress_is_not_a_heartbeat(self):
        for sid in ("completed", "failed", "interrupted"):
            self.write_live(sid)
        self.write_live("stale", start_seconds=-90000, usage_seconds=-86401)
        self.write_live("uncertain", start_seconds=-500, usage_seconds=-400)
        self.turn_database([(sid, "live-turn", sid) for sid in ("completed", "failed", "interrupted")]
                           + [(sid, "live-turn", "inProgress") for sid in ("stale", "uncertain")])
        data = self.live()
        self.assertEqual(data["active_count"], 0)
        self.assertEqual(data["stale_count"], 1)
        self.assertEqual(data["uncertain_count"], 1)
        self.assertEqual([item["thread_id"] for item in data["threads"]], ["uncertain"])

    def test_activity_without_usage_keeps_a_turn_fresh(self):
        for event_kind, payload in (
                ("response_item", {"type": "function_call_output", "call_id": "tool-1", "output": ""}),
                ("event_msg", {"type": "item_completed", "turn_id": "live-turn", "item": {"type": "mcpToolCall"}})):
            with self.subTest(event_kind=event_kind):
                self.write_live(start_seconds=-600, usage_seconds=-400,
                                extra=[row(event_kind, payload, at(-10))])
                thread = self.live()["threads"][0]
                self.assertEqual(thread["status"], "active")
                self.assertEqual(thread["activity_age_seconds"], 10)
                self.assertEqual(thread["output_tokens"], 120)
                self.assertEqual(thread["rolling_tps"], 0)

    def test_explicit_old_turn_activity_does_not_refresh_new_turn(self):
        self.write("main.jsonl", [meta(), started("old", -1000),
            complete("old", 10000, at=at(-990)), started("new", -500),
            context("new", seconds=-500), counted("new-response", 120, -400, turn="new"),
            row("event_msg", {"type": "item_completed", "turn_id": "old",
                              "item": {"type": "mcpToolCall"}}, at(-1))])
        thread = self.live()["threads"][0]
        self.assertEqual(thread["turn_id"], "new")
        self.assertEqual(thread["status"], "uncertain")
        self.assertEqual(thread["activity_age_seconds"], 400)

    def test_live_snapshot_is_independent_of_last_historical_date_selection(self):
        self.write_live()
        before = self.live()
        self.snapshot("custom", start="2026-10-01", end="2026-10-02")
        after = self.live()
        self.assertEqual(after["threads"], before["threads"])
        self.assertEqual(after["active_count"], 1)


class LiveHTTPTests(LiveFixture):
    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location("codex_pulse_live_http_tests", MODULE_PATH.with_name("server.py"))
        server_module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"usage": usage_module}):
            spec.loader.exec_module(server_module)
        original = self.index.live_snapshot
        self.index.live_snapshot = lambda *args, **kwargs: original(*args, now=NOW, **kwargs)
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

    def get(self, endpoint="/api/live", **query):
        with urlopen(self.base + endpoint + ("?" + urlencode(query) if query else ""), timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_live_http_ignores_history_dates_but_applies_scope_and_model(self):
        self.write_live()
        self.write_live("agent", model="model-b", source={"subagent": {"thread_spawn": {}}}, parent="main")
        status, data = self.get(days="custom", start="2020-01-01", end="2020-01-02",
                                scope="agent", model="model-b")
        self.assertEqual(status, 200)
        self.assertEqual([item["thread_id"] for item in data["threads"]], ["agent"])
        self.assertEqual(data["filters"], {"scope": "agent", "model": "model-b", "provider": "all", "origin": "all"})

    def test_live_http_invalid_scope_returns_400(self):
        with self.assertRaises(HTTPError) as error:
            self.get(scope="invalid")
        self.assertEqual(error.exception.code, 400)
        self.assertIn("error", json.loads(error.exception.read()))
