"""Synthetic regressions for speed factor cohorts and their data boundaries."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from test_usage import RolloutTestCase, meta, modern, complete, legacy, tokens
from usage import calculate_speed_factors


class FactorMathTests(unittest.TestCase):
    def factors(self, completions, turns, **filters):
        return calculate_speed_factors(completions, turns, 1000, lambda ts: True,
            lambda sid: not sid.startswith('outside'), lambda sid: 'codex' if not sid.startswith('outside') else 'claude',
            lambda sid: 'other', filters.get('provider','all'), 'all', filters.get('model','all'))

    def test_overlap_cross_filter_half_open_self_and_future(self):
        completions = {('a','t'):{'interval_start':0,'timestamp':10,'seconds':10,'ttft_ms':80},
                       ('outside1','t'):{'interval_start':5,'timestamp':10,'seconds':5,'ttft_ms':None},
                       ('outside2','t'):{'interval_start':10,'timestamp':20,'seconds':10,'ttft_ms':None},
                       ('outside3','t'):{'interval_start':0,'timestamp':1001,'seconds':1001,'ttft_ms':None}}
        turns = {('a','t'):{'models':{'x'},'output':100,'context_known':True,'context_peak':8000}}
        data=self.factors(completions,turns,provider='codex'); group=data['groups'][0]
        self.assertEqual((data['concurrency_pool_samples'],data['eligible_samples']),(3,1))
        self.assertEqual([r['samples'] for r in group['concurrency']],[0,1,0,0])
        self.assertEqual((group['concurrency'][1]['median_tps'],group['concurrency'][1]['ttft_median_ms']),(10,80))
        self.assertEqual([r['samples'] for r in group['context']],[1,0,0,0])
        completions.pop(('outside1','t'))
        self.assertEqual([r['samples'] for r in self.factors(completions,turns)['groups'][0]['concurrency']],[1,0,0,0])

    def test_exact_concurrency_boundaries_and_context_edges(self):
        completions={}; turns={}
        for group, count in enumerate((1,2,4)):
            for n in range(count):
                key=(f'g{group}-{n}','t'); start=group*20
                completions[key]={'interval_start':start,'timestamp':start+10,'seconds':10,'ttft_ms':None}
                turns[key]={'models':{'x'},'output':10,'context_known':True,'context_peak':(7999,32000,128000)[group]}
        g=self.factors(completions,turns)['groups'][0]
        self.assertEqual([r['samples'] for r in g['concurrency']],[1,0,2,4])
        self.assertEqual([r['samples'] for r in g['context']],[1,2,4,0])

    def test_epoch_millisecond_solo_and_exact_overlap_boundaries(self):
        end=1793628000.123
        for duration in (20.006,30.005,60.111,0.2):
            for concurrent in (1,2,4):
                with self.subTest(duration=duration, concurrent=concurrent):
                    completions={}; turns={}
                    for n in range(concurrent):
                        key=(f's{n}','t')
                        completions[key]={'interval_start':end-duration,'timestamp':end,'seconds':duration,'ttft_ms':None}
                        turns[key]={'models':{'x'},'output':10,'context_known':True,'context_peak':100}
                    f=calculate_speed_factors(completions,turns,end+1,lambda _:True,lambda _:True,
                                              lambda _:'codex',lambda _:'other','all','all','all')
                    expected={1:[1,0,0,0],2:[0,0,2,0],4:[0,0,0,4]}[concurrent]
                    self.assertEqual([row['samples'] for row in f['groups'][0]['concurrency']],expected)

    def test_context_split_boundaries_unknown_and_baseline_comparison(self):
        completions={}; turns={}
        peaks=(100,8000,127999,128000,255999,256000,999999)
        for group, peak in enumerate(peaks):
            for n in range(5):
                key=(f's{group}-{n}','t'); start=(group*5+n)*20
                completions[key]={'interval_start':start,'timestamp':start+10,'seconds':10,'ttft_ms':None}
                output=100 if peak < 128000 else 200 if peak < 256000 else 300
                turns[key]={'models':{'x'},'output':output,'context_known':True,'context_peak':peak}
        key=('unknown','t'); completions[key]={'interval_start':800,'timestamp':810,'seconds':10,'ttft_ms':None}
        turns[key]={'models':{'x'},'output':10,'context_known':False,'context_peak':256000}
        data=self.factors(completions,turns); group=data['groups'][0]; bins=group['context']
        self.assertEqual([row['key'] for row in bins],['0_32k','32_128k','128_256k','256k_plus'])
        self.assertEqual([row['label'] for row in bins][-2:],['128k–<256k','256k及以上'])
        self.assertEqual([row['samples'] for row in bins],[10,5,10,10])
        self.assertEqual([row['baseline_delta_pct'] for row in bins],[0,0,100,200])
        self.assertEqual((data['eligible_samples'],data['context_unknown_samples'],group['context_known_samples']),(36,1,35))
        self.assertEqual([row['samples'] for row in group['concurrency']],[36,0,0,0])

    def test_merged_context_baseline_uses_all_turns_and_half_open_edges(self):
        completions={}; turns={}
        # Two formerly separate buckets have medians 1 and 11, but the merged
        # median is 1 because four of six turns belong to the smaller range.
        samples=[(0,10),(7999,10),(7999,10),(7999,10),(8000,110),(31999,110)]
        samples += [(32000,20)]*5 + [(128000,30)]*5 + [(256000,40)]*5
        for n,(peak,output) in enumerate(samples):
            key=(f's{n}','t'); start=n*20
            completions[key]={'interval_start':start,'timestamp':start+10,'seconds':10,'ttft_ms':None}
            turns[key]={'models':{'x'},'output':output,'context_known':True,'context_peak':peak}
        key=('unknown','t'); completions[key]={'interval_start':900,'timestamp':910,'seconds':10,'ttft_ms':None}
        turns[key]={'models':{'x'},'output':10,'context_known':False,'context_peak':0}
        data=self.factors(completions,turns); group=data['groups'][0]; rows=group['context']
        self.assertEqual([r['key'] for r in rows],['0_32k','32_128k','128_256k','256k_plus'])
        self.assertEqual([r['label'] for r in rows],['0–<32k','32k–<128k','128k–<256k','256k及以上'])
        self.assertEqual([r['samples'] for r in rows],[6,5,5,5])
        self.assertEqual([r['median_tps'] for r in rows],[1,2,3,4])
        self.assertEqual([r['baseline_delta_pct'] for r in rows],[0,100,200,300])
        self.assertEqual((data['eligible_samples'],data['context_unknown_samples'],group['context_known_samples']),(22,1,21))
        self.assertEqual([r['samples'] for r in group['concurrency']],[22,0,0,0])

    def test_bins_comparisons_zero_and_ttft(self):
        completions={}; turns={}
        for n in range(10):
            key=(f's{n}','t'); start=n*20
            completions[key]={'interval_start':start,'timestamp':start+10,'seconds':10,'ttft_ms':n if n%2==0 else None}
            turns[key]={'models':{'x'},'output':100 if n<5 else 0,'context_known':True,'context_peak':0 if n<5 else 32000}
        data=self.factors(completions,turns); group=data['groups'][0]
        self.assertEqual((group['context'][0]['samples'],group['context'][1]['samples']),(5,5))
        self.assertEqual((group['context'][1]['median_tps'],group['context'][1]['baseline_delta_pct']),(0,-100))
        self.assertEqual((group['context'][0]['ttft_samples'],group['context'][0]['ttft_median_ms']),(3,2))
        self.assertIsNone(group['context'][2]['baseline_delta_pct'])
        for n in range(5): turns[(f's{n}','t')]['output']=0
        self.assertIsNone(self.factors(completions,turns)['groups'][0]['context'][1]['baseline_delta_pct'])


class FactorIntegrationTests(RolloutTestCase):
    def test_mixed_unknown_missing_and_legacy_input(self):
        rows=[meta('a')]
        def add(name, model, inp, output=10):
            usage=tokens(inp,output); entry=modern(name,usage,sid='a',turn=name); entry['payload']['model']=model
            rows.extend([entry,complete(name,10000)])
        add('known','x',8000); add('unknown','unknown',100)
        broken=modern('broken',{'output_tokens':10},sid='a',turn='broken'); broken['payload']['model']='x'
        rows.extend([broken, complete('broken',10000)])
        for model in ('x','y'):
            e=modern('mix-'+model,tokens(100,10),sid='a',turn='mix'); e['payload']['model']=model; rows.append(e)
        rows.append(complete('mix',10000)); self.write('a.jsonl',rows)
        self.write('legacy.jsonl',[meta('l'),legacy(tokens(100,10)),complete('legacy-1',10000)])
        snap=self.snapshot(); f=snap['speed_factors']
        self.assertEqual((f['eligible_samples'],f['excluded_mixed_model_samples'],f['excluded_unknown_model_samples'],f['context_unknown_samples']),(2,1,1,1))
        self.assertEqual(f['groups'][0]['context_known_samples'],1)
        self.assertEqual(f['groups'][0]['context'][0]['samples'],1)

    def test_zcode_cache_context_requires_exact_unambiguous_total(self):
        from test_zcode import fixture, T
        c=fixture(self.home/'zcode'/'cli'/'db'/'db.sqlite')
        cases=[('missing', 'NULL', False, 150), ('neither', '999', False, 210),
               ('midpoint', 'input_tokens+output_tokens+(cache_creation_input_tokens+cache_read_input_tokens)/2', False, 150),
               ('included', 'input_tokens+output_tokens', True, 150),
               ('excluded', 'input_tokens+output_tokens+cache_creation_input_tokens+cache_read_input_tokens', True, 210)]
        for label, expression, known, old_input in cases:
            with self.subTest(label=label):
                c.execute(f'UPDATE model_usage SET provider_total_tokens={expression}, computed_total_tokens={expression}')
                c.commit(); self.index.last_scan=0
                data=self.index.snapshot(days='all',provider='zcode',now=datetime.fromtimestamp(T+100,timezone.utc))
                self.assertEqual(data['summary']['input_tokens'],old_input)
                self.assertEqual((data['speed_factors']['eligible_samples'],data['speed_factors']['context_unknown_samples']),
                                 (1,0 if known else 1))
        c.execute('UPDATE model_usage SET cache_creation_input_tokens=0, cache_read_input_tokens=0, provider_total_tokens=NULL, computed_total_tokens=NULL'); c.commit()
        self.index.last_scan=0
        data=self.index.snapshot(days='all',provider='zcode',now=datetime.fromtimestamp(T+100,timezone.utc))
        self.assertEqual(data['speed_factors']['context_unknown_samples'],0)
        c.close()

    def test_duplicate_response_and_partial_missing_input(self):
        first=modern('same',tokens(9000,30),sid='a',turn='t'); first['payload']['model']='x'
        missing=modern('missing',{'output_tokens':5},sid='a',turn='t'); missing['payload']['model']='x'
        self.write('a.jsonl',[meta('a'),first,first,missing,complete('t',10000)])
        data=self.snapshot(); factors=data['speed_factors']; group=factors['groups'][0]
        self.assertEqual((data['summary']['output_tokens'],factors['eligible_samples'],factors['context_unknown_samples']),(35,1,1))
        self.assertEqual((group['concurrency'][0]['median_tps'],group['context_known_samples']),(3.5,0))

    def test_fork_copied_completion_does_not_inflate_pool(self):
        at='2026-10-22T02:00:10Z'
        event=modern('response',tokens(9000,40),sid='main',turn='t',at=at)
        event['payload']['model']='model-a'
        finished=complete('t',10000,at=at)
        self.write('main.jsonl',[meta('main',timestamp='2026-10-22T01:59:00Z'),event,finished])
        before=self.snapshot()['speed_factors']
        self.assertEqual((before['concurrency_pool_samples'],before['eligible_samples']),(1,1))
        self.write('fork.jsonl',[meta('fork',timestamp='2026-10-22T02:00:20Z',forked_from_id='main'),event,finished])
        after=self.snapshot()['speed_factors']
        self.assertEqual((after['concurrency_pool_samples'],after['eligible_samples']),(1,1))
        self.assertEqual([r['samples'] for r in after['groups'][0]['concurrency']],[1,0,0,0])
        # A later completion actually owned by the fork remains in the pool.
        own=modern('fork-response',tokens(100,10),sid='fork',turn='own',at='2026-10-22T02:00:25Z')
        own['payload']['model']='model-a'
        self.write('fork.jsonl',[meta('fork',timestamp='2026-10-22T02:00:20Z',forked_from_id='main'),event,finished,
                                 own,complete('own',5000,at='2026-10-22T02:00:25Z')])
        self.assertEqual(self.snapshot()['speed_factors']['concurrency_pool_samples'],2)

    def test_copied_parent_completion_excluded_for_subagent_without_fork_marker(self):
        at='2026-10-22T02:00:10Z'
        event=modern('response',tokens(9000,40),sid='main',turn='t',at=at); event['payload']['model']='model-a'
        finished=complete('t',10000,at=at)
        self.write('main.jsonl',[meta('main',timestamp='2026-10-22T01:59:00Z'),event,finished])
        self.write('child.jsonl',[meta('child',timestamp='2026-10-22T02:00:20Z',parent_thread_id='main',
                                     source={'subagent':{'thread_spawn':{}}}),event,finished])
        f=self.snapshot(scope='all')['speed_factors']
        self.assertEqual((f['concurrency_pool_samples'],f['eligible_samples']),(1,1))
        self.assertEqual([r['samples'] for r in f['groups'][0]['concurrency']],[1,0,0,0])

    def test_copied_missing_input_cannot_override_canonical_response(self):
        at='2026-10-22T02:00:10Z'
        original=modern('response',tokens(9000,40),sid='main',turn='t',at=at); original['payload']['model']='model-a'
        finished=complete('t',10000,at=at)
        self.write('main.jsonl',[meta('main',timestamp='2026-10-22T01:59:00Z'),original,finished])
        for usage in ({'output_tokens':40}, {}):
            copy=modern('response',usage,sid='main',turn='t',at=at); copy['payload']['model']='model-a'
            self.write('fork.jsonl',[meta('fork',timestamp='2026-10-22T02:00:20Z',forked_from_id='main'),copy,finished])
            data=self.snapshot(); f=data['speed_factors']; group=f['groups'][0]
            self.assertEqual((data['summary']['input_tokens'],f['eligible_samples'],f['context_unknown_samples']),(9000,1,0))
            self.assertEqual((group['context_known_samples'],group['context'][0]['samples']),(1,1))
        # A distinct zero-token record without input still makes this turn unknown.
        missing=modern('independent-missing',{},sid='main',turn='t',at=at); missing['payload']['model']='model-a'
        self.write('main.jsonl',[meta('main',timestamp='2026-10-22T01:59:00Z'),original,missing,finished])
        self.assertEqual(self.snapshot()['speed_factors']['context_unknown_samples'],1)

    def test_old_snapshot_and_csv_are_identical_to_baseline(self):
        event=modern('same',tokens(9000,40),sid='a',turn='t'); event['payload']['model']='model-a'
        finished=complete('t',10000)
        self.write('a.jsonl',[meta('a'),event,finished])
        self.write('fork.jsonl',[meta('fork',timestamp='2026-10-22T03:00:00Z',forked_from_id='a'),event,finished])
        candidate=Path(__file__).resolve().parents[1]
        baseline=candidate.parents[1]/'work'/'speed-factors'/'baseline'
        if not baseline.exists(): self.skipTest('external comparison baseline unavailable')
        script = """import json,sys
