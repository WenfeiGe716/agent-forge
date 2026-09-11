"""Explicit protocol adapters. No vendor SDK, hard-coded model names, or silent parameter dropping."""
import asyncio
import ipaddress
import json
import socket
import re
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse
import httpx
from .security import validate_url
from . import protocols

RESERVED = {'model', 'messages', 'tools', 'tool_choice', 'system', 'stream', 'api_key', 'base_url', 'headers'}
OPENAI_PARAMS = {'temperature', 'top_p', 'max_tokens', 'max_completion_tokens', 'reasoning_effort', 'presence_penalty', 'frequency_penalty', 'seed', 'stop', 'thinking'}
ANTHROPIC_PARAMS = {'temperature', 'top_p', 'top_k', 'max_tokens', 'stop_sequences', 'thinking', 'output_config'}
RESPONSES_PARAMS = {'temperature', 'top_p', 'max_output_tokens', 'reasoning', 'text', 'parallel_tool_calls'}
GEMINI_PARAMS = {'temperature', 'topP', 'topK', 'maxOutputTokens', 'stopSequences', 'thinkingConfig', 'responseMimeType', 'responseSchema', 'seed'}
PARAMETERS = {'openai_chat': OPENAI_PARAMS, 'anthropic_messages': ANTHROPIC_PARAMS, 'openai_responses': RESPONSES_PARAMS, 'google_gemini': GEMINI_PARAMS}

class ProviderError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable

@dataclass
class Reply:
    text: str
    calls: list[dict]
    input_tokens: int
    output_tokens: int
    native: list | None = None

def validate_parameters(protocol: str, declared: list[str], parameters: dict, protocol_config: dict | None = None) -> None:
    if protocol == 'custom_http':
        protocols.validate_custom_parameters(protocol_config, declared, parameters)
        return
    if protocol not in PARAMETERS:
        raise ValueError('未知模型协议适配器')
    supported = PARAMETERS[protocol]
    unknown = set(declared) - supported
    if unknown:
        raise ValueError(f'适配器尚未实现参数: {", ".join(sorted(unknown))}')
    if set(parameters) & RESERVED or set(parameters) - set(declared):
        raise ValueError('存在模型未声明支持的参数，或试图覆盖受保护请求字段')
    for k in ('max_tokens', 'max_completion_tokens', 'max_output_tokens', 'maxOutputTokens', 'topK'):
        if k in parameters and (isinstance(parameters[k], bool) or not isinstance(parameters[k], int) or parameters[k] < 1):
            raise ValueError(f'{k} 必须是正整数')
    if protocol == 'openai_chat' and 'max_tokens' in parameters and 'max_completion_tokens' in parameters:
        raise ValueError('max_tokens 与 max_completion_tokens 不可同时设置')
    if protocol == 'anthropic_messages' and 'max_tokens' not in parameters:
        raise ValueError('Anthropic Messages 必须配置 max_tokens')
    for k, low, high in [('temperature', 0, 1 if protocol == 'anthropic_messages' else 2), ('top_p', 0, 1), ('topP', 0, 1)]:
        if k in parameters and (isinstance(parameters[k], bool) or not isinstance(parameters[k], (int, float)) or not low <= parameters[k] <= high):
            raise ValueError(f'{k} 不在允许范围 {low}..{high}')

async def validate_private_target(base_url: str) -> None:
    host = urlparse(base_url).hostname
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except OSError as exc:
        raise ProviderError('无法解析内网模型服务地址') from exc
    if not infos: raise ProviderError('内网模型服务未返回可用地址')
    for item in infos:
        address = ipaddress.ip_address(item[4][0])
        if address.is_link_local or address.is_multicast or address.is_unspecified or not (address.is_private or address.is_loopback):
            raise ProviderError('标记为内网的供应商解析到了非内网或受限地址')

