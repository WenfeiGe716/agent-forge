"""Bounded, declarative JSON/HTTP adapters. Templates are data, never Python/code.

Exact {{name}} values inject typed JSON; authentication is the only string
interpolation and is confined to one configured header on the provider origin.
"""
import copy
import json
import math
import re
from typing import Any, Literal
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

RESERVED_PARAMETERS = {'model', 'messages', 'tools', 'tool_choice', 'system', 'stream', 'api_key', 'base_url', 'headers'}
NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,79}$')
TOKEN = re.compile(r'^\{\{([a-z_]+)\}\}$')
HEADER = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
MISSING = object()


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ParameterRule(Strict):
    type: Literal['string', 'number', 'integer', 'boolean', 'object', 'array']
    minimum: float | None = Field(None, allow_inf_nan=False)
    maximum: float | None = Field(None, allow_inf_nan=False)
    enum: list[Any] | None = Field(None, max_length=100)
    required: bool = False

    @model_validator(mode='after')
    def check(self):
        if self.minimum is not None or self.maximum is not None:
            if self.type not in ('number', 'integer'):
                raise ValueError('参数范围仅可用于数字类型')
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError('参数 minimum 不可大于 maximum')
        return self


class Auth(Strict):
    header: str = 'Authorization'
    value: str = 'Bearer {{api_key}}'


class CallFields(Strict):
    id: str
    name: str
    arguments: str


class ResponseMap(Strict):
    text: str
    calls: str | None = None
    call_fields: CallFields | None = None
    input_tokens: str | None = None
    output_tokens: str | None = None


class Discovery(Strict):
    endpoint: str
    items: str
    id: str
    name: str | None = None


def default_messages():
    return {
        'system': {'role': 'system', 'content': '{{content}}'},
        'user': {'role': 'user', 'content': '{{content}}'},
        'assistant': {'role': 'assistant', 'content': '{{content}}', 'tool_calls': '{{calls}}'},
        'tool': {'role': 'tool', 'tool_call_id': '{{call_id}}', 'content': '{{content}}'},
    }


