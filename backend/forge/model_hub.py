"""Measured model assets and explainable recommendations. Generation only creates drafts."""
from copy import deepcopy
import asyncio
import hashlib
import json
import math
import secrets
import time

from fastapi import Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import Field, model_validator
from sqlalchemy import String, Integer, JSON, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, ModelProfile, Provider, ProviderRevision, Skill, SkillRevision, session, uid, now, emit
from .schema import Strict, TOOLS_BY_ROLE
from .security import decrypt
from . import providers

DIMENSIONS = {'coding': '代码理解', 'reasoning': '推理', 'tool': '工具往返',
              'latency': '响应速度', 'cost': '成本', 'reliability': '请求成功率'}
SUITE = {
    'id': 'forge-core-smoke', 'version': 1,
    'description': '基础协议和受控问答测试；不代表真实项目编码能力或通用排行榜。代码答案只比对，不执行。',
    'cases': [
        {'id': 'off-by-one', 'dimension': 'coding', 'prompt': 'Python range(n) emits integers from 0 through n-1. For n=4, return ONLY the JSON array of values, no markdown.', 'expected': '[0,1,2,3]', 'match': 'json'},
        {'id': 'boundary', 'dimension': 'coding', 'prompt': 'A function should accept a nonempty string s. Which Python expression rejects both None and the empty string: A: s is None; B: not s; C: len(s)>0. Return only the letter.', 'expected': 'B', 'match': 'exact'},
        {'id': 'arithmetic', 'dimension': 'reasoning', 'prompt': 'A shelf has 17 books. 8 are borrowed and 3 are returned. How many remain? Return only the integer.', 'expected': '12', 'match': 'exact'},
        {'id': 'ordering', 'dimension': 'reasoning', 'prompt': 'Task C depends on B, and B depends on A. Return the valid execution order as only a JSON array of task letters.', 'expected': '["A","B","C"]', 'match': 'json'},
    ],
}


class ModelRevision(Base):
    __tablename__ = 'model_asset_revisions'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(32), default='')
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class BenchmarkRecord(Base):
    __tablename__ = 'model_benchmarks'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    model_id: Mapped[str] = mapped_column(String(32), index=True)
    model_version: Mapped[int] = mapped_column(Integer)
    provider_revision: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), default='running')
    config: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    actor: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class ModelHubInvocation(Base):
    """Durable reservations prevent retries from silently charging a second time."""
    __tablename__ = 'model_hub_invocations'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor: Mapped[str] = mapped_column(String(32), index=True)
    operation: Mapped[str] = mapped_column(String(24))
    request_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default='running', index=True)
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    response_body: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    finished_at: Mapped[str] = mapped_column(String(40), default='')


def interrupt_incomplete_invocations(s):
    """Call once at startup; uncertain paid calls are never automatically replayed."""
    rows = list(s.scalars(select(ModelHubInvocation).where(ModelHubInvocation.status == 'running')))
    for row in rows:
        row.status, row.status_code, row.finished_at = 'interrupted', 409, now()
        row.response_body = {'detail': '上次模型操作因服务重启中断，费用和结果可能未确认；请检查记录后发起新操作，系统不会自动重放'}
        emit(s, None, 'model_hub.invocation.interrupted', {'actor': row.actor, 'operation': row.operation, 'invocation_id': row.id})
    return len(rows)


