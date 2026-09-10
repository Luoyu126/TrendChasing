import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

import test_pool_workflow as workflow
from test_pool_workflow import item, paper
from trendradar.content_pool.hosted import prepare_schema, run_daily
from trendradar.content_pool.window import for_date
from trendradar.content_pool.postgres import translate, Row
from trendradar.content_pool.delivery import deliver
from scripts.collect_content import collect, load_config, download


class HostedTests(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown
    ingest = workflow.Workflow.ingest
    count = workflow.Workflow.count

    def configured(self, day='2026-09-09'):
        prepare_schema(self.store.db)
        self.meter.config.update(report_date=day, timezone='America/New_York',
            daily_window='previous_day', business_window=for_date(day, 'America/New_York'))

    def test_date_rerun_and_new_runner_rebuild(self):
        self.configured()
        self.ingest(item())
        self.ingest(item())
        self.processor.process()
        self.assertEqual(self.count('records'), 1)
        b = self.digests.prepare(allow_empty=True)
        self.digests.preview(b)
        for f in (self.root / 'reports').iterdir():
            f.unlink()
        self.assertEqual(self.digests.prepare(allow_empty=True), b)
        self.digests.preview(b)
        self.assertTrue((self.root / 'reports' / (b + '.html')).exists())
        self.assertEqual(self.count('digest_days'), 1)
        self.assertEqual(self.count('deliveries'), 0)

    @patch.dict(os.environ, {'EMAIL_FROM':'sender@example.test', 'EMAIL_TO':'reader@example.test',
        'EMAIL_PASSWORD':'fake', 'EMAIL_SMTP_SERVER':'smtp.example.test'})
    def test_successful_date_never_sends_twice(self):
        self.configured()
        self.ingest(item())
        self.processor.process()
        b = self.digests.prepare(allow_empty=True)
        self.digests.preview(b)
        sender = Mock(return_value=('success', 'receipt'))
        self.assertEqual(deliver(self.digests, b, sender)['status'], 'sent')
        self.assertEqual(self.digests.prepare(allow_empty=True), b)
        self.assertEqual(deliver(self.digests, b, sender)['status'], 'already_cleaned')
        self.assertEqual(sender.call_count, 1)

    @patch.dict(os.environ, {'EMAIL_FROM':'sender@example.test', 'EMAIL_TO':'reader@example.test',
        'EMAIL_PASSWORD':'fake', 'EMAIL_SMTP_SERVER':'smtp.example.test'})
    def test_lost_ack_cannot_resend(self):
        self.configured()
        self.ingest(item())
        self.processor.process()
        b = self.digests.prepare(allow_empty=True)
        self.digests.preview(b)
        sender = Mock(return_value=('success', 'receipt'))
        with patch.object(self.digests, 'acknowledge', side_effect=OSError):
            with self.assertRaises(OSError):
                deliver(self.digests, b, sender)
        sender.reset_mock()
        self.assertEqual(deliver(self.digests, b, sender)['uncertain'], 1)
        sender.assert_not_called()

    def test_database_only_never_discovers_paper(self):
        self.processor.database_only = True
        self.processor.state.save_papers([paper()])
        with patch('requests.get', side_effect=AssertionError('network forbidden')):
            found = self.processor.resolve(item(), {'paper_title':paper().title})
            self.assertEqual(found.canonical_id, paper().canonical_id)
            with self.assertRaisesRegex(ValueError, 'pending_collection'):
                self.processor.resolve(item(), {'paper_title':'missing paper'})

    def test_old_receipts_adopted(self):
        b = self.digests.prepare(allow_empty=True)
        with self.store.db:
            self.store.db.execute('CREATE TABLE daily_publications(batch_id TEXT PRIMARY KEY,day TEXT,kind TEXT)')
            self.store.db.execute("INSERT INTO daily_publications VALUES (?, '2026-09-10', 'daily')", (b,))
        prepare_schema(self.store.db)
        self.assertEqual(self.store.db.execute("SELECT batch_id FROM digest_days WHERE day='2026-09-09'").fetchone()[0], b)

    def test_preview_entry_no_delivery_mutation(self):
        self.configured()
        self.ingest(item())
        self.processor.process()
        b = self.digests.prepare(allow_empty=True)
        self.digests.preview(b)
        self.digests.set_targets(b, {'email:reader@example.test': 1})
        self.digests.acknowledge(b, 'email:reader@example.test', 0, 'sending')
        before = [tuple(r) for r in self.store.db.execute('SELECT * FROM deliveries')]
        cfg = {**self.meter.config, 'db_path':str(self.store.path), 'report_dir':str(self.root / 'reports')}
        args = SimpleNamespace(config='unused', date='2026-09-09', send=False)
        with patch('trendradar.content_pool.hosted.configuration', return_value=(cfg,self.meter.ai,self.processor.cfg)), patch('trendradar.content_pool.hosted.Store', return_value=self.store), patch.object(self.store,'close'), patch('trendradar.content_pool.hosted.deliver', side_effect=AssertionError('SMTP forbidden')), patch('requests.get', side_effect=AssertionError('discovery forbidden')):
            self.assertEqual(run_daily(args, self.root)['status'], 'preview')
        self.assertEqual(before, [tuple(r) for r in self.store.db.execute('SELECT * FROM deliveries')])


class ConfigurationTests(unittest.TestCase):
    def test_dst_windows(self):
        from datetime import datetime
        for day, hours in [('2026-03-08',23), ('2026-11-01',25)]:
            w = for_date(day,'America/New_York')
            self.assertEqual((datetime.fromisoformat(w['end'])-datetime.fromisoformat(w['start'])).total_seconds()/3600,hours)

    def test_workflows_gates_and_custom_rsshub(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        daily = yaml.safe_load((root / '.github/workflows/content-daily.yml').read_text())
        event = daily.get('on', daily.get(True))
        self.assertEqual(event['schedule'], [{'cron':'34 7 * * *','timezone':'America/New_York'}])
        collect_w = yaml.safe_load((root / '.github/workflows/content-collect.yml').read_text())
        self.assertEqual(collect_w['concurrency'], daily['concurrency'])
        self.assertIn('CONTENT_SCHEDULE_ENABLED', daily['jobs']['daily']['if'])
        self.assertEqual(collect_w['jobs']['collect']['steps'][-1]['if'], 'always()')
        compose = yaml.safe_load((root / 'docker/docker-compose.actions.yml').read_text())
        self.assertEqual(compose['services']['rsshub']['build']['context'],'../external/RSSHub')
        sources = load_config(root / 'config/content_sources.actions.yaml')
        self.assertTrue(sources['twitter_accounts'])
        self.assertEqual(len(sources['wechat_feeds']), 3)

    def test_postgres_translation_and_row(self):
        self.assertEqual(translate("SELECT '?' AS literal WHERE id=?"), "SELECT '?' AS literal WHERE id=%s")
        self.assertIn('ON CONFLICT DO NOTHING',translate('INSERT OR IGNORE INTO x VALUES (?)'))
        self.assertEqual(dict(Row((1,'a'),['id','name'])), {'id':1,'name':'a'})

    def test_missing_external_werss_is_recorded_without_network(self):
        from trendradar.content_pool.store import Store
        root = Path(__file__).resolve().parents[1]
        cfg = load_config(root / 'config/content_sources.actions.yaml')
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'WERSS_BASE_URL': ''}):
            store = Store(Path(tmp) / 'pool.db')
            try:
                fetch = Mock(side_effect=AssertionError('unconfigured network'))
                self.assertEqual(collect(store.db,cfg,fetch=fetch,platform='wechat'),3)
                fetch.assert_not_called()
                rows = list(store.db.execute('SELECT status,error FROM fetch_runs'))
                self.assertTrue(all(tuple(r)==('failed','external_werss_not_configured') for r in rows))
            finally:
                store.close()

    def test_feed_retry_bounded(self):
        from urllib.error import URLError
        with patch('scripts.collect_content.urlopen', side_effect=URLError('offline')) as get, patch('scripts.collect_content.time.sleep'):
            with self.assertRaises(URLError):
                download('https://example.test/feed', 2)
            self.assertEqual(get.call_count,3)

class BudgetRecoveryTests(unittest.TestCase):
    def test_daily_budget_survives_restart(self):
        from trendradar.content_pool.store import Store
        from trendradar.content_pool.llm import Meter, BudgetExceeded
        from test_llm_pacing import Client
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'db')
            try:
                cfg = {'report_date':'2026-09-09', 'run_call_budget':1}
                Meter(store, {}, cfg, Client).chat('test', [])
                resumed = Meter(store, {}, cfg, Client)
                with self.assertRaises(BudgetExceeded):
                    resumed.chat('test', [])
            finally:
                store.close()
