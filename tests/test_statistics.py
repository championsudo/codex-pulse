"""Statistics boundaries and filter behavior using isolated synthetic logs."""
from datetime import datetime, timezone
import json
from zoneinfo import ZoneInfo

from test_usage import RolloutTestCase, meta, modern, complete, tokens
from usage import FIELDS, UsageIndex
from test_zcode import fixture, T


class StatisticsTests(RolloutTestCase):
    def snapshot_at(self, now, days='all', **kwargs):
        self.index.last_scan = 0
        return self.index.snapshot(days=days, now=now, **kwargs)

    def turn(self, sid, name, at, output, model='model-a', end=None, duration=60):
        entry = modern(name, tokens(100, output, cached=20, reasoning=output // 2), sid=sid, turn=name, at=at)
        entry['payload']['model'] = model
        return [entry, complete(name, duration * 1000, at=end or at)]

    def test_hourly_conserves_summary_and_local_midnight(self):
        self.write('a.jsonl', [meta('a'), *self.turn('a', 'one', '2026-10-21T15:59:00Z', 10),
                               *self.turn('a', 'two', '2026-10-21T16:00:00Z', 20)])
        snap = self.snapshot_at(datetime(2026, 10, 22, 12, tzinfo=timezone.utc), days='all')
        self.assertEqual([h['hour'] for h in snap['hourly']], list(range(24)))
        self.assertEqual(snap['hourly'][23]['output_tokens'], 10)
        self.assertEqual(snap['hourly'][0]['output_tokens'], 20)
        for field in (*FIELDS, 'request_count'):
            self.assertEqual(sum(h[field] for h in snap['hourly']), snap['summary'][field])
        today = self.snapshot_at(datetime(2026, 10, 22, 12, tzinfo=timezone.utc), days='1')
        self.assertEqual(today['hourly'][23]['request_count'], 0)
        self.assertEqual(today['hourly'][0]['request_count'], 1)

    def test_dst_repeated_hour_merges_and_spring_gap_is_empty(self):
        self.index = UsageIndex(self.home, tz='America/New_York', paseo_home=self.home/'paseo',
                                claude_home=self.home/'claude', opencode_home=self.home/'opencode', zcode_home=self.home/'zcode')
        self.write('a.jsonl', [meta('a'), *self.turn('a', 'first', '2026-11-01T05:30:00Z', 10),
                               *self.turn('a', 'second', '2026-11-01T06:30:00Z', 20)])
        snap = self.snapshot_at(datetime(2026, 11, 1, 8, tzinfo=timezone.utc))
        self.assertEqual(snap['hourly'][1]['output_tokens'], 30)
        self.assertEqual(snap['hourly'][1]['request_count'], 2)
        for field in (*FIELDS, 'request_count'):
            self.assertEqual(sum(h[field] for h in snap['hourly']), snap['summary'][field])
        self.write('a.jsonl', [meta('a'), *self.turn('a', 'spring', '2026-03-08T07:30:00Z', 10)])
        snap = self.snapshot_at(datetime(2026, 3, 8, 8, tzinfo=timezone.utc))
        self.assertEqual(snap['hourly'][2]['request_count'], 0)
        self.assertEqual(snap['hourly'][3]['request_count'], 1)

    def test_week_cutoff_and_range_independence(self):
        rows = [meta('a')]
        for name, at, out in [('before', '2026-10-18T15:59:59Z', 1),
                              ('start', '2026-10-18T16:00:00Z', 2),
                              ('previous', '2026-10-12T04:00:00Z', 3),
                              ('previous-after', '2026-10-12T04:00:01Z', 4),
                              ('now', '2026-10-19T04:00:00Z', 5),
                              ('future', '2026-10-19T04:00:01Z', 6)]:
            rows += self.turn('a', name, at, out)
        self.write('a.jsonl', rows)
        now = datetime(2026, 10, 19, 4, tzinfo=timezone.utc)
        all_data = self.snapshot_at(now)
        today_data = self.snapshot_at(now, days='1')
        custom = self.snapshot_at(now, days='custom', start='2026-10-10', end='2026-10-10')
        w = all_data['week_over_week']
        self.assertEqual(w, today_data['week_over_week'])
        self.assertEqual(w, custom['week_over_week'])
        self.assertEqual((w['current']['request_count'], w['current']['output_tokens']), (2, 7))
        self.assertEqual((w['previous']['request_count'], w['previous']['output_tokens']), (1, 3))
        self.assertEqual(w['current']['start_at'], '2026-10-19T00:00:00+08:00')
        self.assertEqual(w['previous']['end_at'], '2026-10-12T12:00:00+08:00')
        self.assertFalse(w['date_range_applies'])

    def test_speed_cross_week_overlap_mixed_models_and_zero(self):
        rows = [meta('a'), *self.turn('a', 'cross', '2026-10-18T15:59:50Z', 60,
                                      end='2026-10-18T16:00:50Z', duration=120),
                *self.turn('a', 'overlap', '2026-10-18T16:00:10Z', 120,
                           end='2026-10-18T16:01:10Z', duration=60),
                *self.turn('a', 'zero', '2026-10-18T16:03:00Z', 0, duration=60)]
        mixed = modern('mix-1', tokens(100, 10), sid='a', turn='mix', at='2026-10-18T16:04:00Z')
        mixed['payload']['model'] = 'model-a'
        other = modern('mix-2', tokens(100, 20), sid='a', turn='mix', at='2026-10-18T16:04:01Z')
        other['payload']['model'] = 'model-b'
        rows += [mixed, other, complete('mix', 60000, at='2026-10-18T16:05:00Z')]
        self.write('a.jsonl', rows)
        now = datetime(2026, 10, 19, 4, tzinfo=timezone.utc)
        all_snap = self.snapshot_at(now)
        current = all_snap['week_over_week']['current']
        self.assertEqual((current['speed_samples'], current['speed_output_tokens'], current['active_seconds']), (4, 210, 300))
        self.assertEqual(current['average_turn_tps'], 0.7)
        single = self.snapshot_at(now, model='model-a')['week_over_week']['current']
        self.assertEqual((single['speed_samples'], single['speed_output_tokens'], single['active_seconds']), (3, 180, 240))
        self.assertEqual(single['average_turn_tps'], 0.75)
        self.assertIsNone(all_snap['week_over_week']['previous']['average_turn_tps'])
        self.assertIsNone(all_snap['week_over_week']['change_pct']['average_turn_tps'])
        self.write('a.jsonl', [meta('a'), *self.turn('a', 'zero', '2026-10-18T16:03:00Z', 0)])
        week = self.snapshot_at(now)['week_over_week']
        self.assertEqual(week['current']['average_turn_tps'], 0)
        self.assertIsNone(week['change_pct']['average_turn_tps'])

    def test_speed_trends_ignore_global_model_and_exclude_mixed_from_model_series(self):
        rows = [meta('a'), *self.turn('a', 'one', '2026-10-19T00:00:00Z', 60, duration=60),
                *self.turn('a', 'parallel', '2026-10-19T00:00:00Z', 120, duration=60),
                *self.turn('a', 'zero', '2026-10-20T00:00:00Z', 0, duration=60),
                *self.turn('a', 'b', '2026-10-19T00:03:00Z', 30, model='model-b')]
        a = modern('mix-a', tokens(100, 10), sid='a', turn='mix', at='2026-10-19T00:04:00Z')
        b = modern('mix-b', tokens(100, 20), sid='a', turn='mix', at='2026-10-19T00:04:01Z')
        a['payload']['model'] = 'model-a'; b['payload']['model'] = 'model-b'
        rows += [a, b, complete('mix', 60000, at='2026-10-19T00:05:00Z')]
        self.write('a.jsonl', rows)
        now = datetime(2026, 10, 21, 4, tzinfo=timezone.utc)
        whole = self.snapshot_at(now, days='7')
        filtered = self.snapshot_at(now, days='7', model='model-b')
        self.assertEqual(whole['speed_trends'], filtered['speed_trends'])
        self.assertEqual(filtered['summary']['output_tokens'], 50)
        self.assertEqual(sum(h['output_tokens'] for h in filtered['hourly']), 50)
        trends = whole['speed_trends']
        all_days = {row['date']: row for row in trends['all']}
        for day in whole['daily']:
            if day['speed_samples']:
                for field in ('speed_samples', 'average_turn_tps', 'combined_tps'):
                    self.assertEqual(all_days[day['date']][field], day[field])
        a_days = {row['date']: row for row in trends['models']['model-a']}
        self.assertEqual((a_days['2026-10-19']['speed_samples'], a_days['2026-10-19']['average_turn_tps'],
                          a_days['2026-10-19']['combined_tps']), (2, 1.5, 3))
        self.assertEqual((a_days['2026-10-20']['speed_samples'], a_days['2026-10-20']['average_turn_tps']), (1, 0))
        self.assertNotIn('2026-10-21', a_days)
        self.assertEqual(trends['models']['model-b'][0]['speed_samples'], 1)
        self.assertEqual(self.snapshot_at(now, days='7', provider='claude')['speed_trends'], {'all': [], 'models': {}})

    def test_scope_model_provider_origin_filters(self):
        self.write('a.jsonl', [meta('a'), *self.turn('a', 'a', '2026-10-19T00:00:00Z', 10)])
        self.write('b.jsonl', [meta('b', source={'subagent': {'thread_spawn': {}}}),
                               *self.turn('b', 'b', '2026-10-19T01:00:00Z', 20, model='model-b')])
        agent = self.home/'paseo'/'agents'/'a.json'
        agent.parent.mkdir(parents=True)
        agent.write_text(json.dumps({'provider':'codex', 'persistence':{'provider':'codex','sessionId':'a'}}))
        now = datetime(2026, 10, 19, 4, tzinfo=timezone.utc)
        for filt, expected in (({'scope':'main'}, 10), ({'scope':'agent'}, 20),
                               ({'model':'model-b'}, 20), ({'provider':'claude'}, 0),
                               ({'origin':'paseo'}, 10), ({'origin':'other'}, 20)):
            snap = self.snapshot_at(now, **filt)
            self.assertEqual(snap['week_over_week']['current']['output_tokens'], expected, filt)
            self.assertEqual(sum(h['output_tokens'] for h in snap['hourly']), expected, filt)

    def test_previous_spring_gap_normalized_and_current_fold_retained(self):
        self.index = UsageIndex(self.home, tz='America/New_York', paseo_home=self.home/'paseo',
                                claude_home=self.home/'claude', opencode_home=self.home/'opencode', zcode_home=self.home/'zcode')
        now = datetime(2026, 3, 15, 2, 30, tzinfo=ZoneInfo('America/New_York'))
        week = self.snapshot_at(now)['week_over_week']
        self.assertEqual(week['previous']['end_at'], '2026-03-08T03:30:00-04:00')
        now = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo('America/New_York'), fold=1)
        week = self.snapshot_at(now)['week_over_week']
        self.assertEqual(week['current']['end_at'], '2026-11-01T01:30:00-05:00')
        self.assertEqual(week['previous']['end_at'], '2026-10-25T01:30:00-04:00')

    def test_week_percentage_with_observed_zero_and_positive_baseline(self):
        self.write('a.jsonl', [meta('a'),
                   *self.turn('a', 'previous', '2026-10-12T00:00:00Z', 20),
                   *self.turn('a', 'current', '2026-10-19T00:00:00Z', 0)])
        now = datetime(2026, 10, 19, 4, tzinfo=timezone.utc)
        week = self.snapshot_at(now)['week_over_week']
        self.assertEqual(week['change_pct']['total_tokens'], -16.67)
        self.assertEqual(week['change_pct']['output_tokens'], -100)
        self.assertEqual(week['change_pct']['average_turn_tps'], -100)
        (self.home / 'sessions' / 'a.jsonl').unlink()
        self.write('b.jsonl', [meta('a'),
                   *self.turn('a', 'previous', '2026-10-12T00:00:00Z', 0),
                   *self.turn('a', 'current', '2026-10-19T00:00:00Z', 20)])
        week = self.snapshot_at(now)['week_over_week']
        self.assertEqual(week['previous']['average_turn_tps'], 0)
        self.assertIsNone(week['change_pct']['output_tokens'])
        self.assertIsNone(week['change_pct']['average_turn_tps'])

    def test_zcode_statistics_with_other_provider_present(self):
        fixture(self.home/'zcode'/'cli'/'db'/'db.sqlite').close()
        self.write('codex.jsonl', [meta('a'),
                   *self.turn('a', 'codex', '2026-10-26T12:00:10Z', 999)])
        snap = self.snapshot_at(datetime.fromtimestamp(T + 100, timezone.utc), provider='zcode')
        self.assertEqual(snap['summary']['total_tokens'], 200)
        self.assertEqual(sum(row['total_tokens'] for row in snap['hourly']), 200)
        day = next(row for row in snap['daily'] if row['speed_samples'])
        self.assertEqual((day['speed_samples'], day['speed_output_tokens'], day['average_turn_tps']), (1, 30, 1))
        week = snap['week_over_week']['current']
        self.assertEqual((week['total_tokens'], week['output_tokens'], week['request_count']), (200, 30, 2))
        self.assertEqual((week['speed_samples'], week['speed_output_tokens'], week['average_turn_tps']), (1, 30, 1))
        self.assertTrue(all(row['provider'] == 'zcode' for row in snap['threads']))
        self.assertGreater(self.snapshot_at(datetime.fromtimestamp(T + 100, timezone.utc))['summary']['total_tokens'], 200)