async def invoke_once(operation, actor, payload, idempotency_key, success_code, work):
    if idempotency_key is None:
        return await work()
    ident = hashlib.sha256(json.dumps([actor, operation, idempotency_key], separators=(',', ':')).encode()).hexdigest()
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

    def replay(row):
        if row.request_digest != digest:
            raise HTTPException(409, '此幂等键已用于其他请求，请勿用同一键提交不同配置')
        if row.status == 'running':
            raise HTTPException(409, '同一模型操作正在执行，未再次发起计费调用；请稍后使用相同请求查询结果')
        return JSONResponse(deepcopy(row.response_body), status_code=row.status_code or 409)

    try:
        with session() as s:
            existing = s.get(ModelHubInvocation, ident)
            if existing:
                return replay(existing)
            s.add(ModelHubInvocation(id=ident, actor=actor, operation=operation, request_digest=digest))
            s.flush()
    except IntegrityError:
        # Concurrent requests race on the unique reservation, never on the call.
        with session() as s:
            existing = s.get(ModelHubInvocation, ident)
            if existing is None:
                raise
            return replay(existing)

    def record(status, code, body):
        with session() as s:
            row = s.get(ModelHubInvocation, ident)
            row.status, row.status_code = status, code
            row.response_body, row.finished_at = deepcopy(body), now()
            emit(s, None, 'model_hub.invocation.' + status, {
                'actor': actor, 'operation': operation, 'invocation_id': ident, 'status_code': code})

    try:
        result = await work()
    except asyncio.CancelledError:
        record('interrupted', 409, {'detail': '模型操作已中断，费用和结果可能未确认；请检查记录后发起新操作，系统不会自动重放'})
        raise
    except HTTPException as exc:
        record('failed', exc.status_code, {'detail': exc.detail})
        raise
    except ValueError as exc:
        record('failed', 422, {'detail': str(exc)})
        raise
    except Exception:
        detail = '模型操作未完成，系统不会自动重放；请检查记录后发起新操作'
        record('failed', 500, {'detail': detail})
        raise HTTPException(500, detail)
    record('completed', success_code, result)
    return result


class BenchmarkInput(Strict):
    acknowledge: bool = False
    suite_ref: dict | None = None


class CapabilityInput(Strict):
    model_profile_id: str = Field(min_length=1, max_length=32)
    benchmark_id: str | None = None
    system_scores: dict = Field(default_factory=dict)
    overrides: dict = Field(default_factory=dict)
    reason: str = Field('', max_length=4000)


class RecommendInput(Strict):
    kind: str = Field(pattern='^(routing|team)$')
    request: str = Field(min_length=3, max_length=12000)
    model_profile_id: str | None = None
    budget: str = Field('medium', pattern='^(low|medium|high)$')
    acknowledge: bool = False
    allow_external: bool = False
    allowed_provider_ids: list[str] = Field(default_factory=list, max_length=100)


def record_model(s, model, actor):
    key = f'{model.id}:{model.version}'
    if not s.get(ModelRevision, key):
        s.add(ModelRevision(id=key, model_id=model.id, version=model.version,
                            config=deepcopy(model.config), actor=actor))


def benchmark_out(row):
    return {'id': row.id, 'model_id': row.model_id, 'model_version': row.model_version,
            'provider_revision': row.provider_revision, 'status': row.status, 'suite': row.config['suite'],
            **deepcopy(row.result), 'actor': row.actor, 'created_at': row.created_at}


def validate_capability(s, config):
    config = CapabilityInput.model_validate(config).model_dump()
    model = s.get(ModelProfile, config.get('model_profile_id'))
    if not model:
        raise ValueError('能力画像的模型不存在')
    scores = config.get('system_scores', {})
    for values in (scores, config.get('overrides', {})):
        if not isinstance(values, dict) or set(values) - set(DIMENSIONS):
            raise ValueError('能力画像包含未知维度')
        for value in values.values():
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100):
                raise ValueError('能力评分必须为0到100之间的有限数值')
    if config.get('overrides') and not config.get('reason', '').strip():
        raise ValueError('人工修正需要填写依据')
    bid = config.get('benchmark_id')
    if bid:
        record = s.get(BenchmarkRecord, bid)
        if not record or record.model_id != model.id or record.status != 'completed':
            raise ValueError('必须引用同一模型已完成的Benchmark证据')
        if record.model_version != model.version:
            raise ValueError('模型配置已改变，请重新评估后发布能力画像')
        provider = s.get(Provider, model.config['provider_id'])
        if not provider or provider.revision_id != record.provider_revision:
            raise ValueError('供应商连接已改变，请重新评估后发布能力画像')
        if scores != record.result.get('scores', {}):
            raise ValueError('系统评分必须与Benchmark证据一致；请通过人工修正修改最终画像')
    elif any(v is not None for v in scores.values()):
        raise ValueError('系统评分必须有真实Benchmark证据；未评估时请使用人工修正')
    return config


