"""Opt-in live smoke test. Uses encrypted saved provider; never prints API keys.

Creates one model asset when missing, runs one Benchmark, then leaves AI team and
routing suggestions as unapproved drafts. Does not bind agents or start tasks.
"""
import argparse
import json
import uuid
from pathlib import Path
import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider-url', required=True)
    parser.add_argument('--model-id', required=True)
    parser.add_argument('--paid', action='store_true', help='Explicitly allow up to 8 model requests')
    parser.add_argument('--skip-benchmark', action='store_true', help='Reuse an existing completed benchmark; only test recommendations')
    parser.add_argument('--kind', choices=['routing', 'team', 'both'], default='both')
    parser.add_argument('--base-url', default='http://127.0.0.1:8787')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / '.local/config.json').read_text())
    with httpx.Client(base_url=args.base_url, headers={'X-Forge-Request': '1'}, timeout=300, trust_env=False) as client:
        client.post('/api/auth/login', json={'username': 'admin', 'password': config['ADMIN_PASSWORD']}).raise_for_status()
        provider = next(p for p in client.get('/api/providers').json() if p['base_url'] == args.provider_url)
        response = client.post('/api/model-hub/providers/' + provider['id'] + '/discover', json={})
        response.raise_for_status()
        catalog = response.json()['models']
        if args.model_id not in {m['id'] for m in catalog}:
            raise SystemExit('Selected model absent from discovered provider catalog')
        print(json.dumps({'catalog_count': len(catalog), 'model_id': args.model_id}, ensure_ascii=False), flush=True)
        if not args.paid:
            return
        models = client.get('/api/models').json()
        model = next((m for m in models if m['provider_id'] == provider['id'] and m['model_id'] == args.model_id), None)
        if not model:
            response = client.post('/api/models', json={'name': args.model_id + ' · 联调模型', 'provider_id': provider['id'],
                'model_id': args.model_id, 'allowed_parameters': ['max_tokens'], 'parameters': {'max_tokens': 4096}, 'supports_tools': True})
            response.raise_for_status()
            model = response.json()
        baseline_agents = client.get('/api/agents').json()
        baseline_runs = client.get('/api/runs').json()
        if args.skip_benchmark:
            overview = client.get('/api/model-hub').json()
            benchmark = next((b for b in overview['benchmarks'] if b['model_id'] == model['id']
                and b['model_version'] == model['version'] and b['status'] == 'completed'), None)
            if not benchmark:
                raise SystemExit('No completed benchmark for this exact model version to reuse')
            print('Reusing completed benchmark; no benchmark calls repeated.', flush=True)
        else:
            print('Running explicitly authorized Benchmark (up to 6 model calls)...', flush=True)
            response = client.post('/api/model-hub/models/' + model['id'] + '/benchmark', json={'acknowledge': True},
                                  headers={'Idempotency-Key': str(uuid.uuid4())})
            response.raise_for_status()
            benchmark = response.json()
        print(json.dumps({'benchmark_status': benchmark['status'], 'requests': benchmark['requests'],
            'scores': benchmark['scores'], 'error': benchmark['error']}, ensure_ascii=False), flush=True)
        recommendations = []
        kinds = ('routing', 'team') if args.kind == 'both' else (args.kind,)
        for kind in kinds if benchmark['status'] == 'completed' else ():
            response = client.post('/api/design/recommend', json={'kind': kind, 'request': '设计一个内部商城系统开发方案，包含规划、开发、测试和独立审查。仅生成建议，不执行任务。',
                'model_profile_id': model['id'], 'acknowledge': True, 'allow_external': True, 'allowed_provider_ids': [provider['id']], 'budget': 'medium'},
                headers={'Idempotency-Key': str(uuid.uuid4())})
            data = response.json()
            recommendations.append({'kind': kind, 'http_status': response.status_code, 'id': data.get('id'),
                                    'status': data.get('status'), 'generation': data.get('generation'), 'error': data.get('detail')})
            print(json.dumps(recommendations[-1], ensure_ascii=False), flush=True)
        unchanged = client.get('/api/agents').json() == baseline_agents and client.get('/api/runs').json() == baseline_runs
        report = {'catalog_count': len(catalog), 'model_id': model['model_id'], 'model_profile_id': model['id'],
                  'benchmark': benchmark, 'recommendations': recommendations, 'live_agents_and_tasks_unchanged': unchanged}
        output = json.dumps(report, ensure_ascii=False, indent=2)
        (root / '.local/live-verification-v013.json').write_text(output)
        (root / ('.local/live-verification-v013-' + uuid.uuid4().hex[:8] + '.json')).write_text(output)
        print('Live Agents and tasks unchanged:', unchanged, flush=True)
        if not unchanged or benchmark['status'] != 'completed' or any(r['http_status'] != 201 for r in recommendations):
            raise SystemExit(1)


if __name__ == '__main__':
    main()
