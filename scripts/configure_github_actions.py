"""Import existing local configuration into GitHub Actions without logging values.

Default is a names-only plan. --apply requires an authenticated GitHub CLI with
repository Variables/Secrets write permissions. Does not dispatch any workflow.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trendradar.content_pool.runtime import load_env

SECRET_NAMES = (
    'SUPABASE_DATABASE_URL', 'AI_API_KEY', 'EMAIL_FROM', 'EMAIL_TO',
    'EMAIL_PASSWORD', 'XIAOHONGSHU_COOKIE', 'ZHIHU_COOKIES', 'TWITTER_AUTH_TOKEN',
)


def values(root=ROOT):
    for filename in ('config/supabase.local.env', 'config/ai.local.env',
                     'config/email.local.env', 'docker/rsshub.env'):
        load_env(root / filename)
    secrets = {key: os.environ[key] for key in SECRET_NAMES if os.environ.get(key)}
    ai = yaml.safe_load((root / 'config/config.yaml').read_text()).get('ai', {})
    for key, fallback in (('AI_API_KEY', 'api_key'),):
        if not secrets.get(key) and ai.get(fallback):
            secrets[key] = str(ai[fallback])
    variables = {
        'CONTENT_SCHEDULE_ENABLED': 'false',
        'CONTENT_SEND_ENABLED': 'false',
        'LEGACY_CRAWLER_ENABLED': 'false',
        'CONTENT_SOURCES_CONFIG': 'config/content_sources.actions.yaml',
        'AI_MODEL': os.environ.get('AI_MODEL') or str(ai.get('model', '')),
        'AI_API_BASE': os.environ.get('AI_API_BASE') or str(ai.get('api_base', '')),
        'EMAIL_SMTP_SERVER': os.environ.get('EMAIL_SMTP_SERVER', ''),
        'EMAIL_SMTP_PORT': os.environ.get('EMAIL_SMTP_PORT') or '465',
    }
    base = variables['AI_API_BASE']
    if base and (urlsplit(base).username or urlsplit(base).password or urlsplit(base).query):
        raise ValueError('ai_endpoint_contains_credentials_cannot_store_as_variable')
    werss = os.environ.get('WERSS_BASE_URL', '')
    if werss:
        url = urlsplit(werss)
        if url.scheme != 'https' or not url.hostname or url.hostname in ('localhost', '127.0.0.1') or url.username or url.query or url.fragment:
            raise ValueError('external_werss_https_base_required')
        secrets['WERSS_BASE_URL'] = werss
    return variables, secrets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='Luoyu126/TrendRadar')
    parser.add_argument('--gh', default='gh', help='Path to authenticated GitHub CLI')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--variables-only', action='store_true', help='Configure nonsecret Variables only; do not upload Secrets')
    args = parser.parse_args()
    variables, secrets = values()
    if args.variables_only:
        secrets = {}
    missing = sorted(set(SECRET_NAMES) - secrets.keys())
    if 'WERSS_BASE_URL' not in secrets:
        missing.append('WERSS_BASE_URL (external deployment required)')
    print(json.dumps({'variables':sorted(variables), 'secrets':sorted(secrets),
                      'missing':missing, 'apply':args.apply}))
    if not args.apply:
        return 0

    def gh(*argv, value=None):
        result = subprocess.run([args.gh, *argv], input=value, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise RuntimeError('github_configuration_request_failed:' + argv[0])
        return result.stdout

    gh('auth', 'status', '--hostname', 'github.com')
    # Close safety gates before uploading any working SMTP credentials.
    for key, value in variables.items():
        gh('variable', 'set', key, '--repo', args.repo, value=value)
        print('variable configured: ' + key, flush=True)
    for key, value in secrets.items():
        gh('secret', 'set', key, '--repo', args.repo, value=value)
        print('secret configured: ' + key, flush=True)
    remote_vars = json.loads(gh('variable', 'list', '--repo', args.repo, '--json', 'name,value'))
    actual = {r['name']:r['value'] for r in remote_vars}
    if any(actual.get(k) != v for k,v in variables.items()):
        raise RuntimeError('variable_verification_failed')
    if secrets:
        remote_secrets = json.loads(gh('secret', 'list', '--repo', args.repo, '--json', 'name'))
        if not secrets.keys() <= {r['name'] for r in remote_secrets}:
            raise RuntimeError('secret_name_verification_failed')
    print('Verified variable values' + (' and secret names; secret contents are not readable.' if secrets else '; no Secrets uploaded.'))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'status':'failed','category':type(exc).__name__}))
        raise SystemExit(1)
