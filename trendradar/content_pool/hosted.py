"""Database-only daily orchestration, shared by CLI and hosted runners."""
from pathlib import Path

from .delivery import deliver
from .digest import Digests
from .llm import Meter
from .pipeline import Processor
from .runtime import configuration, run_lock
from .store import Store, now
from .window import for_date, previous_day


def prepare_schema(db, zone="America/New_York"):
    with db:
        db.execute('''CREATE TABLE IF NOT EXISTS digest_days (
            day TEXT PRIMARY KEY,batch_id TEXT NOT NULL UNIQUE,
            FOREIGN KEY(batch_id) REFERENCES batches(id))''')
        # Existing migrated receipts must protect the first hosted run too.
        if hasattr(db, 'connection'):
            legacy = db.execute("SELECT to_regclass('trendradar.daily_publications')").fetchone()[0]
        else:
            legacy = db.execute("SELECT 1 FROM sqlite_master WHERE name='daily_publications'").fetchone()
        if legacy:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            for row in db.execute("SELECT p.day,p.batch_id FROM daily_publications p JOIN batches b ON b.id=p.batch_id WHERE p.kind='daily'").fetchall():
                clock = datetime.fromisoformat(row['day']).replace(tzinfo=ZoneInfo(zone))
                day = previous_day(clock, zone)['date']
                old = db.execute('SELECT batch_id FROM digest_days WHERE day=?', (day,)).fetchone()
                if old and old[0] != row['batch_id']:
                    raise ValueError('legacy_daily_date_conflict_requires_review')
                db.execute('INSERT INTO digest_days VALUES (?,?) ON CONFLICT(day) DO NOTHING', (day, row['batch_id']))


def run_daily(args, root):
    from scripts.run_content_daily import notice

    cfg, ai, paper = configuration(root, args.config)
    window = for_date(args.date, cfg['timezone']) if args.date else previous_day(now(), cfg['timezone'])
    if cfg.get('sources_config'):
        from scripts.collect_content import load_config
        source_settings = load_config(cfg['sources_config'], args.config)
        if not source_settings.get('wechat', {}).get('enabled', True):
            cfg['excluded_platforms'] = ['wechat']
    cfg = {**cfg, 'daily_window': 'previous_day', 'business_window': window,
           'report_date': window['date']}
    with run_lock(Path(cfg['db_path']).with_suffix('.run.lock')):
        store = Store(cfg['db_path'])
        try:
            prepare_schema(store.db, cfg["timezone"])
            meter = Meter(store, ai, cfg)
            processor = Processor(store, meter, paper)
            processor.database_only = True
            digests = Digests(store, meter, paper, cfg['report_dir'])
            existing = store.db.execute('SELECT batch_id FROM digest_days WHERE day=?', (window['date'],)).fetchone()
            if existing and digests.get(existing[0])['status'] == 'cleaned':
                return {'status': 'already_sent', 'date': window['date'], 'batch_id': existing[0]}
            if not existing:
                processor.process()
            message = notice(store, window['start'], excluded_platforms=cfg.get('excluded_platforms', []))
            if cfg.get('sources_config'):
                from scripts.collect_content import load_config, sources
                expected = {sid for _, sid, _ in sources(load_config(cfg['sources_config'], args.config))}
                if paper['enabled']:
                    expected.add('arxiv')
                attempted = {r[0] for r in store.db.execute('SELECT DISTINCT source_id FROM fetch_runs')}
                if expected - attempted:
                    message += f' {len(expected - attempted)} 个配置来源尚无采集记录。'
            pending = store.db.execute("SELECT count(*) FROM records WHERE reason='ValueError' AND status='pending'").fetchone()[0]
            if pending:
                message += f' {pending} 条内容待核验，保留后续处理。'
            batch_id = existing[0] if existing else digests.prepare(notice=message, allow_empty=True)
            # Rebuild HTML/JSON from DB on every fresh runner. No artifacts needed.
            digests.preview(batch_id, allow_fallback=True)
            if args.send:
                return deliver(digests, batch_id)
            return {'status': 'preview', 'date': window['date'], 'batch_id': batch_id,
                    'preview': str(digests.report_dir / (batch_id + '.html'))}
        finally:
            store.close()