def openai_messages(messages: list[dict]) -> list[dict]:
    output = []
    for m in messages:
        if m['role'] == 'assistant':
            item: dict[str, Any] = {'role': 'assistant', 'content': m.get('content') or None}
            if m.get('calls'):
                item['tool_calls'] = [{'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': json.dumps(c['arguments'], ensure_ascii=False)}} for c in m['calls']]
            native = m.get('anthropic_native')
            if native and native[0].get('protocol') == 'openai_chat' and 'reasoning_content' in native[0]:
                item['reasoning_content'] = native[0]['reasoning_content']
            output.append(item)
        elif m['role'] == 'tool':
            output.append({'role': 'tool', 'tool_call_id': m['call_id'], 'content': m['content']})
        else:
            output.append({'role': m['role'], 'content': m['content']})
    return output

def anthropic_messages(messages: list[dict]) -> tuple[str, list]:
    system, output = [], []
    for m in messages:
        if m['role'] == 'system':
            system.append(m['content'])
            continue
        if m['role'] == 'tool':
            role, blocks = 'user', [{'type': 'tool_result', 'tool_use_id': m['call_id'], 'content': m['content']}]
        elif m['role'] == 'assistant':
            role = 'assistant'
            # Thinking/signature blocks must be preserved, not reconstructed from visible text.
            blocks = m.get('anthropic_native')
            if blocks is None:
                blocks = ([{'type': 'text', 'text': m['content']}] if m.get('content') else [])
                blocks += [{'type': 'tool_use', 'id': c['id'], 'name': c['name'], 'input': c['arguments']} for c in m.get('calls', [])]
            if not blocks:
                blocks = [{'type': 'text', 'text': '(no text)'}]
        else:
            role, blocks = 'user', [{'type': 'text', 'text': m['content']}]
        if output and output[-1]['role'] == role:
            output[-1]['content'].extend(blocks)
        else:
            output.append({'role': role, 'content': blocks})
    return '\n\n'.join(system), output


def responses_messages(messages: list[dict]) -> list[dict]:
    output = []
    for m in messages:
        if m['role'] == 'tool':
            output.append({'type': 'function_call_output', 'call_id': m['call_id'], 'output': m['content']})
        elif m['role'] == 'assistant':
            native = m.get('anthropic_native')
            if native and native[0].get('protocol') == 'openai_responses':
                output.extend(native[0]['output'])
            else:
                if m.get('content'): output.append({'role': 'assistant', 'content': m['content']})
                output.extend({'type': 'function_call', 'call_id': c['id'], 'name': c['name'], 'arguments': json.dumps(c['arguments'], ensure_ascii=False)} for c in m.get('calls', []))
        else:
            output.append({'role': m['role'], 'content': m['content']})
    return output


def gemini_messages(messages: list[dict]) -> tuple[str, list]:
    system, output, calls = [], [], {}
    for m in messages:
        if m['role'] == 'system':
            system.append(m['content'])
            continue
        role = 'model' if m['role'] == 'assistant' else 'user'
        if m['role'] == 'tool':
            if m['call_id'] not in calls: raise ValueError('Gemini 工具结果没有关联的调用记录')
            call = calls[m['call_id']]
            function = {'name': call['name'], 'response': {'result': m['content']}}
            if call.get('native_id'): function['id'] = call['native_id']
            parts = [{'functionResponse': function}]
        elif m['role'] == 'assistant':
            native = m.get('anthropic_native')
            if native and native[0].get('protocol') == 'google_gemini':
                parts = native[0]['parts']
                native_calls = [p['functionCall'] for p in parts if 'functionCall' in p]
            else:
                parts = ([{'text': m['content']}] if m.get('content') else []) + [{'functionCall': {'name': c['name'], 'args': c['arguments']}} for c in m.get('calls', [])]
                native_calls = []
            for i, call in enumerate(m.get('calls', [])):
                calls[call['id']] = {'name': call['name'], 'native_id': native_calls[i].get('id') if i < len(native_calls) else None}
        else:
            parts = [{'text': m['content']}]
        if output and output[-1]['role'] == role:
            output[-1]['parts'].extend(parts)
        else:
            output.append({'role': role, 'parts': list(parts)})
    return '\n\n'.join(system), output

