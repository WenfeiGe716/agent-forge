"""Human-reviewed configuration revisions, distinct from executable runtime state.

Revision content is append-only. Publishing only grants eligibility for future
runs; even a published team needs an explicit instantiation operation.
"""
from copy import deepcopy
import hashlib
import json
import secrets

from fastapi import Depends, HTTPException
from sqlalchemy import Integer, JSON, String, Text, UniqueConstraint, select, update
from sqlalchemy.orm import Mapped, mapped_column

from .db import Agent, Base, ModelProfile, Provider, ProviderRevision, emit, now, session, uid
from .design_schema import (
    AssetConfirmation, AssetCreate, AssetEdit, AssetKind, BenchmarkConfig,
    OfficeConfig, RoutingConfig, TeamConfig, WorkflowConfig,
)
from .schema import AgentInput, TOOLS_BY_ROLE

KINDS = {'capability', 'routing', 'team', 'office', 'workflow', 'benchmark'}
SOURCES = {'manual', 'ai', 'benchmark'}


class DesignAsset(Base):
    __tablename__ = 'design_assets'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    latest_version: Mapped[int] = mapped_column(Integer, default=1)
    published_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lock_version: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class DesignRevision(Base):
    __tablename__ = 'design_revisions'
    __table_args__ = (UniqueConstraint('asset_id', 'version'),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(120))
    config: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(24))
    generation: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default='draft')
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    published_by: Mapped[str] = mapped_column(String(32), default='')
    published_at: Mapped[str] = mapped_column(String(40), default='')
    revoked_by: Mapped[str] = mapped_column(String(32), default='')
    revoked_at: Mapped[str] = mapped_column(String(40), default='')


class DesignAudit(Base):
    __tablename__ = 'design_audit'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    asset_id: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer)
    digest: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(24))
    actor: Mapped[str] = mapped_column(String(32))
    comment: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[str] = mapped_column(String(40), default=now)


