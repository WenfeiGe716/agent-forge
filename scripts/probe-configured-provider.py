#!/usr/bin/env python3
"""Explicit live inference probe using one saved provider; never prints credentials."""
import argparse
import json
from pathlib import Path
import sqlite3
import time

import httpx
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]


def configured_provider(name, database=None, config=None):
    local = json.loads(Path(config or ROOT / '.local/config.json').read_text())
    with sqlite3.connect(f"file:{database or ROOT / '.local/product.sqlite3'}?mode=ro", uri=True) as db:
        row = db.execute('SELECT p.id,r.config,r.encrypted_key FROM providers p JOIN provider_revisions r ON r.id=p.revision_id WHERE p.name=? AND p.enabled=1', (name,)).fetchone()
    if row is None:
        raise RuntimeError('Enabled provider not found')
    provider = json.loads(row[1])
    key = Fernet(local['ENCRYPTION_KEY'].encode()).decrypt(row[2].encode()).decode()
    return row[0], provider, key


def probe(name, model, database=None, config=None):
    provider_id, provider, key = configured_provider(name, database, config)
    if provider['protocol'] != 'openai_responses':
        raise RuntimeError('This probe requires the configured Responses protocol')
    headers = dict(provider.get('protocol_config', {}).get('headers', {}))
    headers.update(Authorization='Bearer ' + key, Accept='text/event-stream')
    tool = {'type': 'function', 'name': 'verify_sum', 'description': 'Verify arithmetic with the local host.',
            'parameters': {'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'integer'}}, 'required': ['a', 'b'], 'additionalProperties': False}}
    inputs = [{'role': 'user', 'content': 'Call verify_sum with a=19 and b=23, then report the returned result.'}]
    report = {'provider': name, 'provider_id': provider_id, 'model': model, 'protocol': provider['protocol'], 'real_provider_used': True, 'requests': []}
    with httpx.Client(timeout=httpx.Timeout(90, connect=20), trust_env=False, follow_redirects=False) as client:
        for step in range(2):
            payload = {'model': model, 'input': inputs, 'tools': [tool], 'stream': True, 'max_output_tokens': 2048}
            if step == 0:
                payload['tool_choice'] = {'type': 'function', 'name': 'verify_sum'}
            started = time.monotonic()
            completed = None
            events = []
            try:
                with client.stream('POST', provider['base_url'].rstrip('/') + '/responses', headers=headers, json=payload) as response:
                    status = response.status_code
                    if status != 200:
                        # Retain only the upstream's structured error classification, never its raw body.
                        try:
                            error = json.loads(response.read()).get('error', {})
                            kind = {k: error.get(k) for k in ['type', 'code']}
                        except (ValueError, AttributeError):
                            kind = {'type': 'unstructured_upstream_error'}
                        report['requests'].append({'http_status': status, 'seconds': round(time.monotonic() - started, 2), 'error': kind, 'request_id': response.headers.get('x-request-id')})
                        break
                    for line in response.iter_lines():
                        if not line.startswith('data: ') or line == 'data: [DONE]':
                            continue
                        event = json.loads(line[6:])
                        events.append(event.get('type'))
                        if event.get('type') == 'response.completed':
                            completed = event['response']
                report['requests'].append({'http_status': status, 'seconds': round(time.monotonic() - started, 2), 'completed': completed is not None, 'event_types': sorted(set(x for x in events if x)), 'usage': (completed or {}).get('usage')})
            except httpx.HTTPError as error:
                report['requests'].append({'error': type(error).__name__, 'seconds': round(time.monotonic() - started, 2)})
                break
            if completed is None:
                break
            outputs = completed.get('output', [])
            if step == 0:
                calls = [item for item in outputs if item.get('type') == 'function_call' and item.get('name') == 'verify_sum']
                if len(calls) != 1 or json.loads(calls[0]['arguments']) != {'a': 19, 'b': 23}:
                    break
                report['tool_call_verified'] = True
                inputs += outputs + [{'type': 'function_call_output', 'call_id': calls[0]['call_id'], 'output': '{"result":42}'}]
            else:
                output_text = ' '.join(part.get('text', '') for item in outputs for part in item.get('content', []))
                report['tool_result_verified'] = '42' in output_text
    report['ok'] = report.get('tool_call_verified', False) and report.get('tool_result_verified', False)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', default='智蔻')
    parser.add_argument('--model', required=True)
    parser.add_argument('--database', type=Path, help='Saved product database to read, defaults to .local/product.sqlite3')
    parser.add_argument('--config', type=Path, help='Local configuration holding that database encryption key')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = probe(args.provider, args.model, args.database, args.config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['ok'] else 1)