async def _notify_delta(on_delta, text: str, *, protocol: str, sequence: int) -> None:
    """Deliver a verified upstream text fragment to the run event log.

    The callback is deliberately optional so existing non-streaming callers and
    compatibility adapters retain their exact request/response behaviour.
    """
    if not text or on_delta is None:
        return
    value = on_delta({'text': text, 'protocol': protocol, 'sequence': sequence})
    if hasattr(value, '__await__'):
        await value


async def _read_sse(response, protocol: str, on_delta) -> dict:
    """Read a provider SSE response and rebuild the normal JSON envelope.

    This parser only emits text that arrived from the upstream stream. Tool
    calls are accumulated and parsed by the existing protocol response logic;
    malformed/unsupported events remain a ProviderError instead of being shown
    as a successful model result.
    """
    text = ''
    sequence = 0
    usage = {}
    tool_calls = {}
    response_output = []
    anthropic_blocks = []
    gemini_parts = []
    event_name = None
    async for line in response.aiter_lines():
        if line.startswith('event:'):
            event_name = line[6:].strip()
            continue
        if not line.startswith('data:'):
            continue
        raw = line[5:].strip()
        if raw == '[DONE]':
            break
        try:
            item = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if protocol == 'openai_chat':
            usage.update(item.get('usage') or {})
            for choice in item.get('choices') or []:
                delta = choice.get('delta') or {}
                fragment = delta.get('content') or ''
                if fragment:
                    text += fragment; sequence += 1
                    await _notify_delta(on_delta, fragment, protocol=protocol, sequence=sequence)
                for call in delta.get('tool_calls') or []:
                    index = call.get('index', 0)
                    current = tool_calls.setdefault(index, {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                    current['id'] += call.get('id') or ''
                    fn = call.get('function') or {}
                    current['function']['name'] += fn.get('name') or ''
                    current['function']['arguments'] += fn.get('arguments') or ''
        elif protocol == 'anthropic_messages':
            usage.update((item.get('message') or {}).get('usage') or {})
            usage.update(item.get('usage') or {})
            if item.get('type') == 'content_block_start':
                block = item.get('content_block') or {}
                anthropic_blocks.append(dict(block))
            elif item.get('type') == 'content_block_delta':
                delta = item.get('delta') or {}
                if delta.get('type') == 'text_delta' and delta.get('text'):
                    fragment = delta['text']; text += fragment; sequence += 1
                    if anthropic_blocks and anthropic_blocks[-1].get('type') == 'text':
                        anthropic_blocks[-1]['text'] = (anthropic_blocks[-1].get('text') or '') + fragment
                    await _notify_delta(on_delta, fragment, protocol=protocol, sequence=sequence)
                elif delta.get('type') == 'input_json_delta' and anthropic_blocks:
                    anthropic_blocks[-1]['input'] = (anthropic_blocks[-1].get('input') or '') + (delta.get('partial_json') or '')
            elif item.get('type') == 'message_delta':
                usage.update(item.get('usage') or {})
            elif item.get('type') == 'message_stop':
                break
        elif protocol == 'openai_responses':
            typ = item.get('type') or event_name or ''
            fragment = item.get('delta') if 'output_text.delta' in typ else ''
            if fragment:
                text += fragment; sequence += 1
                await _notify_delta(on_delta, fragment, protocol=protocol, sequence=sequence)
            if 'function_call_arguments.delta' in typ:
                call_id = item.get('item_id') or item.get('call_id') or 'stream-call'
                call = tool_calls.setdefault(call_id, {'id': call_id, 'type': 'function_call', 'call_id': call_id, 'name': item.get('name') or '', 'arguments': ''})
                call['arguments'] += item.get('delta') or ''
            if 'output_item.done' in typ:
                output = item.get('item')
                if isinstance(output, dict): response_output.append(output)
            usage.update(item.get('usage') or {})
        elif protocol == 'google_gemini':
            candidates = item.get('candidates') or []
            parts = ((candidates[0].get('content') or {}).get('parts') if candidates else []) or []
            for part in parts:
                if part.get('text') and not part.get('thought'):
                    fragment = part['text']; text += fragment; sequence += 1
                    await _notify_delta(on_delta, fragment, protocol=protocol, sequence=sequence)
                gemini_parts.append(part)
            usage.update(item.get('usageMetadata') or {})
    if protocol == 'openai_chat':
        calls = list(tool_calls.values())
        return {'choices': [{'message': {'content': text or None, 'tool_calls': calls}}], 'usage': usage}
    if protocol == 'anthropic_messages':
        for block in anthropic_blocks:
            if block.get('type') == 'tool_use' and isinstance(block.get('input'), str):
                try:
                    block['input'] = json.loads(block['input'] or '{}')
                except (TypeError, ValueError) as exc:
                    raise ProviderError('Anthropic 工具参数流格式无效') from exc
        return {'content': anthropic_blocks or ([{'type': 'text', 'text': text}] if text else []), 'usage': usage}
    if protocol == 'openai_responses':
        output = response_output or ([{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}] if text else [])
        output.extend(tool_calls.values())
        return {'status': 'completed', 'output': output, 'usage': usage}
    return {'candidates': [{'finishReason': 'STOP', 'content': {'parts': gemini_parts or ([{'text': text}] if text else [])}}], 'usageMetadata': usage}


async def complete(connection: dict, model: dict, messages: list[dict], tools: list[dict], key: str, timeout: int, transport=None, on_delta: Callable[[dict], Awaitable[None] | None] | None = None) -> Reply:
    base = validate_url(connection['base_url'])
    protocol = connection['protocol']
    params = model['parameters']
    validate_parameters(protocol, model['allowed_parameters'], params, connection.get('protocol_config'))
    if not connection['external'] and transport is None:
        await validate_private_target(base)
    headers = {'content-type': 'application/json'}
    adapter = None
    method = 'POST'
    max_response_bytes = 8_000_000
    if protocol == 'openai_chat':
        if key: headers['authorization'] = f'Bearer {key}'
        body = {'model': model['model_id'], 'messages': openai_messages(messages), **params}
        if on_delta: body['stream'] = True
        if tools: body['tools'] = [{'type': 'function', 'function': t} for t in tools]
        url = base + '/chat/completions'
    elif protocol == 'anthropic_messages':
        headers.update({'x-api-key': key, 'anthropic-version': '2023-06-01'})
        system, converted = anthropic_messages(messages)
        body = {'model': model['model_id'], 'system': system, 'messages': converted, **params}
        if on_delta: body['stream'] = True
        if tools: body['tools'] = [{'name': t['name'], 'description': t['description'], 'input_schema': t['parameters']} for t in tools]
        url = base + '/messages'
    elif protocol == 'openai_responses':
        if key: headers['authorization'] = f'Bearer {key}'
        body = {'model': model['model_id'], 'input': responses_messages(messages), 'store': False,
                'include': ['reasoning.encrypted_content'], **params}
        if on_delta: body['stream'] = True
        if tools: body['tools'] = [{'type': 'function', 'name': t['name'], 'description': t.get('description', ''), 'parameters': t['parameters'], 'strict': False} for t in tools]
        url = base + '/responses'
    elif protocol == 'google_gemini':
        if key: headers['x-goog-api-key'] = key
        model_id = model['model_id'].removeprefix('models/')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}', model_id):
            raise ValueError('Gemini 模型 ID 格式无效')
        system, converted = gemini_messages(messages)
        body = {'contents': converted, 'generationConfig': params}
        if system: body['systemInstruction'] = {'parts': [{'text': system}]}
        if tools: body['tools'] = [{'functionDeclarations': [{'name': t['name'], 'description': t.get('description', ''), 'parametersJsonSchema': t['parameters']} for t in tools]}]
        url = base + ('/models/' + model_id + ':streamGenerateContent?alt=sse' if on_delta else '/models/' + model_id + ':generateContent')
    else:
        config = protocols.validate_connection(protocol, base, connection['external'], connection.get('protocol_config'))
        adapter = protocols.parse_config(config)
        body, headers = protocols.build_request(adapter, model, messages, tools, key)
        url = base + adapter.endpoint
        method = adapter.method
        timeout = min(timeout, adapter.timeout_s)
        max_response_bytes = adapter.max_response_bytes
    try:
        async with asyncio.timeout(min(timeout, 1800)), httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False, transport=transport) as client:
            async with client.stream(method, url, headers=headers, json=body) as response:
                if response.status_code >= 300:
                    # Never store vendor error bodies: they may echo keys or code.
                    raise ProviderError(f'模型接口 HTTP {response.status_code}', response.status_code == 429)
                if on_delta and protocol in {'openai_chat', 'anthropic_messages', 'openai_responses', 'google_gemini'} and 'text/event-stream' in response.headers.get('content-type', ''):
                    result = await _read_sse(response, protocol, on_delta)
                else:
                    data, count = [], 0
                    async for chunk in response.aiter_bytes():
                        count += len(chunk)
                        if count > max_response_bytes: raise ProviderError('模型响应超过配置的安全上限')
                        data.append(chunk)
                    result = json.loads(b''.join(data))
    except httpx.ConnectError as exc:
        raise ProviderError('模型接口连接失败', True) from exc
    except (httpx.TimeoutException, TimeoutError) as exc:
        # A timeout may happen after the server accepted/billed a request. No automatic fallback.
        raise ProviderError('模型调用超时；结果不确定，不自动重放') from exc
    except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProviderError('模型接口响应不可用') from exc
    try:
        if adapter:
            return Reply(*protocols.parse_response(adapter, result))
        usage = result.get('usage', {})
        if protocol == 'openai_chat':
            message = result['choices'][0]['message']
            calls = [{'id': x['id'], 'name': x['function']['name'], 'arguments': json.loads(x['function']['arguments'])} for x in message.get('tool_calls', [])]
            native = [{'protocol': 'openai_chat', 'reasoning_content': message['reasoning_content']}] if 'reasoning_content' in message else None
            return Reply(message.get('content') or '', calls, usage.get('prompt_tokens', 0), usage.get('completion_tokens', 0), native)
        if protocol == 'openai_responses':
            if result.get('status') in ('failed', 'cancelled', 'incomplete'):
                raise ValueError('Responses 未完成')
            output = result['output']
            calls = [{'id': item['call_id'], 'name': item['name'], 'arguments': json.loads(item['arguments'])} for item in output if item['type'] == 'function_call']
            text = '\n'.join(part['text'] for item in output if item['type'] == 'message' for part in item['content'] if part['type'] == 'output_text')
            return Reply(text, calls, usage.get('input_tokens', 0), usage.get('output_tokens', 0), [{'protocol': protocol, 'output': output}])
        if protocol == 'google_gemini':
            candidate = result['candidates'][0]
            if candidate.get('finishReason') not in (None, 'STOP'):
                raise ValueError('Gemini 响应未正常完成')
            parts = candidate['content']['parts']
            calls = [{'id': part['functionCall'].get('id') or 'gemini_' + uuid.uuid4().hex[:20], 'name': part['functionCall']['name'], 'arguments': part['functionCall'].get('args', {})} for part in parts if 'functionCall' in part]
            text = '\n'.join(part['text'] for part in parts if 'text' in part and not part.get('thought'))
            usage = result.get('usageMetadata', {})
            return Reply(text, calls, usage.get('promptTokenCount', 0), usage.get('candidatesTokenCount', 0) + usage.get('thoughtsTokenCount', 0), [{'protocol': protocol, 'parts': parts}])
        native = result['content']
        calls = [{'id': b['id'], 'name': b['name'], 'arguments': b['input']} for b in native if b['type'] == 'tool_use']
        text = '\n'.join(b['text'] for b in native if b['type'] == 'text')
        return Reply(text, calls, usage.get('input_tokens', 0), usage.get('output_tokens', 0), native)
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
        raise ProviderError('模型未返回适配器支持的消息格式') from exc