def checksum(kind, name, config):
    try:
        encoded = json.dumps({'kind': kind, 'name': name, 'config': config},
                             ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError('设计配置必须是有限数值组成的JSON对象') from exc
    if len(encoded.encode()) > 400_000:
        raise ValueError('设计配置超过400KB')
    return hashlib.sha256(encoded.encode()).hexdigest()


def revision_out(asset, revision):
    return {
        'id': asset.id, 'kind': asset.kind, 'name': revision.name,
        'version': revision.version, 'latest_version': asset.latest_version,
        'published_version': asset.published_version, 'digest': revision.digest,
        'config': deepcopy(revision.config), 'status': revision.status,
        'source': revision.source, 'created_by': revision.created_by,
        'generation': deepcopy(revision.generation or {}),
        'created_at': revision.created_at, 'published_by': revision.published_by,
        'published_at': revision.published_at, 'revoked_by': revision.revoked_by,
        'revoked_at': revision.revoked_at,
    }


def _audit(s, asset, revision, action, actor, comment=''):
    s.add(DesignAudit(asset_id=asset.id, version=revision.version, digest=revision.digest,
                      action=action, actor=actor, comment=comment))
    emit(s, None, 'design.' + action, {
        'actor': actor, 'asset_id': asset.id, 'kind': asset.kind,
        'version': revision.version, 'digest': revision.digest, 'comment': comment,
        'source': revision.source,
    })


def create_asset(s, kind, name, config, actor, source='manual'):
    """Trusted server helper. API callers may only create manual provenance."""
    if kind not in KINDS or source not in SOURCES:
        raise ValueError('未知设计类型或建议来源')
    if not isinstance(name, str) or not name.strip() or len(name) > 120 or not isinstance(config, dict):
        raise ValueError('设计需要1至120字符名称和JSON配置对象')
    digest = checksum(kind, name, config)
    asset = DesignAsset(kind=kind, created_by=actor)
    s.add(asset)
    s.flush()
    revision = DesignRevision(id=f'{asset.id}:1', asset_id=asset.id, version=1,
                              name=name, config=deepcopy(config), digest=digest,
                              source=source, created_by=actor)
    s.add(revision)
    s.flush()
    _audit(s, asset, revision, 'proposed', actor)
    return revision_out(asset, revision)


def _get(s, asset_id, version=None):
    asset = s.get(DesignAsset, asset_id)
    if not asset:
        raise HTTPException(404, '设计配置不存在')
    revision = s.get(DesignRevision, f'{asset.id}:{version or asset.latest_version}')
    if not revision:
        raise HTTPException(404, '设计版本不存在')
    return asset, revision


def set_generation(s, asset_id, version, metadata):
    """Attach bounded server-generated evidence once; never expose a writable API."""
    if not isinstance(metadata, dict):
        raise ValueError('生成依据必须是JSON对象')
    checksum('generation', 'provenance', metadata)
    asset, revision = _get(s, asset_id, version)
    if revision.status != 'draft':
        raise ValueError('已发布版本的生成依据不可修改')
    if revision.generation and revision.generation != metadata:
        raise ValueError('已记录的生成依据不可覆盖')
    revision.generation = deepcopy(metadata)
    s.flush()
    return revision_out(asset, revision)


def _lock(s, asset_id):
    # A write lock serializes revision appends, approvals and revocations.
    changed = s.execute(update(DesignAsset).where(DesignAsset.id == asset_id)
                        .values(lock_version=DesignAsset.lock_version + 1)
                        .execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise HTTPException(404, '设计配置不存在')
    s.expire_all()


def _check_digest(asset, revision, expected=None):
    actual = checksum(asset.kind, revision.name, revision.config)
    if not secrets.compare_digest(actual, revision.digest):
        raise HTTPException(409, '设计内容校验失败，不能发布或执行')
    if expected is not None and not secrets.compare_digest(revision.digest, expected):
        raise HTTPException(409, '确认摘要与当前查看的版本不一致，请刷新后复核')


def edit_asset(s, asset_id, data, actor):
    _lock(s, asset_id)
    asset, previous = _get(s, asset_id)
    if asset.latest_version != data.expected_version:
        raise HTTPException(409, '设计版本冲突，请刷新后再编辑')
    if not data.name.strip():
        raise ValueError('设计名称不能为空')
    version = asset.latest_version + 1
    revision = DesignRevision(id=f'{asset.id}:{version}', asset_id=asset.id, version=version,
                              name=data.name, config=deepcopy(data.config),
                              digest=checksum(asset.kind, data.name, data.config),
                              source='manual', created_by=actor,
                              generation=({**deepcopy(previous.generation), 'edited': True,
                                           'origin_version': previous.generation.get('origin_version', previous.version)}
                                          if previous.generation else {}))
    asset.latest_version = version
    s.add(revision)
    s.flush()
    _audit(s, asset, revision, 'revised', actor)
    return revision_out(asset, revision)


def _model(s, model_id, *, require_tools=False, allow_external=True, allowed_providers=None, parameters=None):
    from .providers import validate_parameters
    model = s.get(ModelProfile, model_id)
    if not model or not model.config.get('enabled', True):
        raise ValueError('设计引用的模型不存在或已停用')
    provider = s.get(Provider, model.config['provider_id'])
    if not provider or not provider.enabled:
        raise ValueError('设计引用的供应商不存在或已停用')
    connection = s.get(ProviderRevision, provider.revision_id)
    if not connection:
        raise ValueError('供应商配置版本不存在')
    if allowed_providers and provider.id not in allowed_providers:
        raise ValueError('模型供应商不在策略白名单中')
    if connection.config.get('external', True) and not allow_external:
        raise ValueError('策略禁止使用外部模型')
    if require_tools and not model.config.get('supports_tools', False):
        raise ValueError('此设计需要支持工具调用的模型')
    effective = deepcopy(model.config.get('parameters', {})) | (parameters or {})
    if connection.config['protocol'] == 'custom_http':
        validate_parameters(connection.config['protocol'], model.config['allowed_parameters'],
                            effective, connection.config.get('protocol_config'))
    else:
        validate_parameters(connection.config['protocol'], model.config['allowed_parameters'], effective)
    return model


def validate_config(s, kind, config):
    """Validate at publication and again before any new runtime can consume it."""
    if kind == 'capability':
        from .model_hub import validate_capability
        validate_capability(s, config)
    elif kind == 'routing':
        parsed = RoutingConfig.model_validate(config)
        for provider_id in parsed.rules.allowed_provider_ids:
            provider = s.get(Provider, provider_id)
            if not provider or not provider.enabled:
                raise ValueError('策略供应商白名单包含不存在或停用的供应商')
        for model_id in parsed.assignments.values():
            _model(s, model_id, require_tools=parsed.rules.require_tools,
                   allow_external=parsed.rules.allow_external,
                   allowed_providers=parsed.rules.allowed_provider_ids)
    elif kind == 'team':
        from .skills import freeze_bindings
        parsed = TeamConfig.model_validate(config)
        for member in parsed.members:
            if member.role_template_ref:
                from .role_templates import validate_reference
                validate_reference(s, member.role_template_ref.model_dump())
            if not member.model_profile_id:
                raise ValueError(f'{member.name} 尚未选择模型，请完善后发布')
            models = [member.model_profile_id, *member.fallback_model_ids]
            if len(set(models)) != len(models):
                raise ValueError('成员主模型和回退模型不能重复')
            for model_id in models:
                _model(s, model_id, require_tools=True, parameters=member.model_parameters)
            freeze_bindings(s, [binding.model_dump() for binding in member.skills])
    elif kind == 'office':
        from .organization import validate_members
        validate_members(s, config)
    elif kind == 'workflow':
        parsed = WorkflowConfig.model_validate(config)
        for agent_id in parsed.graph.participants:
            agent = s.get(Agent, agent_id)
            if not agent or not agent.config.get('enabled', True):
                raise ValueError('编排包含不存在或停用的成员')
    elif kind == 'benchmark':
        BenchmarkConfig.model_validate(config)
    else:
        raise ValueError('未知设计类型')


def resolve_published(s, ref):
    if not isinstance(ref, dict) or set(ref) - {'id', 'version', 'digest'}:
        raise ValueError('设计引用需要明确的id和version')
    if not ref.get('id') or type(ref.get('version')) is not int or ref['version'] < 1:
        raise ValueError('设计引用需要明确的id和正整数version')
    asset, revision = _get(s, ref['id'], ref['version'])
    _check_digest(asset, revision, ref.get('digest'))
    if revision.status != 'published':
        raise ValueError('设计版本未发布或已撤销，不能用于新的运行')
    validate_config(s, asset.kind, revision.config)
    return {key: value for key, value in revision_out(asset, revision).items()
            if key in ('id', 'kind', 'name', 'version', 'digest', 'config')}


def published_assets(s, kind):
    """Active default publications; invalid references are ineligible to recommend."""
    if kind not in KINDS:
        raise ValueError('未知设计类型')
    result = []
    for asset in s.scalars(select(DesignAsset).where(DesignAsset.kind == kind,
                                                     DesignAsset.published_version.is_not(None))):
        try:
            result.append(resolve_published(s, {'id': asset.id, 'version': asset.published_version}))
        except (ValueError, HTTPException):
            continue
    return result


def review_asset(s, asset_id, data, actor, action):
    _lock(s, asset_id)
    asset, revision = _get(s, asset_id, data.version)
    _check_digest(asset, revision, data.digest)
    if action == 'publish':
        if revision.status == 'revoked':
            raise HTTPException(409, '已撤销版本不能重新发布，请创建新修订')
        validate_config(s, asset.kind, revision.config)
        if revision.status == 'published':
            return revision_out(asset, revision)
        revision.status = 'published'
        revision.published_by, revision.published_at = actor, now()
        asset.published_version = max(asset.published_version or 0, revision.version)
    elif action == 'revoke':
        if revision.status == 'revoked':
            return revision_out(asset, revision)
        if revision.status != 'published':
            raise HTTPException(409, '只有已发布版本可以撤销')
        revision.status = 'revoked'
        revision.revoked_by, revision.revoked_at = actor, now()
        s.flush()
        asset.published_version = s.scalar(select(DesignRevision.version).where(
            DesignRevision.asset_id == asset.id, DesignRevision.status == 'published'
        ).order_by(DesignRevision.version.desc()).limit(1))
    else:
        raise ValueError('未知审批操作')
    s.flush()
    _audit(s, asset, revision, 'published' if action == 'publish' else 'revoked', actor, data.comment)
    return revision_out(asset, revision)


def instantiate_team(s, asset_id, data, actor):
    _lock(s, asset_id)
    resolved = resolve_published(s, {'id': asset_id, 'version': data.version, 'digest': data.digest})
    if resolved['kind'] != 'team':
        raise ValueError('只有团队模板可以创建运行成员')
    parsed = TeamConfig.model_validate(resolved['config'])
    mapping = {member.key: uid() for member in parsed.members}
    instance_id = uid()
    for member in parsed.members:
        config = member.model_dump(exclude={'key', 'office_position', 'tool_permissions'})
        config['reports_to'] = mapping.get(member.reports_to)
        config['communication']['recipients'] = [mapping[key] for key in member.communication.recipients]
        config = AgentInput.model_validate(config).model_dump()
        config['design_ref'] = {'id': asset_id, 'version': data.version,
                                'digest': data.digest, 'member_key': member.key,
                                'instance_id': instance_id}
        config['tool_permissions'] = (member.tool_permissions if member.tool_permissions is not None
                                      else [*TOOLS_BY_ROLE[member.role], 'request_decision'])
        if member.office_position is not None:
            config['office_position'] = member.office_position.model_dump()
        s.add(Agent(id=mapping[member.key], config=config))
    s.flush()
    asset, revision = _get(s, asset_id, data.version)
    _audit(s, asset, revision, 'instantiated', actor, data.comment)
    workflow = parsed.workflow.model_dump() if parsed.workflow else None
    if workflow:
        workflow['participants'] = [mapping[key] for key in workflow['participants']]
        if workflow.get('assessor_id'):
            workflow['assessor_id'] = mapping[workflow['assessor_id']]
    emit(s, None, 'team.instantiated', {'actor': actor, 'team_id': asset_id,
         'version': data.version, 'digest': data.digest, 'member_mapping': mapping,
         'instance_id': instance_id})
    return {'agent_ids': list(mapping.values()), 'member_mapping': mapping,
            'team_ref': {'id': asset_id, 'version': data.version},
            'digest': data.digest, 'workflow': workflow, 'instance_id': instance_id}


def register(app):
    from .api import admin, current_user

    @app.get('/api/design/assets')
    def list_assets(kind: AssetKind | None = None, user=Depends(current_user)):
        with session() as s:
            query = select(DesignAsset).order_by(DesignAsset.created_at.desc(), DesignAsset.id)
            if kind:
                query = query.where(DesignAsset.kind == kind)
            return [revision_out(asset, s.get(DesignRevision, f'{asset.id}:{asset.latest_version}'))
                    for asset in s.scalars(query)]

    @app.post('/api/design/assets', status_code=201)
    def create(data: AssetCreate, user=Depends(admin)):
        with session() as s:
            return create_asset(s, data.kind, data.name, data.config, user.id, 'manual')

    @app.get('/api/design/assets/{asset_id}')
    def detail(asset_id: str, user=Depends(current_user)):
        with session() as s:
            asset, revision = _get(s, asset_id)
            result = revision_out(asset, revision)
            result['versions'] = [revision_out(asset, item) for item in s.scalars(
                select(DesignRevision).where(DesignRevision.asset_id == asset.id)
                .order_by(DesignRevision.version.desc()))]
            result['audit'] = [
                {field: getattr(item, field) for field in ('id', 'version', 'digest', 'action', 'actor', 'comment', 'created_at')}
                for item in s.scalars(select(DesignAudit).where(DesignAudit.asset_id == asset.id)
                                     .order_by(DesignAudit.created_at, DesignAudit.id))]
            return result

    @app.get('/api/design/assets/{asset_id}/versions/{version}')
    def version(asset_id: str, version: int, user=Depends(current_user)):
        if version < 1:
            raise HTTPException(422, '版本必须是正整数')
        with session() as s:
            return revision_out(*_get(s, asset_id, version))

    @app.put('/api/design/assets/{asset_id}')
    def edit(asset_id: str, data: AssetEdit, user=Depends(admin)):
        with session() as s:
            return edit_asset(s, asset_id, data, user.id)

    @app.post('/api/design/assets/{asset_id}/publish')
    def publish(asset_id: str, data: AssetConfirmation, user=Depends(admin)):
        with session() as s:
            return review_asset(s, asset_id, data, user.id, 'publish')

    @app.post('/api/design/assets/{asset_id}/revoke')
    def revoke(asset_id: str, data: AssetConfirmation, user=Depends(admin)):
        with session() as s:
            return review_asset(s, asset_id, data, user.id, 'revoke')

    @app.post('/api/design/assets/{asset_id}/instantiate', status_code=201)
    def instantiate(asset_id: str, data: AssetConfirmation, user=Depends(admin)):
        with session() as s:
            return instantiate_team(s, asset_id, data, user.id)
