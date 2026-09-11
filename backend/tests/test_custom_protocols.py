import asyncio
import copy
import json

import httpx
import pytest

from forge import protocols, providers
from forge.db import ProviderRevision, session
from forge.security import decrypt
from forge.snapshots import freeze_model


CONFIG = {
    'endpoint': '/generate',
    'auth': {'header': 'X-Company-Token', 'value': 'Token {{api_key}}'},
    'body': {'deployment': '{{model}}', 'history': '{{messages}}', 'functions': '{{tools}}', 'options': '{{parameters}}'},
    'supported_parameters': ['creativity', 'output_limit'],
    'parameter_schema': {'creativity': {'type': 'number', 'minimum': 0, 'maximum': 3}, 'output_limit': {'type': 'integer', 'minimum': 1, 'required': True}},
    'message_templates': {
        'system': {'speaker': 'policy', 'text': '{{content}}'},
        'user': {'speaker': 'human', 'text': '{{content}}'},
        'assistant': {'speaker': 'model', 'text': '{{content}}', 'actions': '{{calls}}'},
        'tool': {'speaker': 'function', 'ref': '{{call_id}}', 'value': '{{content}}'},
    },
    'call_template': {'ref': '{{id}}', 'action': '{{name}}', 'payload': '{{arguments}}'},
    'tool_template': {'action': '{{name}}', 'info': '{{description}}', 'input': '{{parameters}}'},
    'response': {'text': '/result/answer', 'calls': '/result/actions', 'call_fields': {'id': '/ref', 'name': '/action', 'arguments': '/payload'}, 'input_tokens': '/meter/in', 'output_tokens': '/meter/out'},
    'discovery': {'endpoint': '/catalog', 'items': '/entries', 'id': '/identifier', 'name': '/label'},
}
MODEL = {'model_id': 'private-language-v2', 'allowed_parameters': ['creativity', 'output_limit'], 'parameters': {'creativity': 2.5, 'output_limit': 100}}
TOOL = {'name': 'read_file', 'description': 'Read a file', 'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}
KEY = 'synthetic-protocol-test-key'


def connection(config=None, **overrides):
    return {'protocol': 'custom_http', 'protocol_config': copy.deepcopy(config or CONFIG), 'base_url': 'https://private.example/v2', 'external': True, **overrides}


def invoke(conn, model, messages, tools, handler):
    return asyncio.run(providers.complete(conn, model, messages, tools, KEY, 10, httpx.MockTransport(handler)))


def test_custom_json_protocol_complete_tool_roundtrip_preserves_typed_values():
    history = [{'role': 'system', 'content': 'Use tools'}, {'role': 'user', 'content': 'Read code'}]
    def first(request):
        assert str(request.url) == 'https://private.example/v2/generate'
        assert request.headers['x-company-token'] == 'Token ' + KEY
        body = json.loads(request.content)
        assert KEY not in request.content.decode()
        assert body['options'] == MODEL['parameters']
        assert body['deployment'] == MODEL['model_id']
        assert body['history'][0] == {'speaker': 'policy', 'text': 'Use tools'}
        assert body['functions'][0]['input'] == TOOL['parameters']
        return httpx.Response(200, json={'result': {'answer': None, 'actions': [{'ref': 'r1', 'action': 'read_file', 'payload': '{"path":"main.py"}'}]}, 'meter': {'in': 42, 'out': 9}})
    reply = invoke(connection(), MODEL, history, [TOOL], first)
    assert reply.calls == [{'id': 'r1', 'name': 'read_file', 'arguments': {'path': 'main.py'}}]
    assert (reply.input_tokens, reply.output_tokens) == (42, 9)
    history += [{'role': 'assistant', 'content': reply.text, 'calls': reply.calls}, {'role': 'tool', 'call_id': 'r1', 'content': 'file contents'}]
    def second(request):
        body = json.loads(request.content)
        assert body['history'][2]['actions'][0] == {'ref': 'r1', 'action': 'read_file', 'payload': {'path': 'main.py'}}
        assert body['history'][3] == {'speaker': 'function', 'ref': 'r1', 'value': 'file contents'}
        return httpx.Response(200, json={'result': {'answer': 'Reviewed', 'actions': []}, 'meter': {'in': 55, 'out': 3}})
    assert invoke(connection(), MODEL, history, [TOOL], second).text == 'Reviewed'


@pytest.mark.parametrize('endpoint', ['https://evil.example/go', '//evil.example/go', '/a/../go', '/%2e%2e/go', '/%252e%252e/go', '/go?api_key=x', '/go#fragment', '/a\\b', '/go\nHost:evil'])
def test_custom_endpoint_cannot_redirect_credentials(endpoint):
    config = copy.deepcopy(CONFIG); config['endpoint'] = endpoint
    with pytest.raises(ValueError): protocols.parse_config(config)


@pytest.mark.parametrize('patch', [
    {'headers': {'Authorization': 'literal-secret'}},
    {'headers': {'Host': 'evil.example'}},
    {'auth': {'header': 'Authorization', 'value': 'literal-secret'}},
    {'auth': {'header': 'Authorization', 'value': '{{api_key}}\r\nX-Evil:1'}},
    {'body': {'model': '{{model}}', 'messages': '{{messages}}', 'key': '{{api_key}}'}},
    {'body': {'model': '{{model}}', 'messages': '{{messages}}', 'code': '{{__import__}}'}},
    {'response': {'text': '$.result'}},
])
def test_invalid_configs_rejected_before_persistence(patch):
    with pytest.raises(ValueError): protocols.parse_config(copy.deepcopy(CONFIG) | patch)


def test_no_auth_and_root_parameter_placement_are_preserved():
    config = copy.deepcopy(CONFIG)
    config.update(auth=None, parameter_placement='root')
    config['body'].pop('options')
    normalized = protocols.validate_connection('custom_http', 'https://example.test', True, config)
    assert normalized['auth'] is None
    def handler(request):
        body = json.loads(request.content)
        assert body['output_limit'] == 100 and 'x-company-token' not in request.headers and 'authorization' not in request.headers
        return httpx.Response(200, json={'result': {'answer': 'OK'}})
    assert invoke(connection(normalized), MODEL, [{'role': 'user', 'content': 'hello'}], [], handler).text == 'OK'


def test_custom_schema_validates_model_and_agent_parameters():
    providers.validate_parameters('custom_http', MODEL['allowed_parameters'], MODEL['parameters'], CONFIG)
    for parameters in [{'output_limit': True}, {'output_limit': 0}, {'output_limit': 100, 'creativity': 4}, {'output_limit': 1, 'temperature': 1}, {'creativity': 1}]:
        with pytest.raises(ValueError): providers.validate_parameters('custom_http', MODEL['allowed_parameters'], parameters, CONFIG)
    with pytest.raises(ValueError): providers.validate_parameters('custom_http', ['max_tokens'], {'max_tokens': 1}, CONFIG)


def test_custom_configuration_revisions_freeze_protocol_and_never_reveal_secret(client):
    payload = {'name': 'Private adapter', 'protocol': 'custom_http', 'base_url': 'http://custom.internal/v1', 'external': False, 'protocol_config': CONFIG, 'api_key': KEY}
    response = client.post('/api/providers', json=payload)
    assert response.status_code == 201, response.text
    pid, rid = response.json()['id'], response.json()['revision_id']
    model = client.post('/api/models', json={'name': 'Private model', 'provider_id': pid, **MODEL, 'input_cost_per_million': 1.2, 'output_cost_per_million': 4.5, 'context_window': 32768})
    assert model.status_code == 201, model.text
    mid = model.json()['id']
    with session() as s:
        frozen = freeze_model(s, mid, {}, False)
        rev = s.get(ProviderRevision, rid)
        assert decrypt(rev.encrypted_key) == KEY
        assert KEY not in json.dumps(rev.config)
    payload['protocol_config'] = copy.deepcopy(CONFIG)
    payload['protocol_config']['endpoint'] = '/generate-v2'
    payload.pop('api_key')
    edited = client.put('/api/providers/' + pid, json=payload)
    assert edited.status_code == 200, edited.text
    assert edited.json()['revision_id'] != rid
    assert frozen['connection']['protocol_config']['endpoint'] == '/generate'
    assert frozen['model']['input_cost_per_million'] == 1.2
    assert KEY not in client.get('/api/providers').text
    assert client.post('/api/models', json={'name': 'Bad overrides', 'provider_id': pid, **MODEL, 'parameters': {'output_limit': 0}}).status_code == 422


def test_external_custom_http_rejected_and_text_only_model_cannot_claim_tools(client):
    payload = {'name': 'External', 'protocol': 'custom_http', 'base_url': 'http://example.test/v1', 'protocol_config': CONFIG}
    assert client.post('/api/providers', json=payload).status_code == 422
    config = copy.deepcopy(CONFIG); config['response'] = {'text': '/answer'}
    payload.update(base_url='https://example.test/v1', protocol_config=config)
    p = client.post('/api/providers', json=payload)
    assert p.status_code == 201, p.text
    body = {'name': 'Text', 'provider_id': p.json()['id'], **MODEL}
    assert client.post('/api/models', json=body).status_code == 422
    assert client.post('/api/models', json=body | {'supports_tools': False}).status_code == 201


@pytest.mark.parametrize('result', [
    {'result': {'answer': {'not': 'text'}}},
    {'result': {'answer': 'x'}, 'meter': {'in': -1}},
    {'result': {'actions': [{'ref': 'x', 'action': 'read_file', 'payload': '[]'}]}},
    {'result': {'actions': [{'ref': 'x', 'action': 'read_file', 'payload': {}}, {'ref': 'x', 'action': 'read_file', 'payload': {}}]}},
    {'unexpected': 'shape'},
])
def test_custom_response_validation_is_safe_and_explicit(result):
    with pytest.raises(providers.ProviderError, match='消息格式'):
        invoke(connection(), MODEL, [{'role': 'user', 'content': 'hello'}], [], lambda r: httpx.Response(200, json=result))


def test_custom_transport_size_and_redirect_boundaries():
    config = copy.deepcopy(CONFIG); config['max_response_bytes'] = 1024
    with pytest.raises(providers.ProviderError, match='安全上限'):
        invoke(connection(config), MODEL, [], [], lambda r: httpx.Response(200, content=b'x' * 1025))
    requests = []
    def redirect(request):
        requests.append(request)
        return httpx.Response(302, headers={'location': 'https://evil.test/stolen'}, json={'echo': KEY})
    with pytest.raises(providers.ProviderError) as error: invoke(connection(), MODEL, [], [], redirect)
    assert len(requests) == 1 and KEY not in str(error.value)


def test_custom_discovery_maps_catalog_without_model_call():
    def handler(request):
        assert request.method == 'GET' and str(request.url) == 'https://private.example/v2/catalog'
        assert request.headers['x-company-token'] == 'Token ' + KEY
        return httpx.Response(200, json={'entries': [{'identifier': 'corp-model', 'label': '内部模型'}]})
    result = asyncio.run(providers.discover(connection(), KEY, transport=httpx.MockTransport(handler)))
    assert result == [{'id': 'corp-model', 'name': '内部模型'}]


def test_openai_responses_preserves_reasoning_items_and_function_call_outputs():
    conn = {'protocol': 'openai_responses', 'base_url': 'https://api.openai.com/v1', 'external': True}
    model = {'model_id': 'account-selected-model', 'allowed_parameters': ['max_output_tokens', 'reasoning'], 'parameters': {'max_output_tokens': 100, 'reasoning': {'effort': 'low'}}}
    native = [{'id': 'reasoning-1', 'type': 'reasoning', 'summary': [], 'encrypted_content': 'synthetic-signature'}, {'type': 'function_call', 'call_id': 'call-1', 'name': 'read_file', 'arguments': '{"path":"a.py"}'}]
    def first(request):
        assert str(request.url) == 'https://api.openai.com/v1/responses'
        body = json.loads(request.content)
        assert body['store'] is False and body['include'] == ['reasoning.encrypted_content']
        assert body['tools'][0]['name'] == 'read_file' and body['max_output_tokens'] == 100
        return httpx.Response(200, json={'status': 'completed', 'output': native, 'usage': {'input_tokens': 10, 'output_tokens': 4}})
    messages = [{'role': 'user', 'content': 'Read a file'}]
    reply = invoke(conn, model, messages, [TOOL], first)
    messages += [{'role': 'assistant', 'content': reply.text, 'calls': reply.calls, 'anthropic_native': reply.native}, {'role': 'tool', 'call_id': 'call-1', 'content': 'file contents'}]
    def second(request):
        body = json.loads(request.content)
        assert body['input'][1:3] == native
        assert body['input'][3] == {'type': 'function_call_output', 'call_id': 'call-1', 'output': 'file contents'}
        return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'Done'}]}], 'usage': {'input_tokens': 20, 'output_tokens': 2}})
    assert invoke(conn, model, messages, [TOOL], second).text == 'Done'


