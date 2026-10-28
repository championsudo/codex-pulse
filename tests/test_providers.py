"""Synthetic sources only: no test reads personal agent data."""
import csv
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from usage import UsageIndex
from providers import parse_claude, parse_opencode
from server import export_csv

NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
T = NOW.timestamp()
SECRET = "PRIVATE_BODY_SENTINEL_NOT_FOR_API"

class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.codex = self.root/'codex'; self.claude = self.root/'claude'; self.paseo = self.root/'paseo'; self.oc = self.root/'opencode'
        self.index = UsageIndex(self.codex, 'UTC', paseo_home=self.paseo, claude_home=self.claude, opencode_home=self.oc)
    def write(self, path, rows):
        path.parent.mkdir(parents=True,exist_ok=True); path.write_text(''.join(json.dumps(r)+'\n' for r in rows)); return path
    def cuser(self, i, dt=0, content=None, **kw):
        return dict(type='user', uuid=i, sessionId='same', timestamp=T+dt, message={'content': content or SECRET}, **kw)
    def cassistant(self, mid='m', dt=10, stop='end_turn', tokens=None, **kw):
        return dict(type='assistant', sessionId='same', timestamp=T+dt, message={'id':mid,'model':'model-c','content':[{'type':'text','text':SECRET}], 'stop_reason':stop,'usage':tokens or {'input_tokens':10,'output_tokens':20}}, **kw)
    def snapshot(self, **kw):
        self.index.last_scan=0; return self.index.snapshot(days=kw.pop('days','all'), now=datetime.fromtimestamp(T+100,timezone.utc),**kw)
    def codex_log(self, sid='same', parent=None, source=None, root=None):
        m={'id':sid,'timestamp':T-10}
        if parent:m['parent_thread_id']=parent
        if source:m['source']=source
        p={'thread_id':sid,'response_id':'r-'+sid,'turn_id':'t','model':'model-x','usage':{'input_tokens':3,'output_tokens':7}}
        if root:p['session_id']=root
        return self.write(self.codex/'sessions'/f'{sid}.jsonl',[{'type':'session_meta','timestamp':T,'payload':m},{'type':'event_msg','timestamp':T,'payload':{'type':'task_started','turn_id':'t'}},{'type':'token_usage_record','timestamp':T+3,'payload':p},{'type':'event_msg','timestamp':T+5,'payload':{'type':'task_complete','turn_id':'t'}}])
    def link(self, provider, sid='same', name='link', **kw):
        p=self.paseo/'agents'/f'{name}.json';p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(dict(provider=provider,persistence={'provider':provider,'sessionId':sid},**kw)));return p
    def db(self, v2=False):
        self.oc.mkdir(); p=self.oc/'opencode.db';c=sqlite3.connect(p)
        if v2:
            c.executescript('CREATE TABLE session_v2(id TEXT,parent_id TEXT,title TEXT,version TEXT); CREATE TABLE session_message(id TEXT,session_id TEXT,type TEXT,seq INTEGER,time_created INTEGER,time_updated INTEGER,data TEXT);')
            c.execute("INSERT INTO session_v2 VALUES('same',NULL,'Demo','2.0.10')")
        else:
            c.executescript('CREATE TABLE session(id TEXT,parent_id TEXT,title TEXT,version TEXT); CREATE TABLE message(id TEXT,session_id TEXT,time_created INTEGER,data TEXT); CREATE TABLE part(id TEXT,session_id TEXT,message_id TEXT,time_created INTEGER,data TEXT);')
            c.execute("INSERT INTO session VALUES('same',NULL,'Demo','1.19.4')")
        return p,c
    def octokens(self):return {'input':10,'output':20,'reasoning':5,'cache':{'read':6,'write':4}}
    def test_provider_collision_filters_and_csv(self):
        self.codex_log(); self.write(self.claude/'projects'/'p'/'same.jsonl',[self.cuser('u'),self.cassistant()]);self.link('claude')
        data=self.snapshot(); self.assertEqual(data['summary']['total_tokens'],40)
        self.assertEqual({r['id'] for r in data['sessions']},{'same','claude:same'})
        self.assertEqual(self.snapshot(provider='claude',origin='paseo')['summary']['total_tokens'],30)
        self.assertEqual(self.snapshot(provider='codex',origin='paseo')['summary']['total_tokens'],0)
        self.assertEqual(self.snapshot(origin='other')['summary']['total_tokens'],10)
        for kind in ('sessions','daily','turns'):
            rows=list(csv.reader(io.StringIO(export_csv(self.snapshot(provider='claude',origin='paseo'),kind).decode('utf-8-sig'))))
            self.assertIn('代理工具筛选',rows[0]); self.assertIn('Paseo关联筛选',rows[0]); self.assertEqual(rows[1][-2:],['claude','paseo'])
        self.assertNotIn(SECRET,json.dumps(data)); self.assertNotIn(SECRET,json.dumps(list(self.index.cache.values())))
    def test_paseo_multilevel_real_parent_and_independent_fork(self):
        self.codex_log();self.codex_log('child',source={'subagent':{'thread_spawn':{'parent_thread_id':'same'}}},root='child');self.codex_log('grand','child');self.link('codex')
        self.assertEqual(self.snapshot(origin='paseo')['summary']['thread_count'],3)
        p=self.codex_log('fork');text=p.read_text().replace('"id": "fork"','"id": "fork", "forked_from_id": "same"');p.write_text(text)
        self.assertEqual(self.snapshot(origin='paseo')['summary']['thread_count'],3)
        self.assertEqual(self.snapshot(origin='other')['summary']['thread_count'],1)
    def test_paseo_update_delete_malformed_conflict(self):
        self.codex_log();p=self.link('codex');self.assertEqual(self.snapshot(origin='paseo')['summary']['thread_count'],1)
        p.write_text('{');self.assertEqual(self.snapshot(origin='paseo')['summary']['thread_count'],0);self.assertTrue(self.snapshot()['source']['warnings'])
        p.write_text(json.dumps({'provider':'codex','persistence':{'provider':'claude','sessionId':'same'}}));self.assertEqual(self.snapshot(origin='paseo')['summary']['thread_count'],0)
        self.link('codex');self.assertEqual(self.snapshot(origin='paseo')['summary']['thread_count'],1)
        p.unlink();self.assertEqual(self.snapshot(origin='paseo')['summary']['thread_count'],0)
    def test_claude_last_snapshot_and_cache_semantics(self):
        rows=[self.cuser('u'), self.cassistant(tokens={'input_tokens':30,'output_tokens':90}), self.cassistant(dt=11,tokens={'input_tokens':10,'output_tokens':20,'cache_creation_input_tokens':4,'cache_creation':{'ephemeral_5m_input_tokens':4},'cache_read_input_tokens':6,'output_tokens_details':{'thinking_tokens':3}})]
        self.write(self.claude/'projects'/'p'/'same.jsonl',rows);s=self.snapshot()['summary']
        self.assertEqual((s['input_tokens'],s['output_tokens'],s['cached_input_tokens'],s['reasoning_output_tokens']),(20,20,6,3));self.assertEqual(s['request_count'],1);self.assertEqual(s['speed_samples'],1);self.assertEqual(s['reasoning_coverage'],'full')
    def test_claude_tool_loop_and_missing_thinking(self):
        rows=[self.cuser('u'),self.cassistant('m1',stop='tool_use'),self.cuser('tool',dt=12,content=[{'type':'tool_result','content':SECRET}]),self.cassistant('m2',dt=20)]
        self.write(self.claude/'projects'/'p'/'same.jsonl',rows);s=self.snapshot()['summary']
        self.assertEqual(s['speed_samples'],1);self.assertEqual(s['speed_output_tokens'],40);self.assertEqual(s['reasoning_coverage'],'none');self.assertEqual(s['average_turn_tps'],2)
    def test_claude_orphan_does_not_invent_speed(self):
        self.write(self.claude/'projects'/'p'/'same.jsonl',[self.cassistant()]);s=self.snapshot()['summary'];self.assertEqual(s['request_count'],1);self.assertEqual(s['speed_samples'],0)
    def test_claude_new_human_cannot_close_with_old_duplicate(self):
        rows=[self.cuser('u'),self.cassistant(),self.cuser('new',dt=30),self.cassistant(dt=35),self.cassistant('m2',dt=40)]
        self.write(self.claude/'projects'/'p'/'same.jsonl',rows);s=self.snapshot()['summary'];self.assertEqual(s['speed_samples'],2);self.assertEqual(s['speed_output_tokens'],40);self.assertEqual(s['active_seconds'],20)
    def test_claude_subagent_parent_and_coverage(self):
        self.write(self.claude/'projects'/'p'/'same.jsonl',[self.cuser('u'),self.cassistant(tokens={'input_tokens':1,'output_tokens':2,'output_tokens_details':{'thinking_tokens':0}})])
        self.write(self.claude/'projects'/'p'/'same'/'subagents'/'agent-child.jsonl',[self.cuser('u',agentId='child'),self.cassistant(agentId='child')]);self.link('claude')
        d=self.snapshot(origin='paseo');self.assertEqual(d['summary']['session_count'],1);self.assertEqual(d['summary']['thread_count'],2);self.assertEqual(d['summary']['reasoning_coverage'],'partial');self.assertIn('claude:same:agent:child',{r['id'] for r in d['threads']})
    def test_opencode_v1_steps_preferred_fallback_and_old(self):
        p,c=self.db();u=self.octokens()
        c.execute('INSERT INTO message VALUES(?,?,?,?)',('m1','same',T*1000,json.dumps({'role':'assistant','modelID':'o-model','time':{'completed':T*1000},'tokens':u,'content':SECRET})))
        for i in range(2):c.execute('INSERT INTO part VALUES(?,?,?,?,?)',(f'p{i}','same','m1',(T+i)*1000,json.dumps({'type':'step-finish','tokens':u,'text':SECRET})))
        c.execute('INSERT INTO message VALUES(?,?,?,?)',('m2','same',(T+3)*1000,json.dumps({'role':'assistant','modelID':'o-model','time':{'completed':T*1000},'tokens':u})));c.commit();c.close()
        s=self.snapshot(provider='opencode')['summary'];self.assertEqual(s['request_count'],3);self.assertEqual(s['total_tokens'],135);self.assertEqual(s['speed_samples'],0);self.assertEqual(s['reasoning_output_tokens'],15)
        c=sqlite3.connect(p);c.execute("UPDATE session SET version='1.2.0'");c.commit();c.close();self.assertEqual(self.snapshot(provider='opencode')['summary']['total_tokens'],0)
    def test_opencode_v2_idle_boundaries_and_no_live_claim(self):
        p,c=self.db(True)
        rows=[('user',0,{}),('assistant',1,{'tokens':self.octokens(),'model':{'id':'o-model'}}),('user',2,{}),('assistant',3,{'tokens':self.octokens(),'model':{'id':'o-model'}}),('idle',4,{'outcome':'succeeded'}),('user',5,{}),('assistant',6,{'tokens':self.octokens(),'model':{'id':'o-model'}})]
        for typ,seq,d in rows:
            d['time']={'created':(T+seq)*1000,'streamed':(T+seq)*1000}
            c.execute('INSERT INTO session_message VALUES(?,?,?,?,?,?,?)',(str(seq),'same',typ,seq,(T+seq)*1000,(T+seq)*1000,json.dumps(d)))
        c.commit();c.close();s=self.snapshot(provider='opencode')['summary'];self.assertEqual(s['request_count'],3);self.assertEqual(s['speed_samples'],1);self.assertEqual(s['speed_output_tokens'],50);self.assertEqual(s['active_seconds'],4)
        self.index.last_scan=0;self.assertEqual(self.index.live_snapshot(provider='opencode',now=NOW)['threads'],[])
    def test_bad_database_and_schema_do_not_break_codex(self):
        self.codex_log();self.oc.mkdir();p=self.oc/'opencode.db';p.write_bytes(b'invalid')
        self.assertEqual(self.snapshot()['summary']['total_tokens'],10);self.assertTrue(self.snapshot()['source']['warnings'])
        p.unlink();c=sqlite3.connect(p);c.execute('CREATE TABLE unknown(x)');c.close();self.assertEqual(self.snapshot()['summary']['total_tokens'],10)
    def test_live_filters_are_consistent(self):
        self.codex_log();self.write(self.claude/'projects'/'p'/'same.jsonl',[self.cuser('u'),self.cassistant(stop='tool_use')]);self.link('claude');self.index.last_scan=0
        d=self.index.live_snapshot(now=datetime.fromtimestamp(T+30,timezone.utc),provider='claude',origin='paseo');self.assertEqual(d['active_count'],1);self.assertEqual(d['threads'][0]['provider'],'claude');self.assertEqual(d['threads'][0]['origin'],'paseo');self.assertEqual(d['filters']['provider'],'claude')
    def test_invalid_filters(self):
        for kw in ({'provider':'bad'},{'origin':'bad'}):
            with self.assertRaises(ValueError):self.snapshot(**kw)
            with self.assertRaises(ValueError):self.index.live_snapshot(**kw)

    def test_claude_nonfinite_row_does_not_erase_valid_rows(self):
        self.write(self.claude/'projects'/'p'/'same.jsonl',[self.cuser('u'),self.cassistant(),self.cassistant('bad',tokens={'input_tokens':float('nan'),'output_tokens':3})])
        d=self.snapshot();self.assertEqual(d['summary']['total_tokens'],30);self.assertTrue(d['source']['warnings'])
    def test_opencode_bad_json_row_retains_valid_usage_and_model(self):
        p,c=self.db();c.execute('INSERT INTO message VALUES(?,?,?,?)',('m','same',T*1000,json.dumps({'role':'assistant','modelID':'o-model','time':{'completed':T*1000},'tokens':self.octokens()})))
        c.execute('INSERT INTO part VALUES(?,?,?,?,?)',('bad','same','m',T*1000,'{'))
        c.execute('INSERT INTO message VALUES(?,?,?,?)',('bad','same',T*1000,'{'));c.commit();c.close()
        d=self.snapshot(provider='opencode',model='o-model');self.assertEqual(d['summary']['total_tokens'],45);self.assertTrue(d['source']['warnings'])
    def test_opencode_v2_completed_date_and_complete_turn_output(self):
        p,c=self.db(True);midnight=datetime(2026,10,26,tzinfo=timezone.utc).timestamp()
        for seq,typ,ts,data in [(0,'user',midnight-10,{}),(1,'assistant',midnight-5,{'tokens':self.octokens(),'model':{'id':'o-model'},'time':{'created':(midnight-5)*1000,'streamed':(midnight+2)*1000}}),(2,'idle',midnight+5,{'outcome':'succeeded'})]:
            c.execute('INSERT INTO session_message VALUES(?,?,?,?,?,?,?)',(str(seq),'same',typ,seq,ts*1000,ts*1000,json.dumps(data)))
        c.commit();c.close();d=self.snapshot(days='custom',start='2026-10-26',end='2026-10-26',provider='opencode')
        self.assertEqual(d['summary']['total_tokens'],45);self.assertEqual(d['summary']['speed_output_tokens'],25);self.assertEqual(d['summary']['active_seconds'],15);self.assertEqual(d['summary']['today_tokens'],45)
    def test_claude_broken_tail_recovers_and_deleted_log_disappears(self):
        p=self.write(self.claude/'projects'/'p'/'same.jsonl',[self.cuser('u')]);p.write_text(p.read_text()+json.dumps(self.cassistant()))
        self.assertEqual(self.snapshot()['summary']['request_count'],0)
        p.write_text(p.read_text()+'\n');self.assertEqual(self.snapshot()['summary']['request_count'],1)
        p.unlink();self.assertEqual(self.snapshot()['summary']['request_count'],0)
    def test_paseo_duplicate_records_do_not_duplicate_usage(self):
        self.codex_log();self.link('codex',name='one');self.link('codex',name='two')
        self.assertEqual(self.snapshot(origin='paseo')['summary']['total_tokens'],10)
    def test_claude_is_meta_tool_only_and_compact_do_not_start_turns(self):
        self.write(self.claude/'projects'/'p'/'same.jsonl',[self.cuser('meta',isMeta=True),self.cuser('compact',isCompactSummary=True),self.cuser('tools',content=[{'type':'tool_result','content':SECRET}]),self.cassistant()])
        d=self.snapshot();self.assertEqual(d['summary']['speed_samples'],0);self.assertEqual(d['summary']['request_count'],1)

    def test_claude_invalid_final_snapshot_preserves_last_valid(self):
        self.write(self.claude/'projects'/'p'/'same.jsonl',[self.cuser('u'),self.cassistant(),self.cassistant(dt=11,tokens={'input_tokens':'invalid','output_tokens':None})])
        d=self.snapshot();self.assertEqual(d['summary']['total_tokens'],30);self.assertEqual(d['summary']['active_seconds'],10);self.assertTrue(d['source']['warnings'])
    def test_claude_final_snapshot_updates_one_completion(self):
        self.write(self.claude/'projects'/'p'/'same.jsonl',[self.cuser('u'),self.cassistant(),self.cassistant(dt=11,tokens={'input_tokens':10,'output_tokens':22})])
        s=self.snapshot()['summary'];self.assertEqual(s['speed_samples'],1);self.assertEqual(s['active_seconds'],11);self.assertEqual(s['speed_output_tokens'],22)
    def test_claude_old_message_replay_preserves_event_date(self):
        self.write(self.claude/'projects'/'p'/'same.jsonl',[self.cuser('u'),self.cassistant(),self.cuser('new',dt=30),self.cassistant(dt=90000)])
        d=self.snapshot();self.assertEqual(d['summary']['total_tokens'],30);self.assertEqual(d['summary']['speed_samples'],1)

    def test_bad_timestamp_isolated_and_warned(self):
        self.write(self.claude/'projects'/'p'/'same.jsonl',[self.cuser('u'),self.cassistant(),{**self.cassistant('bad'),'timestamp':-1e100}])
        d=self.snapshot();self.assertEqual(d['summary']['total_tokens'],30);self.assertTrue(d['source']['warnings'])
    def test_opencode_wrong_json_shape_does_not_erase_valid_usage(self):
        p,c=self.db(True)
        for seq,tokens in enumerate([self.octokens(),42,'invalid']):
            data={'tokens':tokens,'model':{'id':'o-model'} if seq==0 else 'broken','time':{'streamed':T*1000}}
            c.execute('INSERT INTO session_message VALUES(?,?,?,?,?,?,?)',(str(seq),'same','assistant',seq,T*1000,T*1000,json.dumps(data)))
        c.commit();c.close();d=self.snapshot(provider='opencode');self.assertEqual(d['summary']['total_tokens'],45);self.assertTrue(d['source']['warnings'])
    def test_none_reasoning_csv_is_blank_with_coverage(self):
        self.write(self.claude/'projects'/'p'/'same.jsonl',[self.cuser('u'),self.cassistant()]);d=self.snapshot()
        row=next(csv.DictReader(io.StringIO(export_csv(d).decode('utf-8-sig'))));self.assertEqual(row['推理输出Token'],'');self.assertEqual(row['推理记录完整性'],'none')

    def test_independent_fork_copied_parent_ledger_cannot_inherit_paseo(self):
        main=self.codex_log();fork=self.codex_log('fork',root='fork');rows=[json.loads(line) for line in fork.read_text().splitlines()]
        rows[0]['payload']['forked_from_id']='same';copied=json.loads(main.read_text().splitlines()[2]);copied['payload']['session_id']='same';rows.insert(1,copied)
        self.write(fork,rows);self.link('codex');d=self.snapshot(origin='paseo');self.assertEqual(d['summary']['thread_count'],1)
        self.assertEqual(self.snapshot(origin='other')['threads'][0]['id'],'fork')
    def test_mixed_group_origin_stable_and_filter_specific(self):
        self.codex_log();self.codex_log('child',parent='same');self.link('codex','child')
        d=self.snapshot();self.assertEqual(d['sessions'][0]['origin'],'mixed');self.assertEqual(d['sessions'][0]['origins'],['other','paseo'])
        self.index.cache=dict(reversed(list(self.index.cache.items())));self.assertEqual(self.snapshot()['sessions'][0]['origin'],'mixed')
        self.assertEqual(self.snapshot(origin='paseo')['sessions'][0]['origin'],'paseo');self.assertEqual(self.snapshot(origin='other')['sessions'][0]['origin'],'other')

    def test_huge_integer_usage_does_not_erase_valid_claude(self):
        self.write(self.claude/'projects'/'p'/'same.jsonl',[self.cuser('u'),self.cassistant(),self.cassistant('bad',tokens={'input_tokens':10**500,'output_tokens':1})])
        d=self.snapshot();self.assertEqual(d['summary']['total_tokens'],30);self.assertTrue(d['source']['warnings'])
    def test_v1_v2_projection_coexistence_and_broken_other_schema(self):
        p,c=self.db(True);c.execute('INSERT INTO session_message VALUES(?,?,?,?,?,?,?)',('v2','same','assistant',1,T*1000,T*1000,json.dumps({'model':{'id':'o'},'tokens':self.octokens(),'time':{'streamed':T*1000}})))
        c.executescript('CREATE TABLE session(id TEXT,parent_id TEXT,title TEXT,version TEXT);CREATE TABLE message(id TEXT,session_id TEXT,time_created INTEGER,data TEXT);CREATE TABLE part(id TEXT,session_id TEXT,message_id TEXT,time_created INTEGER,data TEXT);')
        c.execute("INSERT INTO session VALUES('same',NULL,'projection','1.19.4')");c.execute('INSERT INTO message VALUES(?,?,?,?)',('v1','same',T*1000,json.dumps({'role':'assistant','modelID':'o','tokens':self.octokens(),'time':{'completed':T*1000}})))
        c.commit();c.close();self.assertEqual(self.snapshot(provider='opencode')['summary']['total_tokens'],45)
        c=sqlite3.connect(p);c.execute('ALTER TABLE part RENAME TO old_part');c.execute('CREATE TABLE part(wrong TEXT)');c.commit();c.close();self.assertEqual(self.snapshot(provider='opencode')['summary']['total_tokens'],45)

    def test_claude_invalid_identifier_and_model_shapes_are_isolated(self):
        invalid=self.cassistant();invalid['message']['id']=[]
        model=self.cassistant('model');model['message']['model']={}
        self.write(self.claude/'projects'/'p'/'same.jsonl',[self.cuser('u'),self.cassistant(),invalid,model])
        d=self.snapshot();self.assertEqual(d['summary']['total_tokens'],60);self.assertTrue(d['source']['warnings'])

    def test_v2_messages_require_v2_session_not_v1_projection(self):
        p,c=self.db(True);c.executescript('CREATE TABLE session(id TEXT,parent_id TEXT,title TEXT,version TEXT);CREATE TABLE message(id TEXT,session_id TEXT,time_created INTEGER,data TEXT);CREATE TABLE part(id TEXT,session_id TEXT,message_id TEXT,time_created INTEGER,data TEXT);')
        c.execute("INSERT INTO session VALUES('old',NULL,'projection','1.2.0')")
        for sid in ('same','old'):
            c.execute('INSERT INTO session_message VALUES(?,?,?,?,?,?,?)',(sid,sid,'assistant',1,T*1000,T*1000,json.dumps({'tokens':self.octokens(),'model':{'id':'o'},'time':{'streamed':T*1000}})))
        c.commit();c.close();d=self.snapshot(provider='opencode');self.assertEqual(d['summary']['total_tokens'],45);self.assertEqual({r['id'] for r in d['threads']},{'opencode:same'})

    def test_live_known_reasoning_includes_measured_token_sum(self):
        path=self.codex_log();rows=[json.loads(line) for line in path.read_text().splitlines()[:-1]]
        rows[2]['payload']['usage']['reasoning_output_tokens']=4;self.write(path,rows)
        self.index.last_scan=0;d=self.index.live_snapshot(provider='codex',now=datetime.fromtimestamp(T+30,timezone.utc))
        self.assertEqual(d['active_count'],1);self.assertEqual(d['threads'][0]['reasoning_coverage'],'full');self.assertEqual(d['threads'][0]['reasoning_output_tokens'],4)

    def test_v2_terminal_compaction_usage_warns_without_guessing_time(self):
        p,c=self.db(True)
        for seq,status in enumerate(('completed','failed','running')):
            data={'status':status,'tokens':self.octokens(),'time':{'created':T*1000}}
            c.execute('INSERT INTO session_message VALUES(?,?,?,?,?,?,?)',(str(seq),'same','compaction',seq,T*1000,T*1000,json.dumps(data)))
        c.execute('INSERT INTO session_message VALUES(?,?,?,?,?,?,?)',('no-usage','same','compaction',4,T*1000,T*1000,json.dumps({'status':'completed','time':{'created':T*1000}})))
        c.execute('INSERT INTO session_message VALUES(?,?,?,?,?,?,?)',('human-token','same','user',5,T*1000,T*1000,json.dumps({'tokens':self.octokens(),'time':{'created':T*1000,'streamed':T*1000}})))
        c.commit();c.close();d=self.snapshot(provider='opencode');self.assertEqual(d['summary']['total_tokens'],0)
        self.assertEqual(sum('压缩请求' in w for w in d['source']['warnings']),1)
        c=sqlite3.connect(p);c.execute("DELETE FROM session_message WHERE id='0'");c.commit();c.close()
        self.assertTrue(any('压缩请求' in w for w in self.snapshot()['source']['warnings']))
        c=sqlite3.connect(p);c.execute("DELETE FROM session_message WHERE id='1'");c.commit();c.close()
        self.assertFalse(any('压缩请求' in w for w in self.snapshot()['source']['warnings']))

    def test_v2_unmeasured_usage_excludes_only_affected_turn_speed(self):
        p,c=self.db(True)
        rows=[('user',{}),('assistant',{'tokens':self.octokens(),'time':{'streamed':T*1000}}),('compaction',{'status':'completed','tokens':self.octokens()}),('idle',{'outcome':'succeeded'}),('user',{}),('assistant',{'tokens':self.octokens(),'time':{'streamed':(T+5)*1000}}),('assistant',{'tokens':self.octokens()}),('idle',{'outcome':'succeeded'}),('user',{}),('assistant',{'tokens':self.octokens(),'time':{'streamed':(T+9)*1000}}),('idle',{'outcome':'succeeded'})]
        for seq,(typ,data) in enumerate(rows):
            c.execute('INSERT INTO session_message VALUES(?,?,?,?,?,?,?)',(str(seq),'same',typ,seq,(T+seq)*1000,(T+seq)*1000,json.dumps(data)))
        c.commit();c.close();d=self.snapshot(provider='opencode');s=d['summary']
        self.assertEqual(s['total_tokens'],135);self.assertEqual(s['speed_samples'],1);self.assertEqual(s['speed_output_tokens'],25);self.assertEqual(s['active_seconds'],2);self.assertEqual(s['average_turn_tps'],12.5)
        self.assertEqual(len(d['sessions'][0]['turns']),1)

    def test_v2_invalid_usage_isolated_to_its_current_turn(self):
        p,c=self.db(True);bad={'input':'invalid'};zero={'input':0,'output':0,'reasoning':0,'cache':{'read':0,'write':0}}
        rows=[('assistant',{'tokens':bad}),('user',{}),('assistant',{'tokens':self.octokens(),'time':{'streamed':(T+2)*1000}}),('assistant',{'tokens':bad}),('idle',{'outcome':'succeeded'}),('user',{}),('assistant',{'tokens':self.octokens(),'time':{'streamed':(T+6)*1000}}),('compaction',{'status':'failed','tokens':bad}),('idle',{'outcome':'succeeded'}),('assistant',{'tokens':bad}),('user',{}),('assistant',{'tokens':zero,'time':{'streamed':(T+11)*1000}}),('user',{'tokens':bad}),('system',{'tokens':bad}),('assistant',{'tokens':self.octokens(),'time':{'streamed':(T+14)*1000}}),('idle',{'outcome':'succeeded'})]
        for seq,(typ,data) in enumerate(rows):c.execute('INSERT INTO session_message VALUES(?,?,?,?,?,?,?)',(str(seq),'same',typ,seq,(T+seq)*1000,(T+seq)*1000,json.dumps(data)))
        c.commit();c.close();d=self.snapshot(provider='opencode');s=d['summary']
        self.assertEqual(s['request_count'],3);self.assertEqual(s['total_tokens'],135);self.assertEqual(s['speed_samples'],1);self.assertEqual(s['speed_output_tokens'],25);self.assertEqual(s['active_seconds'],5);self.assertEqual(s['average_turn_tps'],5)

if __name__=='__main__':unittest.main()
