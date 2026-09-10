"""Opt-in local PG tests. TEST_POSTGRES_DSN must point at a disposable cluster.
Creates/drops a uniquely named database; never reads SUPABASE_DATABASE_URL.
"""
import os
import unittest
import uuid
from unittest.mock import patch

import test_hosted
from trendradar.content_pool.postgres import Database, row_factory, LOCK_ID
from trendradar.content_pool.store import SCHEMA, Store
from trendradar.content_pool.supabase_sync import postgres_ddl


@unittest.skipUnless(os.environ.get('TEST_POSTGRES_DSN'), 'local PostgreSQL DSN not supplied')
class PostgresTests(test_hosted.HostedTests):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.conninfo import make_conninfo
        cls.connect = staticmethod(psycopg.connect)
        cls.admin_dsn = os.environ['TEST_POSTGRES_DSN']
        cls.name = 'trendradar_test_' + uuid.uuid4().hex
        with cls.connect(cls.admin_dsn, autocommit=True) as db:
            db.execute('CREATE DATABASE ' + cls.name)
        cls.dsn = make_conninfo(cls.admin_dsn, dbname=cls.name)

    @classmethod
    def tearDownClass(cls):
        with cls.connect(cls.admin_dsn, autocommit=True) as db:
            db.execute('DROP DATABASE ' + cls.name + ' WITH (FORCE)')

    def setUp(self):
        with self.connect(self.dsn, autocommit=True) as db:
            db.execute('DROP SCHEMA IF EXISTS trendradar CASCADE')
            db.execute('CREATE SCHEMA trendradar')
            db.execute('SET search_path=trendradar,pg_catalog')
            for sql in SCHEMA.split(';'):
                if sql.strip():
                    db.execute(postgres_ddl(sql))
        # Local cluster has no TLS; only connection transport is substituted.
        # Runtime initialization, SQL, advisory locks, transactions are real PG.
        def connect_local(uri, **kwargs):
            kwargs.pop('sslmode',None)
            kwargs.pop('sslrootcert',None)
            return self.connect(self.dsn, **kwargs)
        self.env = patch.dict(os.environ, {'CONTENT_DATABASE_BACKEND':'supabase',
            'SUPABASE_DATABASE_URL':'postgresql://localhost:5432/test'})
        self.connpatch = patch('psycopg.connect', side_effect=connect_local)
        self.env.start()
        self.connpatch.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.connpatch.stop)
        super().setUp()

    def test_connection_lock_and_recovery(self):
        with self.assertRaisesRegex(RuntimeError,'another_content_run'):
            Store(self.store.path)
        self.store.cache_put('persist', 'test', {'value':1})
        self.store.close()
        fresh = Store(self.store.path)
        self.assertEqual(fresh.cache_get('persist'), {'value':1})
        self.store = fresh
        # Sequence-generated fetch IDs and named placeholders.
        from scripts.collect_content import save_items
        from test_pool_workflow import item
        with fresh.db:
            first = fresh.db.execute("INSERT INTO fetch_runs(source_id,started_at,status) VALUES ('test','now','running')").lastrowid
            save_items(fresh.db, 'test', [item()], '2026-09-09T00:00:00+00:00')
        self.assertGreater(first,0)
        with fresh.db:
            save_items(fresh.db, 'test', [item()], '2026-09-09T00:00:00+00:00')
        self.assertEqual(fresh.db.execute('SELECT count(*) FROM records').fetchone()[0],1)

    def test_paper_ingestion_and_limits(self):
        from trendradar.content_pool.paper_ingest import ingest_papers
        from test_pool_workflow import paper
        with self.store.db:
            ingest_papers(self.store.db,[paper()])
        self.processor.process()
        self.assertEqual(self.count('papers'),1)
        self.assertEqual(self.count('judgments'),1)
        self.meter.defer(2)
        self.meter.defer(1)
        self.assertEqual(self.count('llm_limits'),1)

    def test_transaction_rollback(self):
        with self.assertRaises(ValueError):
            with self.store.db:
                self.store.db.execute("INSERT INTO analysis_cache VALUES ('rollback','test','{}','now')")
                raise ValueError('rollback')
        self.assertIsNone(self.store.cache_get('rollback'))

    @patch.dict(os.environ, {'EMAIL_FROM':'sender@example.test','EMAIL_TO':'reader@example.test',
        'EMAIL_PASSWORD':'fake','EMAIL_SMTP_SERVER':'smtp.example.test'})
    def test_fresh_runner_other_directory_can_deliver(self):
        from test_pool_workflow import item
        from trendradar.content_pool.digest import Digests
        from trendradar.content_pool.llm import Meter
        from trendradar.content_pool.delivery import deliver
        self.configured()
        self.ingest(item())
        self.processor.process()
        b = self.digests.prepare(allow_empty=True)
        self.digests.preview(b)
        self.store.close()
        self.store = Store(self.root / 'new-runner' / 'pool.db')
        meter = Meter(self.store,self.meter.ai,self.meter.config)
        digests = Digests(self.store,meter,self.processor.cfg,self.root / 'fresh-reports')
        digests.preview(b)
        self.assertEqual(deliver(digests,b,lambda *_:('success','receipt'))['status'],'sent')
