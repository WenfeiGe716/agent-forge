"""Provider-owned inventory. Reading or synchronizing never enables a model."""
from copy import deepcopy
import asyncio
import hashlib
import json
import secrets
from datetime import datetime, timezone

from fastapi import Depends, HTTPException
from pydantic import Field, model_validator
from sqlalchemy import JSON, String, Boolean, select, update, exists
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from . import providers
from .db import Base, Provider, ProviderRevision, ModelProfile, Agent, session, now, emit, uid
from .security import decrypt
from .schema import Strict, ModelInput

CATALOG_TTL_SECONDS = 3600


class ProviderCatalog(Base):
    __tablename__ = 'provider_model_catalogs'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(32), index=True)
    revision_id: Mapped[str] = mapped_column(String(32))
    entries: Mapped[list] = mapped_column(JSON, default=list)
    last_synced_at: Mapped[str] = mapped_column(String(40), default='')
    last_attempt_at: Mapped[str] = mapped_column(String(40), default='')
    error: Mapped[str] = mapped_column(String(500), default='')
    sync_token: Mapped[str] = mapped_column(String(32), default='')


class CatalogPullReceipt(Base):
    """Latest successful pull per connection; detailed history stays in audit events.

    Separate from the existing cache table so old deployments need no destructive
    migration or rewriting of their saved entries.
    """
    __tablename__ = 'provider_catalog_pull_receipts'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)


class CatalogModelState(Base):
    """Retire current-list entries reversibly, preserving IDs and version history."""
    __tablename__ = 'provider_catalog_model_states'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(32), index=True)
    retired: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)


def source_fields(row):
    return {key: deepcopy(row[key]) for key in ('id', 'name', 'capabilities', 'context_window') if key in row}