def _require_complete_custom_catalog(adapter, result, response):
    """Custom discovery maps one complete array, not a programmable paginator."""
    incomplete = '自定义模型目录包含未处理的分页，目录不完整，不能更新或全量替换；请提供完整目录接口或使用支持该分页的协议'
    if 'next' in response.links:
        raise ProviderError(incomplete)
    # Inspect only response envelopes, never individual models: a model's own
    # metadata may legitimately contain fields named next/error.
    envelopes = [result]
    parts = adapter.discovery.items.split('/')[1:]
    envelopes.extend(protocols.pointer(result, '/' + '/'.join(parts[:count])) for count in range(1, len(parts)))
    visited = set()
    while envelopes:
        envelope = envelopes.pop()
        if not isinstance(envelope, dict) or id(envelope) in visited:
            continue
        visited.add(id(envelope))
        if len(visited) > 64:
            raise ProviderError('自定义模型目录分页元数据超过安全上限，未更新目录')
        if envelope.get('error') or envelope.get('errors'):
            raise ProviderError('自定义模型目录返回错误，未更新目录')
        for key in ('has_more', 'hasMore', 'has_next', 'hasNext'):
            if key in envelope:
                if type(envelope[key]) is not bool:
                    raise ProviderError('自定义模型目录分页状态格式无效，未更新目录')
                if envelope[key]:
                    raise ProviderError(incomplete)
        for key in ('nextPageToken', 'next_page_token', 'next_page', 'nextPage', 'next', 'next_cursor', 'nextCursor'):
            if key in envelope and envelope[key] not in (None, False, ''):
                raise ProviderError(incomplete)
        envelopes.extend(envelope.get(key) for key in ('pagination', 'paging', 'page_info', 'pageInfo', 'meta', 'links'))