def test_gemini_native_thought_signatures_parallel_calls_and_usage_roundtrip():
    conn = {'protocol': 'google_gemini', 'base_url': 'https://generativelanguage.googleapis.com/v1beta', 'external': True}
    model = {'model_id': 'models/account-model', 'allowed_parameters': ['maxOutputTokens'], 'parameters': {'maxOutputTokens': 100}}
    parts = [{'text': 'synthetic internal thought', 'thought': True}, {'functionCall': {'id': 'native-id', 'name': 'read_file', 'args': {'path': 'a.py'}}, 'thoughtSignature': 'synthetic-signature'}]
    def first(request):
        assert str(request.url).endswith('/models/account-model:generateContent')
        assert request.headers['x-goog-api-key'] == KEY and 'key=' not in str(request.url)
        body = json.loads(request.content)
        assert body['systemInstruction']['parts'][0]['text'] == 'System'
        assert body['tools'][0]['functionDeclarations'][0]['parametersJsonSchema'] == TOOL['parameters']
        return httpx.Response(200, json={'candidates': [{'content': {'role': 'model', 'parts': parts}, 'finishReason': 'STOP'}], 'usageMetadata': {'promptTokenCount': 10, 'candidatesTokenCount': 2, 'thoughtsTokenCount': 3}})
    history = [{'role': 'system', 'content': 'System'}, {'role': 'user', 'content': 'Read'}]
    reply = invoke(conn, model, history, [TOOL], first)
    assert reply.text == '' and reply.output_tokens == 5 and reply.calls[0]['id'] == 'native-id'
    history += [{'role': 'assistant', 'content': '', 'calls': reply.calls, 'anthropic_native': reply.native}, {'role': 'tool', 'call_id': 'native-id', 'content': 'file'}]
    def second(request):
        body = json.loads(request.content)
        assert body['contents'][1]['parts'] == parts
        assert body['contents'][2]['parts'][0]['functionResponse'] == {'id': 'native-id', 'name': 'read_file', 'response': {'result': 'file'}}
        return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text': 'Done'}]}, 'finishReason': 'STOP'}]})
    assert invoke(conn, model, history, [TOOL], second).text == 'Done'