def catalog_digest(cache, revision_id):
    if not cache or not cache.last_synced_at:
        return ''
    rows = sorted((source_fields(row) for row in cache.entries if row.get('available') is True), key=lambda row: row['id'])
    raw = json.dumps({'revision_id': revision_id, 'models': rows}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def pull_changes(previous, current):
    changes = {key: [] for key in ('added', 'updated', 'removed', 'restored', 'unchanged')}
    old = {row['id']: row for row in previous}
    present = {row['id'] for row in current}
    for row in current:
        earlier = old.get(row['id'])
        key = ('added' if earlier is None else 'restored' if earlier.get('available') is False else
               'updated' if source_fields(earlier) != source_fields(row) else 'unchanged')
        changes[key].append(row['id'])
    changes['removed'] = [row['id'] for row in previous if row.get('available') is True and row['id'] not in present]
    return {key: sorted(value) for key, value in changes.items()}


def catalog_out(s, provider):
    cache = s.get(ProviderCatalog, f'{provider.id}:{provider.revision_id}')
    receipt = s.get(CatalogPullReceipt, f'{provider.id}:{provider.revision_id}')
    profiles = [m for m in s.scalars(select(ModelProfile)) if m.config['provider_id'] == provider.id]
    retired_ids = {state.id for state in s.scalars(select(CatalogModelState).where(
        CatalogModelState.provider_id == provider.id, CatalogModelState.retired.is_(True)))}
    active_profiles = [profile for profile in profiles if profile.id not in retired_ids]
    agents = list(s.scalars(select(Agent)))
    entries = {r['id']: deepcopy(r) for r in cache.entries} if cache else {}
    for profile in profiles:
        mid = profile.config['model_id']
        entry = entries.setdefault(mid, {'id': mid, 'label': mid, 'available': None if not cache or not cache.last_synced_at else False,
                                         'first_seen_at': '', 'last_seen_at': '', 'removed_at': ''})
        entry.setdefault('profiles', []).append({
            'id': profile.id, 'name': profile.config['name'], 'enabled': profile.config.get('enabled', True),
            'version': profile.version,
            'retired': profile.id in retired_ids,
            'primary_bindings': sum(a.config.get('model_profile_id') == profile.id for a in agents),
            'fallback_bindings': sum(profile.id in a.config.get('fallback_model_ids', []) for a in agents)})
    ordered = sorted(entries.values(), key=lambda row: row['id'])
    for row in ordered:
        row.setdefault('profiles', [])
    synced = cache.last_synced_at if cache else ''
    stale = not synced or (datetime.now(timezone.utc) - datetime.fromisoformat(synced)).total_seconds() >= CATALOG_TTL_SECONDS
    available = [r for r in ordered if r['available'] is True]
    return {'provider_id': provider.id, 'revision_id': provider.revision_id, 'entries': ordered,
            'catalog_digest': catalog_digest(cache, provider.revision_id),
            'last_pull': deepcopy(receipt.data) if receipt else None,
            'retired_profile_ids': sorted(retired_ids & {profile.id for profile in profiles}),
            'models': [{k: v for k, v in row.items() if k != 'profiles'} for row in available],
            'last_synced_at': synced, 'last_attempt_at': cache.last_attempt_at if cache else '',
            'error': cache.error if cache else '', 'stale': stale, 'ttl_seconds': CATALOG_TTL_SECONDS,
            'sync_status': 'syncing' if cache and cache.sync_token else 'error' if cache and cache.error else 'ready' if synced else 'never',
            'counts': {'available': len(available), 'configured': len(active_profiles),
                       'retired_configured': len(profiles) - len(active_profiles),
                       'unconfigured': sum(not any(not profile['retired'] for profile in r['profiles']) for r in available),
                       'missing': sum(r['available'] is False for r in ordered),
                       'enabled': sum(m.config.get('enabled', True) for m in active_profiles)},
            'installed': False, 'note': '目录属于当前供应商连接；目录同步不会创建模型配置、启用模型或绑定 Agent。'}


def read_catalog(provider_id):
    with session() as s:
        provider = s.get(Provider, provider_id)
        if not provider:
            raise HTTPException(404, '供应商不存在')
        return catalog_out(s, provider)


def reserve_sync(provider_id):
    with session() as s:
        provider = s.get(Provider, provider_id)
        if not provider or not provider.enabled:
            raise HTTPException(404, '供应商不存在或已停用')
        revision = s.get(ProviderRevision, provider.revision_id)
        revision_id = revision.id
        connection, secret = deepcopy(revision.config), decrypt(revision.encrypted_key)
        key = f'{provider_id}:{revision_id}'
        cache = s.get(ProviderCatalog, key)
        if not cache:
            cache = ProviderCatalog(id=key, provider_id=provider_id, revision_id=revision_id)
            s.add(cache)
            s.flush()
        token = uid()
        s.execute(update(ProviderCatalog).where(ProviderCatalog.id == key).values(
            sync_token=token, last_attempt_at=now(), error=''))
        return key, revision_id, token, connection, secret


async def sync_catalog(provider_id, actor):
    # The reservation commits before network I/O. The last request wins, even if
    # responses return out of order or the provider credentials change meanwhile.
    try:
        key, revision_id, token, connection, secret = reserve_sync(provider_id)
    except IntegrityError:
        key, revision_id, token, connection, secret = reserve_sync(provider_id)
    error = ''
    cancelled = False
    try:
        rows = await providers.discover(connection, secret)
    except providers.ProviderError as exc:
        error = str(exc).replace(secret, '[redacted]')[:500] if secret else str(exc)[:500]
    except asyncio.CancelledError:
        error, cancelled = '模型目录同步已中断，可重新同步；最后成功目录保留。', True
    except Exception:
        error = '模型目录同步失败；最后成功目录保留，请检查供应商连接。'
    finally:
        secret = ''
    stale = False
    with session() as s:
        provider = s.get(Provider, provider_id)
        cache = s.get(ProviderCatalog, key)
        stamp = now()
        changes = {'error': error, 'sync_token': ''}
        if not error and cache:
            changeset = pull_changes(cache.entries, rows)
            inventory = {r['id']: deepcopy(r) for r in cache.entries}
            present = {r['id'] for r in rows}
            for entry in inventory.values():
                if entry['id'] not in present:
                    entry['available'] = False
                    entry['removed_at'] = entry.get('removed_at') or stamp
            for row in rows:
                previous = inventory.get(row['id'], {})
                inventory[row['id']] = {**deepcopy(row), 'label': row.get('name', row['id']), 'available': True,
                                        'first_seen_at': previous.get('first_seen_at') or stamp,
                                        'last_seen_at': stamp, 'removed_at': ''}
            changes.update(entries=list(inventory.values()), last_synced_at=stamp)
        result = s.execute(update(ProviderCatalog).where(
            ProviderCatalog.id == key, ProviderCatalog.sync_token == token,
            exists().where(Provider.id == provider_id, Provider.revision_id == revision_id, Provider.enabled.is_(True))
        ).values(**changes))
        stale = result.rowcount != 1
        if not stale:
            report = None
            if not error:
                endpoint = (connection.get('protocol_config', {}).get('discovery') or {}).get('endpoint', '/models') if connection['protocol'] == 'custom_http' else '/models'
                report = {'status': 'success', 'request_url': connection['base_url'].rstrip('/') + endpoint,
                          'completed_at': stamp, 'received_count': len(rows), 'changes': changeset,
                          'counts': {key: len(value) for key, value in changeset.items()}}
                s.merge(CatalogPullReceipt(id=key, data=report))
            emit(s, None, 'provider.catalog.failed' if error else 'provider.catalog.synced', {
                'actor': actor, 'provider_id': provider_id, 'revision_id': revision_id,
                **({'error': error} if error else {'count': len(rows), 'pull': report})})
    if cancelled:
        raise asyncio.CancelledError()
    if stale:
        raise HTTPException(409, '连接或同步请求已更新，本次旧目录响应未采用。请读取当前目录。')
    if error:
        raise HTTPException(502, error)
    return read_catalog(provider_id)


class CatalogConfirmation(Strict):
    revision_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    catalog_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    acknowledged: bool = False

    @model_validator(mode='after')
    def confirm_changes(self):
        if not self.acknowledged:
            raise ValueError('请核对并确认上游目录及变更影响；新型号停用，需编辑配置后启用')
        return self


class CatalogImport(CatalogConfirmation):
    model_ids: list[str] = Field(min_length=1, max_length=2000)

    @model_validator(mode='after')
    def reviewed_selection(self):
        if len(set(self.model_ids)) != len(self.model_ids) or any(not mid.strip() or len(mid) > 200 for mid in self.model_ids):
            raise ValueError('所选型号必须是非空且不重复的有效模型ID')
        return self


def reviewed_catalog(s, provider_id, data):
    # One provider lock serializes imports/replacements and credential changes.
    locked = s.execute(update(Provider).where(Provider.id == provider_id,
        Provider.revision_id == data.revision_id, Provider.enabled.is_(True)).values(name=Provider.name))
    if not locked.rowcount:
        raise HTTPException(409, '供应商连接已变更或停用，请重新拉取并选择型号')
    provider = s.get(Provider, provider_id)
    key = f'{provider_id}:{data.revision_id}'
    s.execute(update(ProviderCatalog).where(ProviderCatalog.id == key).values(error=ProviderCatalog.error))
    cache = s.get(ProviderCatalog, key)
    if not cache or not cache.last_synced_at or cache.sync_token:
        raise HTTPException(409, '目录尚未完成拉取，请等待成功后重新确认')
    if not secrets.compare_digest(catalog_digest(cache, data.revision_id), data.catalog_digest):
        raise HTTPException(409, '上游目录内容已更新，请重新查看并确认选择')
    return provider, cache


def create_pending_profile(s, provider_id, candidate, actor):
    from .model_hub import record_model
    context = candidate.get('context_window')
    context = context if type(context) is int and 1 <= context <= 1_000_000_000 else None
    # Directory metadata cannot assert tool safety, prices or invocation options.
    draft = ModelInput(provider_id=provider_id, model_id=candidate['id'],
        name=(candidate.get('name') or candidate['id'])[:120], enabled=False, supports_tools=False,
        allowed_parameters=[], parameters={}, context_window=context)
    model = ModelProfile(id=uid(), config=draft.model_dump())
    s.add(model); s.flush()
    record_model(s, model, actor)
    return model


def restore_profile(s, model):
    state = s.get(CatalogModelState, model.id)
    if state and state.provider_id == model.config['provider_id'] and state.retired:
        state.retired, state.updated_at = False, now()
        return True
    return False


def import_selected(provider_id, data, actor):
    from .api import config_out
    with session() as s:
        provider, cache = reviewed_catalog(s, provider_id, data)
        candidates = {row['id']: row for row in cache.entries if row.get('available') is True}
        if any(mid not in candidates for mid in data.model_ids):
            raise ValueError('所选型号不在当前供应商的上游目录中，请重新拉取并选择')
        profiles = [m for m in s.scalars(select(ModelProfile)) if m.config['provider_id'] == provider_id]
        created, skipped, restored = [], [], []
        for mid in data.model_ids:
            matching = [model for model in profiles if model.config['model_id'] == mid]
            if matching:
                recovered = [config_out(model) for model in matching if restore_profile(s, model)]
                restored.extend(recovered)
                if not recovered:
                    skipped.append(mid)
                continue
            model = create_pending_profile(s, provider_id, candidates[mid], actor)
            created.append(config_out(model))
            profiles.append(model)
        s.flush()
        emit(s, None, 'provider.catalog.imported', {'actor': actor, 'provider_id': provider_id,
            'revision_id': data.revision_id, 'catalog_digest': data.catalog_digest,
            'selected_model_ids': data.model_ids, 'created_profile_ids': [row['id'] for row in created],
            'restored_profile_ids': [row['id'] for row in restored],
            'skipped_model_ids': skipped, 'enabled': False})
        return {'created': created, 'skipped': skipped, 'restored': restored, 'catalog': catalog_out(s, provider)}


def replace_current(provider_id, data, actor):
    from .api import config_out
    from .model_hub import record_model
    with session() as s:
        provider, cache = reviewed_catalog(s, provider_id, data)
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(cache.last_synced_at)).total_seconds()
        if cache.error or age > 300:
            raise HTTPException(409, '全量替换需要5分钟内成功拉取的完整目录，请重新从上游拉取')
        candidates = {row['id']: row for row in cache.entries if row.get('available') is True}
        if not candidates:
            raise ValueError('上游目录为空，为防止异常清空，不能执行全量替换；已有配置保留')
        profiles = [m for m in s.scalars(select(ModelProfile)) if m.config['provider_id'] == provider_id]
        known = {model.config['model_id'] for model in profiles}
        created, retired, restored, kept = [], [], [], []
        for model in profiles:
            if model.config['model_id'] in candidates:
                (restored if restore_profile(s, model) else kept).append(config_out(model))
                continue
            state = s.get(CatalogModelState, model.id)
            changed = not state or state.provider_id != provider_id or not state.retired
            if model.config.get('enabled', True):
                record_model(s, model, actor)
                model.config = {**deepcopy(model.config), 'enabled': False}
                model.version += 1
                record_model(s, model, actor)
                changed = True
            s.merge(CatalogModelState(id=model.id, provider_id=provider_id, retired=True, updated_at=now()))
            if changed:
                retired.append(config_out(model))
        for mid in sorted(candidates):
            if mid not in known:
                created.append(config_out(create_pending_profile(s, provider_id, candidates[mid], actor)))
        s.flush()
        emit(s, None, 'provider.catalog.replaced', {'actor': actor, 'provider_id': provider_id,
            'revision_id': data.revision_id, 'catalog_digest': data.catalog_digest,
            'upstream_count': len(candidates), 'created_profile_ids': [m['id'] for m in created],
            'retired_profile_ids': [m['id'] for m in retired], 'restored_profile_ids': [m['id'] for m in restored],
            'kept_profile_ids': [m['id'] for m in kept], 'bindings_changed': False})
        return {'created': created, 'retired': retired, 'restored': restored, 'kept': kept,
                'catalog': catalog_out(s, provider)}


def register(app):
    from .api import admin, current_user

    @app.get('/api/providers/{provider_id}/catalog')
    def get_catalog(provider_id: str, user=Depends(current_user)):
        return read_catalog(provider_id)

    @app.post('/api/providers/{provider_id}/catalog/sync')
    async def synchronize(provider_id: str, user=Depends(admin)):
        return await sync_catalog(provider_id, user.id)

    @app.post('/api/providers/{provider_id}/catalog/import', status_code=201)
    def import_models(provider_id: str, data: CatalogImport, user=Depends(admin)):
        return import_selected(provider_id, data, user.id)

    @app.post('/api/providers/{provider_id}/catalog/replace', status_code=201)
    def replace_models(provider_id: str, data: CatalogConfirmation, user=Depends(admin)):
        return replace_current(provider_id, data, user.id)
