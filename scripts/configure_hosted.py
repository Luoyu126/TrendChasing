"""Materialize nonsecret runner config, retaining all research and scoring rules."""
import os
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]


def main():
    if os.environ.get('CONTENT_DATABASE_BACKEND') != 'supabase':
        raise ValueError('hosted_requires_supabase')
    if not os.environ.get('SUPABASE_DATABASE_URL'):
        raise ValueError('missing_database_url')
    cfg = yaml.safe_load((ROOT / 'config/content_pool.yaml').read_text())
    cfg['sources_config'] = os.environ.get('CONTENT_SOURCES_CONFIG') or 'config/content_sources.actions.yaml'
    cfg['timezone'] = 'America/New_York'
    cfg['daily_window'] = 'previous_day'
    destination = ROOT / 'output/hosted-config.yaml'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(yaml.safe_dump(cfg, allow_unicode=True))


if __name__ == '__main__':
    main()