class HTTPProtocol(Strict):
    endpoint: str
    method: Literal['POST', 'PUT', 'PATCH'] = 'POST'
    body: dict[str, Any]
    response: ResponseMap
    auth: Auth | None = Field(default_factory=Auth)
    headers: dict[str, str] = Field(default_factory=dict, max_length=20)
    supported_parameters: list[str] = Field(default_factory=list, max_length=60)
    parameter_schema: dict[str, ParameterRule] = Field(default_factory=dict, max_length=60)
    parameter_placement: Literal['nested', 'root'] = 'nested'
    message_templates: dict[str, dict[str, Any]] = Field(default_factory=default_messages)
    call_template: dict[str, Any] = Field(default_factory=lambda: {
        'id': '{{id}}', 'type': 'function', 'function': {'name': '{{name}}', 'arguments': '{{arguments_json}}'}})
    tool_template: dict[str, Any] = Field(default_factory=lambda: {'type': 'function', 'function': '{{tool}}'})
    # Some APIs have a separate system field; this option prevents double-sending it.
    separate_system: bool = False
    timeout_s: int = Field(120, ge=1, le=1800)
    max_response_bytes: int = Field(8_000_000, ge=1024, le=8_000_000)
    discovery: Discovery | None = None

    @model_validator(mode='after')
    def check(self):
        validate_endpoint(self.endpoint)
        validate_template(self.body, {'model', 'messages', 'tools', 'parameters', 'system'})
        if not has_ref(self.body, 'model') or not has_ref(self.body, 'messages'):
            raise ValueError('请求模板必须包含 {{model}} 与 {{messages}}')
        if self.supported_parameters and self.parameter_placement == 'nested' and not has_ref(self.body, 'parameters'):
            raise ValueError('声明支持参数时，请求模板必须包含 {{parameters}}')
        if self.parameter_placement == 'root' and set(self.supported_parameters) & set(self.body):
            raise ValueError('根级参数不可覆盖请求模板已有字段')
        if self.separate_system and not has_ref(self.body, 'system'):
            raise ValueError('分离 system 消息时，请求模板必须包含 {{system}}')
        if set(self.message_templates) != {'system', 'user', 'assistant', 'tool'}:
            raise ValueError('必须定义 system/user/assistant/tool 四种消息模板')
        for role, template in self.message_templates.items():
            refs = {'role', 'content'} | ({'calls'} if role == 'assistant' else {'call_id'} if role == 'tool' else set())
            validate_template(template, refs)
            if not has_ref(template, 'content'):
                raise ValueError('消息模板必须保留 {{content}}')
        validate_template(self.call_template, {'id', 'name', 'arguments', 'arguments_json'})
        validate_template(self.tool_template, {'tool', 'name', 'description', 'parameters'})
        if self.response.calls is not None:
            if self.response.call_fields is None:
                raise ValueError('工具调用响应必须配置 call_fields')
            if not has_ref(self.body, 'tools') or not has_ref(self.message_templates['assistant'], 'calls') or not has_ref(self.message_templates['tool'], 'call_id'):
                raise ValueError('工具调用协议必须保留工具定义、调用记录和结果关联 ID')
            if not all(has_ref(self.call_template, key) for key in ('id', 'name')) or not any(has_ref(self.call_template, key) for key in ('arguments', 'arguments_json')):
                raise ValueError('调用模板必须保留 ID、名称与参数')
            if not has_ref(self.tool_template, 'tool') and not all(has_ref(self.tool_template, key) for key in ('name', 'parameters')):
                raise ValueError('工具定义必须保留名称与参数 schema')
        elif self.response.call_fields is not None:
            raise ValueError('call_fields 需要配置 calls 指针')
        for value in (self.response.text, self.response.calls, self.response.input_tokens, self.response.output_tokens):
            if value is not None: validate_pointer(value)
        if self.response.call_fields:
            for value in self.response.call_fields.model_dump().values(): validate_pointer(value)
        if len(set(self.supported_parameters)) != len(self.supported_parameters):
            raise ValueError('支持参数不可重复')
        for name in self.supported_parameters:
            if not NAME.fullmatch(name) or name in RESERVED_PARAMETERS:
                raise ValueError('自定义参数名无效或覆盖受保护字段')
        if set(self.parameter_schema) - set(self.supported_parameters):
            raise ValueError('parameter_schema 包含未声明的参数')
        blocked = {'host', 'content-length', 'transfer-encoding', 'connection', 'proxy-authorization', 'cookie', 'set-cookie', 'content-type'}
        for name, value in self.headers.items():
            validate_header(name, value)
            if name.lower() in blocked or re.search(r'auth|token|api.?key|secret', name, re.I):
                raise ValueError('鉴权必须放在 auth 字段；禁止覆盖连接控制头')
            if '{{' in value or '}}' in value:
                raise ValueError('普通 headers 不支持模板变量')
        if self.auth:
            validate_header(self.auth.header, self.auth.value)
            if self.auth.header.lower() in blocked or self.auth.header.lower() in {h.lower() for h in self.headers}:
                raise ValueError('鉴权 Header 不可覆盖受保护或重复字段')
            if self.auth.value.count('{{api_key}}') != 1 or '{{' in self.auth.value.replace('{{api_key}}', '') or '}}' in self.auth.value.replace('{{api_key}}', ''):
                raise ValueError('鉴权值必须包含且仅包含一个 {{api_key}}；凭据请填写加密 API Key 栏')
        if self.discovery:
            validate_endpoint(self.discovery.endpoint)
            for value in (self.discovery.items, self.discovery.id, self.discovery.name):
                if value is not None: validate_pointer(value)
        return self


def validate_endpoint(value: str) -> str:
    decoded = value
    for _ in range(3): decoded = unquote(decoded)
    if (not value.startswith('/') or value.startswith('//') or len(value) > 1000
        or any(c in decoded for c in ('?', '#', '\\', ':'))
        or any(ord(c) < 33 for c in decoded) or '//' in decoded
        or any(part in ('.', '..') for part in decoded.split('/')) or '%' in decoded):
        raise ValueError('endpoint 必须是同源相对路径；不允许 URL、查询串、路径穿越或跳转')
    return value


def validate_header(name: str, value: str):
    if not HEADER.fullmatch(name) or len(name) > 100 or len(value) > 1000 or any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise ValueError('HTTP Header 名称或值无效')