def effective_profiles(s):
    from .design_center import DesignAsset, DesignRevision, revision_out
    profiles = {}
    assets = []
    for row in s.scalars(select(DesignAsset).where(DesignAsset.kind == 'capability', DesignAsset.published_version.is_not(None))):
        revision = s.get(DesignRevision, f'{row.id}:{row.published_version}')
        if revision and revision.status == 'published':
            assets.append(revision_out(row, revision))
    for asset in sorted(assets, key=lambda a: (a.get('published_at') or a.get('created_at') or '', a['id'])):
        cfg = asset['config']
        mid = cfg['model_profile_id']
        model = s.get(ModelProfile, mid)
        if not model:
            continue
        benchmark = s.get(BenchmarkRecord, cfg.get('benchmark_id')) if cfg.get('benchmark_id') else None
        provider = s.get(Provider, model.config['provider_id'])
        stale = bool(benchmark and (benchmark.model_version != model.version or not provider or benchmark.provider_revision != provider.revision_id))
        final = {dim: cfg.get('overrides', {}).get(dim, cfg.get('system_scores', {}).get(dim)) for dim in DIMENSIONS}
        profiles[mid] = {**asset, 'final_scores': final, 'stale': stale}
    return profiles


def model_overview(s):
    profiles = effective_profiles(s)
    result = []
    for model in s.scalars(select(ModelProfile).order_by(ModelProfile.id)):
        provider = s.get(Provider, model.config['provider_id'])
        result.append({'id': model.id, 'version': model.version, **deepcopy(model.config),
                       'provider_name': provider.name if provider else '',
                       'provider_enabled': bool(provider and provider.enabled),
                       'capability': profiles.get(model.id)})
    return result


def matches_case(case, reply):
    if reply.calls:
        return False
    actual, expected = reply.text.strip(), case['expected'].strip()
    if case['match'] == 'json':
        from .design_schema import parse_benchmark_json

        def equivalent(left, right):
            # Python otherwise considers True == 1, including inside containers.
            if isinstance(left, bool) or isinstance(right, bool):
                return type(left) is type(right) and left == right
            if isinstance(left, dict) or isinstance(right, dict):
                return (isinstance(left, dict) and isinstance(right, dict) and left.keys() == right.keys()
                        and all(equivalent(left[key], right[key]) for key in left))
            if isinstance(left, list) or isinstance(right, list):
                return (isinstance(left, list) and isinstance(right, list) and len(left) == len(right)
                        and all(equivalent(a, b) for a, b in zip(left, right)))
            return left == right

        try:
            return equivalent(parse_benchmark_json(actual), parse_benchmark_json(expected))
        except (ValueError, TypeError, RecursionError):
            return False
    return expected in actual if case['match'] == 'contains' else actual == expected


