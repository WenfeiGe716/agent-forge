"""Provider-scoped, reviewed model-state changes; never discover or rebind models."""
from copy import deepcopy
import hashlib
import json

from fastapi import Depends, HTTPException
from pydantic import Field, StrictBool, model_validator
from sqlalchemy import select, update

from .db import Agent, Event, ModelProfile, Provider, ProviderRevision, emit, session, uid
from .model_hub import record_model
from .provider_catalog import CatalogModelState, catalog_out
from .model_compatibility import model_errors
from .schema import Strict

EVENT_KIND = 'provider.models.bulk-state'


class Selection(Strict):
    id: str = Field(pattern=r'^[a-f0-9]{32}$')
    version: int = Field(ge=1, strict=True)


class BulkStatePreview(Strict):
    revision_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    selections: list[Selection] = Field(min_length=1, max_length=2000)
    enabled: StrictBool

    @model_validator(mode='after')
    def distinct(self):
        if len({item.id for item in self.selections}) != len(self.selections):
            raise ValueError('模型配置选择不能重复')
        return self


class BulkStateConfirmation(BulkStatePreview):
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    acknowledged: StrictBool = False


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _review(s, provider_id, data):
    provider = s.get(Provider, provider_id)
    if not provider:
        raise HTTPException(404, '供应商不存在')
    if provider.revision_id != data.revision_id:
        raise HTTPException(409, '供应商连接修订已改变，请刷新后重新预览')
    if data.enabled and not provider.enabled:
        raise HTTPException(409, '供应商连接已停用；可批量停用模型，但不能批量启用')
    connection = s.get(ProviderRevision, provider.revision_id)
    if not connection:
        raise HTTPException(409, '供应商连接修订不存在')
    agents = list(s.scalars(select(Agent).order_by(Agent.id)))
    rows, items, errors = [], [], []
    for selected in sorted(data.selections, key=lambda item: item.id):
        model = s.get(ModelProfile, selected.id)
        if not model or model.config.get('provider_id') != provider.id:
            raise HTTPException(409, '所选模型不存在或不属于当前供应商，请刷新后重新选择')
        if model.version != selected.version:
            raise HTTPException(409, '所选模型配置版本已改变，请刷新后重新预览')
        state = s.get(CatalogModelState, model.id)
        retired = bool(state and state.provider_id == provider.id and state.retired)
        if data.enabled and retired:
            raise HTTPException(409, '所选配置已从供应商列表移出；请先拉取上游并明确确认恢复，批量启用不会恢复配置')
        cfg = model.config
        if data.enabled:
            errors.extend(model_errors(cfg, connection.config, model.id))
        def bound(agent):
            return {'id': agent.id, 'name': agent.config.get('name', agent.id), 'version': agent.version}
        items.append({'id': model.id, 'version': model.version, 'name': cfg.get('name', model.id),
                      'model_id': cfg['model_id'], 'enabled': cfg.get('enabled', True),
                      'target_enabled': data.enabled, 'supports_tools': cfg.get('supports_tools', False),
                      'retired': retired, 'config_digest': _digest(cfg),
                      'primary': [bound(a) for a in agents if a.config.get('model_profile_id') == model.id],
                      'fallback': [bound(a) for a in agents if model.id in a.config.get('fallback_model_ids', [])]})
        rows.append(model)
    if errors:
        raise ValueError('批量启用未执行：以下模型配置不兼容，请逐项编辑后重新预览；本次没有修改任何模型。\n'
                         + '\n'.join(errors))
    changed = sum(item['enabled'] != data.enabled for item in items)
    review = {'provider_id': provider.id, 'provider_name': provider.name,
              'revision_id': provider.revision_id, 'provider_enabled': provider.enabled,
              'enabled': data.enabled, 'selections': [{'id': row.id, 'version': row.version} for row in rows],
              'items': items, 'counts': {'selected': len(items), 'changed': changed,
                  'unchanged': len(items) - changed,
                  'primary_bindings': sum(len(item['primary']) for item in items),
                  'fallback_bindings': sum(len(item['fallback']) for item in items),
                  'unconfirmed_tools': sum(not item['supports_tools'] for item in items)}}
    return provider, rows, {**review, 'digest': _digest(review)}


def preview(provider_id, data):
    with session() as s:
        return _review(s, provider_id, data)[2]


def apply_state(provider_id, data, actor):
    if not data.acknowledged:
        raise HTTPException(422, '请明确确认批量模型状态调整的影响')
    with session() as s:
        # Same write lock as catalog replacement and single-model edits. Read
        # versions, retirement and bindings only after acquiring this lock.
        s.execute(update(Provider).where(Provider.id == provider_id).values(name=Provider.name))
        s.expire_all()
        provider, rows, review = _review(s, provider_id, data)
        if review['digest'] != data.digest:
            raise HTTPException(409, '配置或 Agent 绑定影响已改变，请重新预览并确认；本次没有修改任何模型')
        operation_id, changed, unchanged, changes = uid(), [], [], []
        for row in rows:
            if row.config.get('enabled', True) == data.enabled:
                unchanged.append({'id': row.id, 'version': row.version, **deepcopy(row.config)})
                continue
            record_model(s, row, actor)
            old_version = row.version
            row.config = {**deepcopy(row.config), 'enabled': data.enabled}
            row.version += 1
            s.flush()
            record_model(s, row, actor)
            changed.append({'id': row.id, 'version': row.version, **deepcopy(row.config)})
            changes.append({'model_id': row.id, 'name': row.config.get('name', row.id),
                            'from_version': old_version, 'to_version': row.version,
                            'was_enabled': not data.enabled, 'enabled': data.enabled})
            emit(s, None, 'model.updated', {'actor': actor, 'model_id': row.id,
                                         'version': row.version, 'operation_id': operation_id})
        receipt = {'operation_id': operation_id, 'actor': actor, 'provider_id': provider.id,
                   'revision_id': provider.revision_id, 'digest': data.digest, 'enabled': data.enabled,
                   'selected_model_ids': [row.id for row in rows], 'changes': changes,
                   'unchanged_model_ids': [row['id'] for row in unchanged], 'counts': review['counts'],
                   'bindings_changed': False, 'capabilities_changed': False}
        emit(s, None, EVENT_KIND, receipt)
        return {'changed': changed, 'unchanged': unchanged, 'receipt': receipt,
                'catalog': catalog_out(s, provider)}


def register(app):
    from .api import admin, current_user

    @app.post('/api/providers/{provider_id}/models/bulk-state/preview')
    def review_models(provider_id: str, data: BulkStatePreview, user=Depends(admin)):
        return preview(provider_id, data)

    @app.post('/api/providers/{provider_id}/models/bulk-state')
    def change_models(provider_id: str, data: BulkStateConfirmation, user=Depends(admin)):
        return apply_state(provider_id, data, user.id)

    @app.get('/api/providers/{provider_id}/models/bulk-state/history')
    def history(provider_id: str, user=Depends(current_user)):
        with session() as s:
            if not s.get(Provider, provider_id):
                raise HTTPException(404, '供应商不存在')
            rows = s.scalars(select(Event).where(Event.kind == EVENT_KIND,
                Event.data['provider_id'].as_string() == provider_id).order_by(Event.id.desc()).limit(30))
            return {'items': [{**deepcopy(row.data), 'event_id': row.id, 'created_at': row.created_at} for row in rows]}
