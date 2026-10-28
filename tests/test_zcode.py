"""Synthetic Z Code data only; all data roots are isolated."""
import csv
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from server import export_csv
from usage import UsageIndex

T = 1793016000  # 2026-10-26 12:00 UTC
SECRET = 'PRIVATE_PROMPT_AND_SECRET_SENTINEL'


def fixture(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.executescript('''CREATE TABLE session(id TEXT, parent_id TEXT, title TEXT);
    CREATE TABLE model_usage(id TEXT, session_id TEXT, turn_id TEXT, model_id TEXT, status TEXT, completed_at INTEGER, input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER, cache_creation_input_tokens INTEGER, cache_read_input_tokens INTEGER, provider_total_tokens INTEGER, computed_total_tokens INTEGER, raw_usage_json TEXT);
    CREATE TABLE turn_usage(session_id TEXT, turn_id TEXT, status TEXT, started_at INTEGER, completed_at INTEGER, model_request_count INTEGER, output_tokens INTEGER);
    CREATE TABLE input_history(text TEXT);''')
    c.execute('INSERT INTO session VALUES(?,?,?)', ('s', None, SECRET))
    c.execute('INSERT INTO input_history VALUES(?)', (SECRET,))
    c.execute('INSERT INTO model_usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', ('r1','s','t','glm-4.7','completed',(T+10)*1000,100,20,5,10,30,120,120,SECRET))
    c.execute('INSERT INTO model_usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', ('r2','s','t','glm-4.7','completed',(T+20)*1000,50,10,2,5,15,80,80,SECRET))
    c.execute('INSERT INTO turn_usage VALUES(?,?,?,?,?,?,?)', ('s','t','completed',T*1000,(T+30)*1000,2,30))
    c.commit()
    return c


class ZCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.db = self.root/'zcode'/'cli'/'db'/'db.sqlite'
        self.index = UsageIndex(self.root/'codex','UTC',paseo_home=self.root/'paseo',claude_home=self.root/'claude',opencode_home=self.root/'opencode',zcode_home=self.root/'zcode')
    def snapshot(self, **kw):
        self.index.last_scan = 0
        return self.index.snapshot(days=kw.pop('days','all'),now=datetime.fromtimestamp(T+100,timezone.utc),**kw)
    def test_explicit_db_overrides_home(self):
        path=self.root/'alternate.sqlite'; fixture(path).close()
        index=UsageIndex(self.root/'codex','UTC',paseo_home=self.root/'paseo',claude_home=self.root/'claude',opencode_home=self.root/'opencode',zcode_home=self.root/'empty',zcode_db=path)
        d=index.snapshot(days='all',now=datetime.fromtimestamp(T+100,timezone.utc),provider='zcode')
        self.assertEqual(d['summary']['total_tokens'],200)
    def test_usage_dedup_filters_and_privacy(self):
        fixture(self.db).close(); d = self.snapshot(provider='zcode',model='glm-4.7')
        s=d['summary']; self.assertEqual((s['input_tokens'],s['output_tokens'],s['cached_input_tokens'],s['reasoning_output_tokens']), (170,30,45,7))
        self.assertEqual((s['request_count'],s['speed_samples'],s['active_seconds']), (2,1,30))
        self.assertEqual(d['threads'][0]['provider'],'zcode'); self.assertEqual(self.snapshot(provider='codex')['summary']['total_tokens'],0)
        self.assertEqual(self.snapshot(days='custom',start='2026-10-25',end='2026-10-25',provider='zcode')['summary']['total_tokens'],0)
        for kind in ('sessions','daily','turns'):
            text=export_csv(d,kind).decode('utf-8-sig'); self.assertNotIn(SECRET,text)
            self.assertEqual(list(csv.reader(io.StringIO(text)))[1][-2:],['zcode','all'])
        self.assertNotIn(SECRET,json.dumps(d)); self.assertNotIn(SECRET,json.dumps(list(self.index.cache.values())))
        self.assertEqual(self.index.live_snapshot(provider='zcode',now=datetime.fromtimestamp(T+100,timezone.utc))['threads'],[])
    def test_boundaries_wal_and_explicit_db(self):
        c=fixture(self.db); c.execute('PRAGMA journal_mode=WAL')
        c.execute("UPDATE turn_usage SET status='error'"); c.commit()
        self.assertEqual(self.snapshot(provider='zcode')['summary']['speed_samples'],0)
        c.execute("UPDATE turn_usage SET status='completed', model_request_count=3"); c.commit()
        self.assertEqual(self.snapshot(provider='zcode')['summary']['speed_samples'],0)
        c.execute("UPDATE turn_usage SET model_request_count=2"); c.commit()
        self.assertEqual(self.snapshot(provider='zcode')['summary']['speed_samples'],1)
        self.index.zcode_db=self.root/'missing'; self.assertEqual(self.snapshot(provider='zcode')['summary']['total_tokens'],0)
        self.index.zcode_db=self.db; self.assertEqual(self.snapshot(provider='zcode')['summary']['total_tokens'],200); c.close()
    def test_modern_input_includes_cache(self):
        c=fixture(self.db)
        c.execute("DELETE FROM model_usage WHERE id='r2'")
        c.execute("UPDATE model_usage SET input_tokens=95, output_tokens=5, cache_creation_input_tokens=0, cache_read_input_tokens=40, provider_total_tokens=100, computed_total_tokens=100 WHERE id='r1'")
        c.execute("UPDATE turn_usage SET model_request_count=1, output_tokens=5"); c.commit(); c.close()
        summary=self.snapshot(provider='zcode')['summary']
        self.assertEqual((summary['input_tokens'],summary['output_tokens'],summary['cached_input_tokens']), (95,5,40))
    def test_failed_cancelled_and_incomplete_turns(self):
        c=fixture(self.db)
        c.execute("UPDATE model_usage SET status='error' WHERE id='r1'")
        c.execute("UPDATE model_usage SET status='cancelled' WHERE id='r2'")
        c.execute("UPDATE turn_usage SET status='error'"); c.commit()
        d=self.snapshot(provider='zcode'); self.assertEqual(d['summary']['total_tokens'],200); self.assertEqual(d['summary']['speed_samples'],0)
        c.execute("UPDATE turn_usage SET status='completed'"); c.execute("DELETE FROM model_usage WHERE id='r2'"); c.commit()
        d=self.snapshot(provider='zcode'); self.assertEqual(d['summary']['total_tokens'],120); self.assertEqual(d['summary']['speed_samples'],0); c.close()
    def test_mixed_model_turn_is_not_used_for_filtered_speed(self):
        c=fixture(self.db); c.execute("UPDATE model_usage SET model_id='other' WHERE id='r2'"); c.commit(); c.close()
        d=self.snapshot(provider='zcode',model='glm-4.7')
        self.assertEqual(d['summary']['total_tokens'],120); self.assertEqual(d['summary']['speed_samples'],0)
    def test_invalid_zcode_preserves_codex_usage(self):
        log=self.root/'codex'/'sessions'/'s.jsonl'; log.parent.mkdir(parents=True)
        rows=[{'type':'session_meta','timestamp':T,'payload':{'id':'s'}},{'type':'token_usage_record','timestamp':T+3,'payload':{'thread_id':'s','response_id':'r','turn_id':'t','model':'glm','usage':{'input_tokens':3,'output_tokens':7}}}]
        log.write_text(''.join(json.dumps(row)+'\n' for row in rows)); self.db.parent.mkdir(parents=True); self.db.write_bytes(b'invalid')
        d=self.snapshot(); self.assertEqual(d['summary']['total_tokens'],10); self.assertEqual(d['threads'][0]['provider'],'codex'); self.assertTrue(d['source']['warnings'])
    def test_bad_database_does_not_break_other_provider(self):
        self.db.parent.mkdir(parents=True); self.db.write_bytes(b'invalid')
        d=self.snapshot(); self.assertTrue(d['source']['warnings']); self.assertEqual(d['summary']['total_tokens'],0)
        self.db.unlink(); c=sqlite3.connect(self.db); c.execute('CREATE TABLE model_usage(wrong TEXT)'); c.close()
        self.assertTrue(self.snapshot()['source']['warnings'])