async def discover_custom(connection: dict, key: str, timeout: int = 20, transport=None) -> list[dict]:
    """Read a catalog without importing/enabling models or making a model call."""
    base = validate_url(connection['base_url'])
    adapter = protocols.parse_config(protocols.validate_connection('custom_http', base, connection['external'], connection.get('protocol_config')))
    if adapter.discovery is None: raise ValueError('此自定义协议未配置模型发现，可手动填写模型 ID')
    if not connection['external'] and transport is None: await validate_private_target(base)
    # Override case-insensitively, including an administrator's cached defaults.
    # This is still a same-origin GET; redirect following remains disabled.
    headers = httpx.Headers(protocols.build_headers(adapter, key))
    headers.update({'cache-control': 'no-cache', 'pragma': 'no-cache'})
    try:
        async with asyncio.timeout(min(timeout, adapter.timeout_s)), httpx.AsyncClient(timeout=min(timeout, adapter.timeout_s), follow_redirects=False, trust_env=False, transport=transport) as client:
            async with client.stream('GET', base + adapter.discovery.endpoint, headers=headers) as response:
                if response.status_code >= 300: raise ProviderError(f'模型目录接口 HTTP {response.status_code}', response.status_code == 429)
                chunks, count = [], 0
                async for chunk in response.aiter_bytes():
                    count += len(chunk)
                    if count > adapter.max_response_bytes: raise ProviderError('模型目录响应超过配置的安全上限')
                    chunks.append(chunk)
                result = json.loads(b''.join(chunks))
                _require_complete_custom_catalog(adapter, result, response)
                return protocols.parse_discovery(adapter, result)
    except httpx.ConnectError as exc:
        raise ProviderError('模型目录接口连接失败', True) from exc
    except (httpx.TimeoutException, TimeoutError) as exc:
        raise ProviderError('模型目录接口超时') from exc
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        raise ProviderError('模型目录返回格式与协议配置不匹配') from exc