from datetime import datetime,timezone
from pathlib import Path
from usage import UsageIndex
from server import export_csv
home=Path(sys.argv[1]); i=UsageIndex(home,'UTC',paseo_home=home/'paseo',claude_home=home/'claude',opencode_home=home/'opencode',zcode_home=home/'zcode')
d=i.snapshot(days='7',now=datetime(2026,10,22,12,tzinfo=timezone.utc)); d.pop('speed_factors',None)
print(json.dumps({'data':d,'csv':[export_csv(d,k).decode('utf-8-sig') for k in ('daily','sessions','turns')]},sort_keys=True))"""
        outputs=[]
        for root in (baseline,candidate):
            env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(root)}
            outputs.append(subprocess.check_output([sys.executable,'-c',script,str(self.home)],cwd=root,env=env,text=True))
        self.assertEqual(json.loads(outputs[0]),json.loads(outputs[1]))

    def test_all_legacy_input_is_unknown(self):
        row=legacy(tokens(100,10)); row['payload']['turn_id']='t'
        from test_live import started, context
        self.write('legacy.jsonl',[meta('l'),started('t'),context('t','x'),row,complete('t',10000)])
        data=self.snapshot()['speed_factors']; self.assertEqual((data['eligible_samples'],data['context_unknown_samples']),(1,1))

if __name__ == '__main__': unittest.main()