def test_deepseek_chat_reasoning_content_survives_tool_cycle():
    conn = {'protocol': 'openai_chat', 'base_url': 'https://api.deepseek.com/v1', 'external': True}
    model = {'model_id': 'account-model', 'allowed_parameters': ['max_tokens', 'thinking'], 'parameters': {'max_tokens': 100, 'thinking': {'type': 'enabled'}}}
    history = [{'role': 'user', 'content': 'Read'}]
    reply = invoke(conn, model, history, [TOOL], lambda r: httpx.Response(200, json={'choices': [{'message': {'content': None, 'reasoning_content': 'synthetic thought fixture', 'tool_calls': [{'id': 'c', 'function': {'name': 'read_file', 'arguments': '{"path":"a"}'}}]}}]}))
    history += [{'role': 'assistant', 'content': reply.text, 'calls': reply.calls, 'anthropic_native': reply.native}, {'role': 'tool', 'call_id': 'c', 'content': 'file'}]
    def second(request):
        assert json.loads(request.content)['messages'][1]['reasoning_content'] == 'synthetic thought fixture'
        return httpx.Response(200, json={'choices': [{'message': {'content': 'Done'}}]})
    assert invoke(conn, model, history, [TOOL], second).text == 'Done'


@pytest.mark.parametrize('protocol', ['openai_chat', 'openai_responses', 'anthropic_messages', 'google_gemini'])
def test_official_catalog_protocols_and_native_pagination(protocol):
    requests = []
    def handler(request):
        requests.append(request)
        assert request.method == 'GET' and request.url.path == '/v1/models'
        if protocol == 'google_gemini':
            assert request.headers['x-goog-api-key'] == KEY
            if len(requests) == 1:
                return httpx.Response(200, json={'models': [{'name': 'models/text', 'displayName': 'Text', 'supportedGenerationMethods': ['generateContent'], 'inputTokenLimit': 120000}, {'name': 'models/embedding', 'supportedGenerationMethods': ['embedContent']}], 'nextPageToken': 'next'})
            assert request.url.params['pageToken'] == 'next'
            return httpx.Response(200, json={'models': [{'name': 'models/text2', 'supportedGenerationMethods': ['generateContent']}]})
        if protocol == 'anthropic_messages':
            assert request.headers['x-api-key'] == KEY and request.headers['anthropic-version'] == '2023-06-01'
            if len(requests) == 1: return httpx.Response(200, json={'data': [{'id': 'text', 'display_name': 'Text'}], 'has_more': True, 'last_id': 'text'})
            assert request.url.params['after_id'] == 'text'
        else: assert request.headers['authorization'] == 'Bearer ' + KEY
        return httpx.Response(200, json={'data': [{'id': 'text2'}]})
    result = asyncio.run(providers.discover({'protocol': protocol, 'base_url': 'https://official.example/v1', 'external': True}, KEY, transport=httpx.MockTransport(handler)))
    assert result[-1]['id'] == 'text2'
    if protocol == 'google_gemini':
        assert result[0]['context_window'] == 120000 and len(result) == 3
        assert result[1]['id'] == 'embedding'
        assert result[1]['capabilities'] == ['embedContent']