async def run_benchmark(model_id, data, actor):
    if not data.acknowledge:
        raise ValueError('Benchmark会调用真实模型并可能计费，请明确确认')
    with session() as s:
        model = s.get(ModelProfile, model_id)
        if not model or not model.config.get('enabled', True):
            raise ValueError('模型不存在或已停用')
        provider = s.get(Provider, model.config['provider_id'])
        if not provider or not provider.enabled:
            raise ValueError('供应商不存在或已停用')
        revision = s.get(ProviderRevision, provider.revision_id)
        suite = deepcopy(SUITE)
        if data.suite_ref:
            from .design_center import resolve_published
            from .design_schema import BenchmarkConfig
            published = resolve_published(s, data.suite_ref)
            if published['kind'] != 'benchmark':
                raise ValueError('请选择已发布的Benchmark套件')
            suite = {**BenchmarkConfig.model_validate(published['config']).model_dump(),
                     'id': published['id'], 'version': published['version'], 'digest': published['digest']}
        connection, config, encrypted = deepcopy(revision.config), deepcopy(model.config), revision.encrypted_key
        record = BenchmarkRecord(id=uid(), model_id=model.id, model_version=model.version,
                                 provider_revision=revision.id, config={'suite': suite, 'model': config}, actor=actor)
        s.add(record)
        record_id, provider_id, provider_rev = record.id, provider.id, revision.id
        emit(s, None, 'benchmark.started', {'actor': actor, 'benchmark_id': record_id, 'model_id': model.id})
    cases, latencies, calls, successes, input_tokens, output_tokens = [], [], 0, 0, 0, 0
    error, interrupted = '', False

    async def invoke(messages, definitions):
        nonlocal calls, successes, input_tokens, output_tokens
        with session() as s:
            live = s.get(Provider, provider_id)
            if not live or not live.enabled:
                raise providers.ProviderError('供应商已停用，Benchmark停止')
            live_model = s.get(ModelProfile, model_id)
            if not live_model or not live_model.config.get('enabled', True):
                raise providers.ProviderError('模型已停用，Benchmark停止')
        calls += 1
        started = time.monotonic()
        reply = await providers.complete(connection, config, messages, definitions, decrypt(encrypted), 30)
        successes += 1
        latencies.append((time.monotonic() - started) * 1000)
        input_tokens += max(0, reply.input_tokens)
        output_tokens += max(0, reply.output_tokens)
        return reply

    try:
        for case in suite['cases']:
            reply = await invoke([{'role': 'user', 'content': case['prompt']}], [])
            cases.append({'id': case['id'], 'dimension': case['dimension'], 'passed': matches_case(case, reply),
                          'latency_ms': round(latencies[-1], 2)})
        nonce = secrets.token_hex(10)
        tool = {'name': 'forge_benchmark_echo', 'description': 'Read-only benchmark. Echo the nonce.',
                'parameters': {'type': 'object', 'properties': {'nonce': {'type': 'string'}}, 'required': ['nonce'], 'additionalProperties': False}}
        messages = [{'role': 'user', 'content': f'Benchmark only. Call forge_benchmark_echo exactly once with nonce {nonce}. Do not answer in text.'}]
        first = await invoke(messages, [tool])
        valid = len(first.calls) == 1 and first.calls[0]['name'] == tool['name'] and first.calls[0]['arguments'] == {'nonce': nonce}
        cases.append({'id': 'tool-call', 'dimension': 'tool', 'passed': valid, 'latency_ms': round(latencies[-1], 2)})
        roundtrip = False
        if valid:
            assistant = {'role': 'assistant', 'content': first.text, 'calls': first.calls}
            if first.native is not None:
                assistant['anthropic_native'] = first.native
            messages.extend([assistant, {'role': 'tool', 'call_id': first.calls[0]['id'], 'content': json.dumps({'nonce': nonce, 'ok': True})},
                             {'role': 'user', 'content': 'The tool succeeded. Reply only OK and do not call a tool.'}])
            second = await invoke(messages, [])
            roundtrip = not second.calls and second.text.strip() == 'OK'
        cases.append({'id': 'tool-roundtrip', 'dimension': 'tool', 'passed': roundtrip})
    except asyncio.CancelledError:
        error, interrupted = 'Benchmark已中断，部分调用的费用或结果可能尚未确认；系统不会自动重放', True
    except (providers.ProviderError, ValueError) as exc:
        error = str(exc) if isinstance(exc, providers.ProviderError) else 'Benchmark响应格式无效'
    except Exception:
        error = 'Benchmark未完成；供应商响应不可用'
    scores = {}
    for dimension in ('coding', 'reasoning', 'tool'):
        selected = [c for c in cases if c['dimension'] == dimension]
        scores[dimension] = round(100 * sum(c['passed'] for c in selected) / len(selected), 2) if selected else None
    scores['reliability'] = round(100 * successes / calls, 2) if calls else None
    latency = sum(latencies) / len(latencies) if latencies else None
    scores['latency'] = round(max(0, 100 * (1 - latency / 30000)), 2) if latency is not None else None
    incost, outcost = config.get('input_cost_per_million'), config.get('output_cost_per_million')
    estimated_cost = (input_tokens * incost + output_tokens * outcost) / 1_000_000 if incost is not None and outcost is not None else None
    scores['cost'] = round(max(0, 100 - (incost + outcost) * 2.5), 2) if estimated_cost is not None else None
    results = {'scores': scores, 'cases': cases, 'requests': calls, 'successful_requests': successes,
               'input_tokens': input_tokens, 'output_tokens': output_tokens, 'mean_latency_ms': round(latency, 2) if latency else None,
               'estimated_cost': estimated_cost, 'error': error, 'finished_at': now(),
               'methodology': '问答/工具分=通过比例×100；速度=max(0,100×(1−平均延迟/30秒))；成本=max(0,100−每百万输入输出单价之和×2.5)，币种由管理员约定；成功率=成功HTTP请求/尝试数。仅衡量该套件。'}
    with session() as s:
        record = s.get(BenchmarkRecord, record_id)
        record.status, record.result = ('interrupted' if interrupted else 'failed' if error else 'completed'), results
        draft = None
        if not error:
            from .design_center import create_asset, set_generation
            draft = create_asset(s, 'capability', config['name'][:110] + ' · 评估画像',
                                 {'model_profile_id': model_id, 'benchmark_id': record_id,
                                  'system_scores': scores, 'overrides': {}, 'reason': ''}, actor, 'benchmark')
            draft = set_generation(s, draft['id'], draft['version'], {'method': 'benchmark', 'benchmark_id': record_id,
                                   'model_profile_id': model_id, 'model_version': record.model_version,
                                   'provider_revision': provider_rev, 'requests': calls})
        emit(s, None, 'benchmark.finished', {'actor': actor, 'benchmark_id': record_id, 'status': record.status,
                                           'requests': calls, 'input_tokens': input_tokens, 'output_tokens': output_tokens})
        output = {**benchmark_out(record), 'draft': draft}
    if interrupted:
        raise asyncio.CancelledError()
    return output