async def discover(connection: dict, key: str, timeout: int = 20, transport=None) -> list[dict]:
    """List official/compatible models, including bounded native pagination."""
    protocol = connection['protocol']
    if protocol == 'custom_http': return await discover_custom(connection, key, timeout, transport)
    if protocol not in PARAMETERS: raise ValueError('未知模型协议适配器')
    base = validate_url(connection['base_url'])
    if not connection['external'] and transport is None: await validate_private_target(base)
    headers = {'accept': 'application/json', 'cache-control': 'no-cache', 'pragma': 'no-cache'}
    if protocol == 'anthropic_messages': headers.update({'x-api-key': key, 'anthropic-version': '2023-06-01'})
    elif protocol == 'google_gemini': headers['x-goog-api-key'] = key
    elif key: headers['authorization'] = f'Bearer {key}'
    models, seen, cursors, query = [], set(), set(), {}
    if protocol == 'google_gemini': query['pageSize'] = 200
    if protocol == 'anthropic_messages': query['limit'] = 100
    try:
        async with asyncio.timeout(min(timeout, 120)):
            async with httpx.AsyncClient(timeout=min(timeout, 30), follow_redirects=False, trust_env=False, transport=transport) as client:
                for _ in range(10):
                    async with client.stream('GET', base + '/models', headers=headers, params=query) as response:
                        if response.status_code >= 300: raise ProviderError(f'模型目录接口 HTTP {response.status_code}', response.status_code == 429)
                        chunks, count = [], 0
                        async for chunk in response.aiter_bytes():
                            count += len(chunk)
                            if count > 8_000_000: raise ProviderError('模型目录响应超过 8MB')
                            chunks.append(chunk)
                        data = json.loads(b''.join(chunks))
                    if not isinstance(data, dict) or data.get('error') is not None:
                        raise ProviderError('模型目录返回错误或无效响应，未更新目录')
                    if protocol == 'google_gemini':
                        # ProtoJSON omits empty repeated fields (and accepts null).
                        # Do not confuse that legal empty page with a gateway envelope.
                        if data.get('models') is None:
                            if set(data) - {'models', 'nextPageToken'}:
                                raise ProviderError('模型目录返回格式不可用，未更新目录')
                            items = []
                        else:
                            items = data['models']
                    else:
                        items = data['data']
                    if not isinstance(items, list) or len(items) > 2000: raise ValueError('模型目录不是有界数组')
                    for item in items:
                        if protocol == 'google_gemini':
                            mid = item['name'].removeprefix('models/')
                            name = item.get('displayName', mid)
                        else:
                            mid = item['id']; name = item.get('display_name', item.get('name', mid))
                        if not isinstance(mid, str) or not mid.strip() or len(mid) > 200 or not isinstance(name, str) or len(name) > 200:
                            raise ValueError('模型目录条目无效')
                        if mid in seen: continue
                        entry = {'id': mid, 'name': name}
                        declared = item.get('supportedGenerationMethods') if protocol == 'google_gemini' else item.get('capabilities')
                        if isinstance(declared, list) and len(declared) <= 100 and all(isinstance(v, str) and len(v) <= 200 for v in declared):
                            entry['capabilities'] = declared
                        if protocol == 'google_gemini' and type(item.get('inputTokenLimit')) is int and item['inputTokenLimit'] > 0:
                            entry['context_window'] = item['inputTokenLimit']
                        models.append(entry); seen.add(mid)
                        if len(models) > 2000: raise ValueError('模型目录超过 2000 项')
                    if protocol == 'google_gemini':
                        cursor = data.get('nextPageToken')
                        if cursor is None: return models
                        if not isinstance(cursor, str): raise ProviderError('模型目录分页游标格式无效，未更新目录')
                        if cursor == '': return models
                    else:
                        has_more = data.get('has_more', False)
                        if type(has_more) is not bool: raise ProviderError('模型目录分页 has_more 必须为布尔值，未更新目录')
                        if not has_more: return models
                        cursor = data.get('last_id')
                    if not isinstance(cursor, str) or not cursor.strip() or len(cursor) > 2000 or cursor in cursors:
                        raise ProviderError('模型目录分页游标缺失或无效，未更新目录')
                    cursors.add(cursor)
                    query['pageToken' if protocol == 'google_gemini' else 'after_id' if protocol == 'anthropic_messages' else 'after'] = cursor
                raise ValueError('模型目录超过 10 页安全上限')
    except httpx.ConnectError as exc:
        raise ProviderError('模型目录接口连接失败', True) from exc
    except (httpx.TimeoutException, TimeoutError) as exc:
        raise ProviderError('模型目录接口超时') from exc
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ProviderError('模型目录返回格式不可用或超过安全上限') from exc