def validate_pointer(value: str):
    if len(value) > 1000 or (value != '' and not value.startswith('/')) or re.search(r'~(?![01])', value):
        raise ValueError('响应字段必须为 RFC 6901 JSON Pointer，例如 /result/text')


def has_ref(value: Any, name: str) -> bool:
    if isinstance(value, dict): return any(has_ref(v, name) for v in value.values())
    if isinstance(value, list): return any(has_ref(v, name) for v in value)
    return value == '{{' + name + '}}'


def validate_template(value: Any, refs: set[str], depth=0):
    if depth > 20: raise ValueError('JSON 模板嵌套超过 20 层')
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str) or '{{' in k or '}}' in k: raise ValueError('模板对象的键必须为固定字符串')
            validate_template(v, refs, depth+1)
    elif isinstance(value, list):
        for v in value: validate_template(v, refs, depth+1)
    elif isinstance(value, str) and ('{{' in value or '}}' in value):
        match = TOKEN.fullmatch(value)
        if not match or match[1] not in refs:
            raise ValueError('未知模板变量或非完整值插值；仅支持已列明的 {{name}} JSON 值')


def render(value: Any, context: dict) -> Any:
    if isinstance(value, dict): return {k: render(v, context) for k, v in value.items()}
    if isinstance(value, list): return [render(v, context) for v in value]
    if isinstance(value, str) and (match := TOKEN.fullmatch(value)):
        return copy.deepcopy(context[match[1]])
    return value


def parse_config(config: dict | None) -> HTTPProtocol:
    try:
        if len(json.dumps(config, ensure_ascii=False, allow_nan=False)) > 64000:
            raise ValueError('自定义协议配置超过 64KB')
        return HTTPProtocol.model_validate(config)
    except (TypeError, RecursionError) as exc:
        raise ValueError('自定义协议必须是有效且有界的 JSON 对象') from exc


def validate_connection(protocol: str, base_url: str, external: bool, config: dict | None) -> dict:
    if protocol != 'custom_http':
        if config: raise ValueError('内置协议不接受自定义 protocol_config')
        return {}
    if external and urlparse(base_url).scheme != 'https':
        raise ValueError('外部自定义协议必须使用 HTTPS，内网服务可使用 HTTP')
    return parse_config(config).model_dump()


def validate_custom_parameters(config: dict | None, declared: list[str], parameters: dict):
    adapter = parse_config(config)
    if len(set(declared)) != len(declared) or set(declared) - set(adapter.supported_parameters):
        raise ValueError('模型声明了自定义协议未实现的参数，或参数重复')
    if set(parameters) - set(declared) or set(parameters) & RESERVED_PARAMETERS:
        raise ValueError('存在模型未声明支持的参数，或试图覆盖受保护请求字段')
    if len(json.dumps(parameters, allow_nan=False)) > 100000:
        raise ValueError('模型参数超过 100KB')
    for name, rule in adapter.parameter_schema.items():
        if rule.required and name not in parameters:
            raise ValueError(f'自定义协议缺少必需参数: {name}')
        if name not in parameters: continue
        value = parameters[name]
        valid = {'string': isinstance(value, str), 'number': type(value) in (int, float),
                 'integer': type(value) is int, 'boolean': type(value) is bool,
                 'object': isinstance(value, dict), 'array': isinstance(value, list)}[rule.type]
        if not valid: raise ValueError(f'参数 {name} 的类型必须为 {rule.type}')
        if type(value) in (int, float) and (not math.isfinite(value) or (rule.minimum is not None and value < rule.minimum) or (rule.maximum is not None and value > rule.maximum)):
            raise ValueError(f'参数 {name} 超出自定义协议允许范围')
        if rule.enum is not None and value not in rule.enum:
            raise ValueError(f'参数 {name} 不在允许的枚举值中')