def ranked_candidates(s, data):
    profiles = effective_profiles(s)
    candidates = []
    for row in s.scalars(select(ModelProfile)):
        cfg = row.config
        provider = s.get(Provider, cfg['provider_id'])
        if not cfg.get('enabled', True) or not cfg.get('supports_tools') or not provider or not provider.enabled:
            continue
        revision = s.get(ProviderRevision, provider.revision_id)
        if not data.allow_external and revision.config['external']:
            continue
        if data.allowed_provider_ids and provider.id not in data.allowed_provider_ids:
            continue
        profile = profiles.get(row.id)
        scores = profile['final_scores'] if profile and not profile.get('stale') else {}
        candidates.append({'id': row.id, 'name': cfg['name'], 'provider_id': provider.id, 'scores': scores,
                           'profile_ref': {'id': profile['id'], 'version': profile['version']} if profile else None})
    return sorted(candidates, key=lambda c: (c['name'], c['id']))


def recommend_config(s, data):
    candidates = ranked_candidates(s, data)
    weights = {'planner': {'reasoning': .7, 'tool': .3}, 'developer': {'coding': .7, 'tool': .3},
               'tester': {'coding': .4, 'reasoning': .3, 'tool': .3}, 'reviewer': {'reasoning': .5, 'coding': .5}}
    reasons, assignments = [], {}
    for role, mapping in weights.items():
        def score(candidate):
            values = [(candidate['scores'].get(k), w) for k, w in mapping.items()]
            known = [(v, w) for v, w in values if v is not None]
            if not known:
                return -1
            quality = sum(v * w for v, w in known) / sum(w for _, w in known)
            cost = candidate['scores'].get('cost')
            return quality if cost is None or data.budget == 'high' else quality * .7 + cost * .3
        ranked = sorted(candidates, key=lambda c: -score(c))
        choice = ranked[0] if ranked else None
        assignments[role] = choice['id'] if choice else None
        reasons.append({'role': role, 'model_id': choice['id'] if choice else None,
                        'reason': ('依据已发布画像加权排序' if score(choice) >= 0 else '尚无已发布评估，按可用目录顺序提供待审核候选') if choice else '没有满足企业规则的已配置模型，请手工配置',
                        'capability_ref': choice.get('profile_ref') if choice else None})
    rules = {'allowed_provider_ids': data.allowed_provider_ids, 'allow_external': data.allow_external, 'require_tools': True,
             'max_model_calls': {'low': 30, 'medium': 100, 'high': 300}[data.budget]}
    routing = {'task_type': data.request[:120], 'assignments': assignments, 'budget': data.budget,
               'max_parallel': 3, 'rules': rules}
    if data.kind == 'routing':
        return routing, reasons, candidates
    members = []
    for i, (role, name, department, instructions) in enumerate([
        ('planner', 'Atlas', '架构', '分析需求，制定验收计划和工作分解。'),
        ('developer', 'Nova', '开发', '完成实现并提供可核对的代码证据。'),
        ('tester', 'Echo', '质量', '设计并运行回归验证，记录失败。'),
        ('reviewer', 'Sage', '审查', '独立检查代码、安全风险及验收证据。')]):
        members.append({'key': role, 'name': name, 'role': role, 'model_profile_id': assignments[role],
                        'fallback_model_ids': [], 'instructions': instructions, 'department': department,
                        'skills': [], 'communication': {'scope': 'run', 'recipients': []},
                        'reports_to': None if i == 0 else 'planner', 'tool_permissions': list(TOOLS_BY_ROLE[role]),
                        'office_position': {'x': -7 + i * 4, 'z': -4, 'floor': 1, 'zone': department}})
    return {'members': members}, reasons, candidates