def test_gemini_model_id_cannot_change_url_path():
    model = {'model_id': '../other?key=bad', 'allowed_parameters': [], 'parameters': {}}
    with pytest.raises(ValueError, match='模型 ID'):
        invoke({'protocol': 'google_gemini', 'base_url': 'https://gemini.example', 'external': True}, model, [], [], lambda r: pytest.fail('must reject before request'))


@pytest.mark.parametrize('protocol', ['openai_chat', 'openai_responses', 'anthropic_messages', 'google_gemini', 'custom_http'])
def test_setup_discovery_uses_common_protocols_and_never_saves_or_calls_model(client, monkeypatch, protocol):
    seen = []
    async def discover(conn, key, **kwargs):
        seen.append(conn)
        assert key == KEY
        return [{'id': 'select-later', 'name': 'Account model', 'context_window': 32000}]
    async def no_complete(*a, **k): pytest.fail('directory discovery must not make model calls')
    monkeypatch.setattr(providers, 'discover', discover)
    monkeypatch.setattr(providers, 'complete', no_complete)
    body = {'name': 'Native setup', 'protocol': protocol, 'base_url': 'https://official.test/v1', 'api_key': KEY}
    if protocol == 'custom_http': body['protocol_config'] = CONFIG
    r = client.post('/api/setup/models', json=body)
    assert r.status_code == 200, r.text
    assert r.json()['models'][0] == {'id': 'select-later', 'name': 'Account model', 'label': 'Account model', 'context_window': 32000}
    assert seen[0]['protocol'] == protocol
    if protocol == 'custom_http': assert seen[0]['protocol_config']['endpoint'] == '/generate'
    assert client.get('/api/providers').json() == [] and client.get('/api/models').json() == []
    assert KEY not in r.text