def build_request(adapter: HTTPProtocol, model: dict, messages: list[dict], tools: list[dict], key: str):
    if tools and adapter.response.calls is None:
        raise ValueError('自定义协议未配置工具调用响应映射，不能执行工具型 Agent')
    converted = []
    for m in messages:
        role = m['role']
        if adapter.separate_system and role == 'system': continue
        calls = [render(adapter.call_template, {'id': c['id'], 'name': c['name'], 'arguments': c['arguments'], 'arguments_json': json.dumps(c['arguments'], ensure_ascii=False)}) for c in m.get('calls', [])]
        converted.append(render(adapter.message_templates[role], {'role': role, 'content': m.get('content') or '', 'calls': calls, 'call_id': m.get('call_id', '')}))
    definitions = [render(adapter.tool_template, {'tool': t, 'name': t['name'], 'description': t.get('description', ''), 'parameters': t['parameters']}) for t in tools]
    body = render(adapter.body, {'model': model['model_id'], 'messages': converted, 'tools': definitions, 'parameters': model['parameters'], 'system': '\n\n'.join(m.get('content', '') for m in messages if m['role'] == 'system')})
    if adapter.parameter_placement == 'root': body.update(model['parameters'])
    if len(json.dumps(body, ensure_ascii=False, allow_nan=False).encode()) > 8_000_000:
        raise ValueError('自定义协议请求超过 8MB')
    return body, build_headers(adapter, key)


def build_headers(adapter: HTTPProtocol, key: str) -> dict:
    headers = {'content-type': 'application/json', **adapter.headers}
    if key and adapter.auth:
        # Substitution happens only here, never in a persisted request/config.
        value = adapter.auth.value.replace('{{api_key}}', key)
        if any(ord(c) < 32 or ord(c) > 126 for c in value): raise ValueError('API Key 含有非法 Header 字符')
        headers[adapter.auth.header] = value
    return headers


def pointer(data: Any, path: str, default=MISSING) -> Any:
    value = data
    try:
        for part in path.split('/')[1:]:
            part = part.replace('~1', '/').replace('~0', '~')
            if isinstance(value, list):
                if not re.fullmatch(r'0|[1-9][0-9]*', part): raise KeyError(part)
                value = value[int(part)]
            elif isinstance(value, dict): value = value[part]
            else: raise KeyError(part)
        return value
    except (KeyError, IndexError, ValueError):
        if default is not MISSING: return default
        raise ValueError('模型响应缺少配置的 JSON Pointer 字段') from None


def parse_response(adapter: HTTPProtocol, result: Any) -> tuple[str, list[dict], int, int]:
    mapping = adapter.response
    calls = []
    if mapping.calls is not None:
        raw_calls = pointer(result, mapping.calls, [])
        if raw_calls is None: raw_calls = []
        if not isinstance(raw_calls, list) or len(raw_calls) > 100: raise ValueError('工具调用必须是最多 100 项的数组')
        seen = set()
        for item in raw_calls:
            fields = mapping.call_fields
            call_id, name, arguments = (pointer(item, fields.id), pointer(item, fields.name), pointer(item, fields.arguments))
            if isinstance(arguments, str): arguments = json.loads(arguments)
            if not isinstance(call_id, str) or not call_id or len(call_id) > 200 or call_id in seen or not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(arguments, dict):
                raise ValueError('工具调用 ID、名称或参数格式无效')
            seen.add(call_id)
            calls.append({'id': call_id, 'name': name, 'arguments': arguments})
    text = pointer(result, mapping.text, None)
    if text is None:
        if not calls: raise ValueError('模型响应没有有效文本或工具调用')
        text = ''
    if not isinstance(text, str): raise ValueError('文本 JSON Pointer 必须指向字符串')
    usage = []
    for path in (mapping.input_tokens, mapping.output_tokens):
        count = pointer(result, path, 0) if path is not None else 0
        if type(count) is not int or count < 0 or count > 1_000_000_000: raise ValueError('token 用量必须是非负整数')
        usage.append(count)
    return text, calls, *usage


def parse_discovery(adapter: HTTPProtocol, result: Any) -> list[dict]:
    mapping = adapter.discovery
    if mapping is None: raise ValueError('此自定义协议未配置模型发现')
    items = pointer(result, mapping.items)
    if not isinstance(items, list) or len(items) > 2000: raise ValueError('发现结果必须是最多 2000 项的数组')
    models, seen = [], set()
    for item in items:
        model_id = pointer(item, mapping.id)
        if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 200: raise ValueError('发现结果包含无效模型 ID')
        name = pointer(item, mapping.name, model_id) if mapping.name is not None else model_id
        if not isinstance(name, str) or not name or len(name) > 200: raise ValueError('发现结果包含无效模型名称')
        if model_id not in seen: models.append({'id': model_id, 'name': name})
        seen.add(model_id)
    return models