def proposal_schema(kind, candidate_ids):
    """Portable, non-executable proposal tool. Publication still uses the full schema."""
    model_id = {'type': 'string', 'enum': candidate_ids} if candidate_ids else {'type': 'string'}
    if kind == 'routing':
        return {'type': 'object', 'properties': {
            'task_type': {'type': 'string'},
            'assignments': {'type': 'object', 'properties': {role: model_id for role in TOOLS_BY_ROLE},
                            'additionalProperties': False},
            'budget': {'type': 'string', 'enum': ['low', 'medium', 'high']},
            'max_parallel': {'type': 'integer', 'minimum': 1, 'maximum': 32},
            'description': {'type': 'string'}},
            'required': ['task_type', 'assignments'], 'additionalProperties': False}
    return {'type': 'object', 'properties': {
        'members': {'type': 'array', 'minItems': 1, 'maxItems': 30, 'items': {
            'type': 'object', 'properties': {
                'key': {'type': 'string'}, 'name': {'type': 'string'},
                'role': {'type': 'string', 'enum': list(TOOLS_BY_ROLE)},
                'model_profile_id': model_id,
                'instructions': {'type': 'string'}, 'department': {'type': 'string'},
                'reports_to': {'type': 'string', 'description': 'Another member key; omit for the team lead.'},
                'office_position': {'type': 'object', 'properties': {
                    'x': {'type': 'number', 'minimum': -12, 'maximum': 12},
                    'z': {'type': 'number', 'minimum': -8, 'maximum': 8},
                    'floor': {'type': 'integer', 'minimum': 1, 'maximum': 10},
                    'zone': {'type': 'string'}}, 'required': ['x', 'z', 'floor'], 'additionalProperties': False}},
            'required': ['key', 'name', 'role', 'model_profile_id', 'instructions'], 'additionalProperties': False}},
        'description': {'type': 'string'}}, 'required': ['members'], 'additionalProperties': False}