@pytest.mark.parametrize('protocol,parameter', [('openai_responses', 'max_output_tokens'), ('google_gemini', 'maxOutputTokens'), ('anthropic_messages', 'max_tokens'), ('custom_http', 'output_limit')])
def test_setup_native_and_custom_connection_persists_correct_dialect_without_implicit_binding(client, monkeypatch, protocol, parameter):
    seen = []
    async def fake_complete(conn, model, messages, tools, key, timeout, **kwargs):
        seen.append((conn, model))
        assert model['parameters'][parameter] > 0
        if tools:
            nonce = messages[0]['content'].split('nonce ')[1].split('.')[0]
            return providers.Reply('', [{'id': 'synthetic-call', 'name': 'forge_connection_check', 'arguments': {'nonce': nonce}}], 1, 1)
        return providers.Reply('OK', [], 1, 1)
    monkeypatch.setattr(providers, 'complete', fake_complete)
    body = {'name': 'Native setup', 'protocol': protocol, 'base_url': 'https://official.test/v1', 'api_key': KEY, 'model_id': 'selected-model', 'acknowledge_probe': True}
    if protocol == 'custom_http': body.update(protocol_config=CONFIG, parameters={'output_limit': 100})
    before = client.get('/api/agents').json()
    r = client.post('/api/setup/connect', json=body, headers={'Idempotency-Key': 'new-protocol-setup'})
    assert r.status_code == 200, r.text
    assert len(seen) == 2 and seen[0][0]['protocol'] == protocol
    assert client.get('/api/agents').json() == before
    saved = client.get('/api/providers').json()[0]
    assert saved['protocol'] == protocol and saved['has_key']
    if protocol == 'custom_http': assert saved['protocol_config']['endpoint'] == '/generate'
    assert KEY not in r.text + json.dumps(saved)


def test_model_hub_saved_provider_discovery_preserves_names_and_is_read_only(client, monkeypatch):
    async def listing(conn, key, **kwargs):
        assert key == KEY and conn['protocol'] == 'openai_responses'
        return [{'id': 'account-model', 'name': '可用型号'}]
    monkeypatch.setattr(providers, 'discover', listing)
    p = client.post('/api/providers', json={'name': 'OpenAI', 'protocol': 'openai_responses', 'base_url': 'https://api.openai.com/v1', 'api_key': KEY}).json()
    r = client.post('/api/model-hub/providers/' + p['id'] + '/discover', json={})
    assert r.status_code == 200, r.text
    assert r.json()['models'][0]['label'] == '可用型号' and not r.json()['installed']
    assert client.get('/api/models').json() == [] and KEY not in r.text
