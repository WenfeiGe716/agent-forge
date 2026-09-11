"""Compare the configured gateway's live /models response with its saved catalog.

Read-only by default; --pull explicitly refreshes catalog metadata, never model
configuration, Agent bindings or inference. Credentials are never printed.
"""
import argparse
import json
import sqlite3
from pathlib import Path

import httpx
from cryptography.fernet import Fernet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider-url', required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:8787')
    parser.add_argument('--pull', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / '.local/config.json').read_text())
    path = config['DATABASE_URL'].removeprefix('sqlite:///')
    with sqlite3.connect('file:' + path + '?mode=ro', uri=True) as db:
        rows = db.execute('select p.id,r.config,r.encrypted_key from providers p join provider_revisions r on r.id=p.revision_id').fetchall()
    row = next(r for r in rows if json.loads(r[1])['base_url'] == args.provider_url)
    connection = json.loads(row[1])
    if connection['protocol'] not in ('openai_chat', 'openai_responses'):
        raise SystemExit('This comparison checks OpenAI-compatible gateway model lists only.')
    key = Fernet(config['ENCRYPTION_KEY'].encode()).decrypt(row[2].encode()).decode() if row[2] else ''
    with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as upstream:
        result = upstream.get(connection['base_url'] + '/models', headers={'Authorization':'Bearer ' + key, 'Accept':'application/json'})
    key = ''
    if result.status_code != 200:
        raise SystemExit('Upstream catalog HTTP ' + str(result.status_code))
    data = result.json()
    model_ids = {item['id'] for item in data['data']}
    print(json.dumps({'upstream_count':len(model_ids),'response_keys':list(data),
        'pagination':{k:data[k] for k in ('has_more','last_id','total','next','next_page') if k in data},
        'cache_headers':{k:result.headers[k] for k in ('age','cache-control','x-cache') if k in result.headers}},ensure_ascii=False))
    assert not data.get('has_more'), 'Comparison requires fetching remaining upstream pages.'
    with httpx.Client(base_url=args.base_url,timeout=40,trust_env=False,headers={'X-Forge-Request':'1'}) as app:
        login=app.post('/api/auth/login',json={'username':'admin','password':config['ADMIN_PASSWORD']})
        login.raise_for_status()
        try:
            before={k:app.get('/api/'+k).json() for k in ('providers','models','agents','runs')}
            path='/api/providers/'+row[0]+'/catalog'
            cached=app.post(path+'/sync',json={}) if args.pull else app.get(path)
            cached.raise_for_status(); catalog=cached.json()
            cached_ids={item['id'] for item in catalog['models']}
            print(json.dumps({'cached_count':len(cached_ids),'last_synced_at':catalog['last_synced_at'],
                'missing_upstream_models':sorted(model_ids-cached_ids),'not_in_current_upstream':sorted(cached_ids-model_ids),
                'configured_count':catalog['counts']['configured']},ensure_ascii=False))
            assert all(app.get('/api/'+k).json()==v for k,v in before.items()), 'Pull mutated configuration or runtime.'
            assert cached_ids==model_ids, 'Saved directory differs from current upstream; pull required.'
        finally:
            app.post('/api/auth/logout',json={})


if __name__ == '__main__':
    main()