def parse_proposal(reply):
    if reply.calls:
        if len(reply.calls) != 1 or reply.calls[0].get('name') != 'propose_design':
            raise ValueError('仅允许一个设计提案，不执行模型返回的其他工具')
        proposed = reply.calls[0].get('arguments')
    else:
        raw = reply.text.strip()
        # Common providers wrap otherwise valid JSON in one Markdown fence. Never
        # search for a JSON substring or accept trailing instructions/extra objects.
        lines = raw.splitlines()
        if len(lines) >= 3 and lines[0].lower() in ('```json', '```') and lines[-1] == '```':
            raw = '\n'.join(lines[1:-1])
        proposed = json.loads(raw)
    if not isinstance(proposed, dict):
        raise ValueError('设计提案必须是JSON对象')
    return proposed


async def recommend(data, actor):
    with session() as s:
        config, reasons, candidates = recommend_config(s, data)
        skill_suggestions = [{'skill_id': sk.id, 'name': sk.name, 'version': sk.latest_version,
                              'status': (s.get(SkillRevision, f'{sk.id}:{sk.latest_version}').status if s.get(SkillRevision, f'{sk.id}:{sk.latest_version}') else 'unknown')}
                             for sk in s.scalars(select(Skill).where(Skill.enabled == True)).all()[:20]]
        generation = {'method': 'rules', 'reason': '基于已发布能力画像和企业约束的本地规则建议',
                      'reasons': reasons, 'skill_suggestions': skill_suggestions, 'requests': 0}
        model, revision, connection, secret = None, None, None, ''
        if data.model_profile_id:
            if not data.acknowledge:
                raise ValueError('AI设计会把本次需求和模型目录发送给所选模型并可能计费，请明确确认')
            model = s.get(ModelProfile, data.model_profile_id)
            if not model or not model.config.get('enabled', True):
                raise ValueError('设计助手模型不存在或已停用')
            provider = s.get(Provider, model.config['provider_id'])
            if not provider or not provider.enabled:
                raise ValueError('设计助手供应商已停用')
            revision = s.get(ProviderRevision, provider.revision_id)
            if revision.config['external'] and not data.allow_external:
                raise ValueError('需要明确允许把本次需求发送给外部设计助手')
            model_config, model_version = deepcopy(model.config), model.version
            connection, secret = deepcopy(revision.config), decrypt(revision.encrypted_key)
            model_revision = revision.id
    if data.model_profile_id:
        schema = proposal_schema(data.kind, [c['id'] for c in candidates])
        definitions = [{'name': 'propose_design', 'description': 'Submit exactly one editable design proposal for human review. This does not publish, create agents, execute tasks, or install anything.', 'parameters': schema}] if model_config.get('supports_tools') else []
        prompt = {'request': data.request, 'kind': data.kind, 'editable_template': config, 'candidate_models': candidates,
                  'proposal_schema': schema,
                  'instructions': 'Call propose_design exactly once with the proposed configuration. If function calls are unavailable, return only the configuration JSON object. Use the schema, candidate model IDs (not model names), and existing roles. Put concise reasons in description. Keep instructions concise. Do not install skills, grant extra tools or create runtime tasks. All output is an unapproved draft. Missing capability scores mean unknown; never claim unmeasured performance as fact.'}
        try:
            reply = await providers.complete(connection, model_config,
                                             [{'role': 'system', 'content': 'You assist a human organization designer. Recommend an editable configuration; the human has final authority.'},
                                              {'role': 'user', 'content': json.dumps(prompt, ensure_ascii=False)}], definitions, secret, 60)
        except providers.ProviderError as exc:
            with session() as s:
                emit(s, None, 'design.generation.failed', {'actor': actor, 'model_id': data.model_profile_id, 'error': str(exc)})
            raise HTTPException(502, str(exc)) from exc
        finally:
            secret = ''
        try:
            proposed = parse_proposal(reply)
            # AI cannot silently expand the user's provider/external rules.
            if data.kind == 'routing':
                proposed['rules'] = config['rules']
            from .design_schema import TeamConfig, RoutingConfig
            proposed = (RoutingConfig if data.kind == 'routing' else TeamConfig).model_validate(proposed).model_dump()
            allowed = {candidate['id'] for candidate in candidates}
            selected_ids = list(proposed['assignments'].values()) if data.kind == 'routing' else [mid for member in proposed['members'] for mid in [member.get('model_profile_id'), *member.get('fallback_model_ids', [])] if mid]
            if set(selected_ids) - allowed:
                raise ValueError('设计助手选择了目录外的模型')
            config = proposed
        except (ValueError, TypeError) as exc:
            with session() as s:
                emit(s, None, 'design.generation.failed', {'actor': actor, 'model_id': data.model_profile_id,
                    'error': 'invalid_proposal', 'validation_type': type(exc).__name__,
                    'response_chars': len(reply.text), 'tool_calls': len(reply.calls),
                    'input_tokens': reply.input_tokens, 'output_tokens': reply.output_tokens})
            raise HTTPException(502, 'AI未返回有效设计JSON；未创建或启用任何配置。请检查模型输出长度限制或换用支持结构化输出的模型。') from exc
        selected = config['assignments'].items() if data.kind == 'routing' else [(m['role'], m['model_profile_id']) for m in config['members']]
        generation = {**generation, 'method': 'ai', 'reason': '所选真实模型生成的未审核设计', 'requests': 1,
                      'rules_reasons': reasons,
                      'reasons': [{'role': role, 'model_id': mid, 'reason': 'AI提案中的模型分配；请结合方案说明和能力证据人工审查'} for role, mid in selected],
                      'model_profile_id': data.model_profile_id, 'model_version': model_version,
                      'provider_revision': model_revision, 'input_tokens': reply.input_tokens, 'output_tokens': reply.output_tokens}
    with session() as s:
        from .design_center import create_asset, set_generation
        draft = create_asset(s, data.kind, data.request[:70] + (' · 路由建议' if data.kind == 'routing' else ' · 团队方案'), config, actor,
                             'ai' if data.model_profile_id else 'manual')
        draft = set_generation(s, draft['id'], draft['version'], generation)
        emit(s, None, 'design.recommended', {'actor': actor, 'asset_id': draft['id'], 'generation': generation})
        return {**draft, 'generation': generation}


def register(app):
    from .api import admin, current_user

    @app.get('/api/model-hub')
    def overview(user=Depends(current_user)):
        with session() as s:
            return {'models': model_overview(s), 'dimensions': DIMENSIONS, 'default_suite': SUITE,
                    'benchmarks': [benchmark_out(b) for b in s.scalars(select(BenchmarkRecord).order_by(BenchmarkRecord.created_at.desc()).limit(100))]}

    @app.get('/api/model-hub/models/{model_id}/history')
    def history(model_id: str, user=Depends(current_user)):
        with session() as s:
            model = s.get(ModelProfile, model_id)
            if not model:
                raise HTTPException(404, '模型不存在')
            versions = [{'version': r.version, 'config': r.config, 'actor': r.actor, 'created_at': r.created_at}
                        for r in s.scalars(select(ModelRevision).where(ModelRevision.model_id == model_id).order_by(ModelRevision.version))]
            if not any(v['version'] == model.version for v in versions):
                versions.append({'version': model.version, 'config': model.config, 'actor': '', 'created_at': None})
            return {'model_id': model_id, 'versions': versions}

    @app.post('/api/model-hub/models/{model_id}/benchmark')
    async def benchmark(model_id: str, data: BenchmarkInput, user=Depends(admin),
                        idempotency_key: str | None = Header(None, min_length=1, max_length=160)):
        return await invoke_once('benchmark', user.id, {'model_id': model_id, **data.model_dump()}, idempotency_key,
                                 200, lambda: run_benchmark(model_id, data, user.id))

    @app.post('/api/model-hub/providers/{provider_id}/discover')
    async def discover(provider_id: str, user=Depends(admin)):
        from .provider_catalog import sync_catalog
        return await sync_catalog(provider_id, user.id)

    @app.post('/api/design/recommend', status_code=201)
    async def recommendation(data: RecommendInput, user=Depends(admin),
                             idempotency_key: str | None = Header(None, min_length=1, max_length=160)):
        return await invoke_once('recommend', user.id, data.model_dump(), idempotency_key,
                                 201, lambda: recommend(data, user.id))
