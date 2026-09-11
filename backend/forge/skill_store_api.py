"""Authenticated Skill Store routes; uses the existing Skill review/runtime pipeline."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Literal

import httpx
from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from . import skills, skill_store as store
from .db import Agent, Run, Skill, SkillRevision, emit, now, session


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class SourceInput(Strict):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal['github', 'archive'] = 'github'
    repository: str = ''
    ref: str = Field('main', min_length=1, max_length=180)
    expected_version: int | None = Field(None, ge=1)


class RevisionRequest(Strict):
    revision_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    expected_version: int | None = Field(None, ge=1)
    reviewed_source: bool = False


class BindRequest(Strict):
    revision_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    agent_id: str = Field(min_length=1, max_length=32)
    expected_agent_version: int = Field(ge=1)
    mode: Literal['always', 'on_demand'] = 'on_demand'
    parameters: dict = Field(default_factory=dict)


class SyncRequest(Strict):
    expected_version: int = Field(ge=1)


class EnabledRequest(SyncRequest):
    enabled: bool


class PublishRequest(Strict):
    skill_id: str = Field(min_length=1, max_length=32)
    version: int = Field(ge=1)
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    title: str = Field(min_length=1, max_length=160)
    category: str = Field('team', min_length=1, max_length=60)


def source_out(s, source):
    items = list(s.scalars(select(store.StoreItem).where(store.StoreItem.source_id == source.id)))
    counts = {'skill': 0, 'role_template': 0}
    for item in items:
        revision = s.get(store.StoreRevision, item.latest_revision)
        if item.available and revision and store.revision_kind(revision) in counts:
            counts[store.revision_kind(revision)] += 1
    return {'id': source.id, **source.config, 'version': source.version, 'enabled': source.enabled,
            'last_sync': source.last_sync, 'resolved_ref': source.resolved_ref, 'last_error': source.last_error,
            'summary': source.summary, 'count': sum(i.available for i in items), 'counts': counts}


def get_item(s, item_id, revision_id=None, *, native_only=True):
    item = s.get(store.StoreItem, item_id)
    if not item:
        raise HTTPException(404, '商店条目不存在')
    revision = s.get(store.StoreRevision, revision_id or item.latest_revision)
    if not revision or revision.item_id != item.id:
        raise HTTPException(404, '版本不属于此条目')
    if native_only:
        store.require_native_revision(revision)
    return item, revision


def register(app):
    from .api import current_user, admin

    @app.exception_handler(store.StoreConflict)
    async def conflict(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409, content={'detail': str(exc)})

    @app.get('/api/skill-store/sources')
    def sources(user=Depends(current_user)):
        with session() as s:
            return [source_out(s, row) for row in s.scalars(select(store.StoreSource).order_by(store.StoreSource.id))]

    @app.post('/api/skill-store/sources', status_code=201)
    def add_source(data: SourceInput, user=Depends(admin)):
        config = store.source_config(data.name, data.kind, data.repository, data.ref)
        with session() as s:
            if data.kind == 'github' and any(src.config.get('repository') == config['repository'] and
                src.config.get('ref') == config['ref'] for src in s.scalars(select(store.StoreSource))):
                raise store.StoreConflict('该仓库及引用已存在，请刷新已有来源')
            src = store.StoreSource(config=config)
            s.add(src)
            s.flush()
            emit(s, None, 'skill_store.source.created', {'actor': user.id, 'source_id': src.id, 'config': config})
            return source_out(s, src)

    @app.put('/api/skill-store/sources/{source_id}')
    def edit_source(source_id: str, data: SourceInput, user=Depends(admin)):
        config = store.source_config(data.name, data.kind, data.repository, data.ref)
        with session() as s:
            src = s.get(store.StoreSource, source_id)
            if not src:
                raise HTTPException(404, '来源不存在')
            if src.config['kind'] in ('builtin', 'team'):
                raise ValueError('内置与团队来源不能改为外部来源')
            if src.version != data.expected_version:
                raise store.StoreConflict('来源版本已改变')
            if config['repository'] != src.config.get('repository') or config['kind'] != src.config['kind']:
                raise ValueError('更换仓库或来源类型请另建来源，保留现有来源记录')
            updated = s.execute(update(store.StoreSource).where(store.StoreSource.id == source_id,
                store.StoreSource.version == data.expected_version).values(config=config, version=data.expected_version + 1))
            if updated.rowcount != 1:
                raise store.StoreConflict('来源同时被修改')
            emit(s, None, 'skill_store.source.updated', {'actor': user.id, 'source_id': source_id, 'config': config})
            s.refresh(src)
            return source_out(s, src)

    @app.post('/api/skill-store/sources/{source_id}/enabled')
    def enabled(source_id: str, data: EnabledRequest, user=Depends(admin)):
        with session() as s:
            changed = s.execute(update(store.StoreSource).where(store.StoreSource.id == source_id,
                store.StoreSource.version == data.expected_version).values(enabled=data.enabled, version=data.expected_version + 1))
            if changed.rowcount != 1:
                raise store.StoreConflict('来源不存在或已改变')
            emit(s, None, 'skill_store.source.enabled', {'actor': user.id, 'source_id': source_id, 'enabled': data.enabled})
            return source_out(s, s.get(store.StoreSource, source_id))

    @app.post('/api/skill-store/sources/{source_id}/sync')
    async def sync(source_id: str, data: SyncRequest, user=Depends(admin)):
        with session() as s:
            src = s.get(store.StoreSource, source_id)
            if not src:
                raise HTTPException(404, '来源不存在')
            if src.config['kind'] != 'github' or not src.enabled:
                raise ValueError('只有已启用的公开GitHub来源可以在线同步')
            if src.version != data.expected_version:
                raise store.StoreConflict('来源已改变，请刷新')
            config = copy.deepcopy(src.config)
        try:
            raw, origin = await store.download_github(config)
            parsed, rejected = store.scan_catalog(store.archive_files(raw), source_id)
            if not parsed:
                raise ValueError('仓库未包含可兼容的SKILL.md或角色定义；原有目录未修改')
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else 'GitHub同步失败：' + type(exc).__name__ + '；已有目录保留，可导入ZIP'
            with session() as s:
                s.execute(update(store.StoreSource).where(store.StoreSource.id == source_id,
                    store.StoreSource.version == data.expected_version).values(last_error=message[:600]))
                emit(s, None, 'skill_store.sync.failed', {'actor': user.id, 'source_id': source_id, 'reason': message[:600]})
            raise HTTPException(502, message[:600]) from exc
        with session() as s:
            src = s.get(store.StoreSource, source_id)
            summary = store.apply_catalog(s, src, parsed, rejected, origin, user.id, data.expected_version)
            return {'source': source_out(s, src), 'summary': summary}

    @app.post('/api/skill-store/sources/{source_id}/archive')
    async def upload_archive(source_id: str, request: Request,
                             expected_version: int = Query(..., ge=1), user=Depends(admin)):
        with session() as s:
            src = s.get(store.StoreSource, source_id)
            if not src:
                raise HTTPException(404, '来源不存在')
            if src.config['kind'] not in ('archive', 'github') or not src.enabled:
                raise ValueError('请选择可用的ZIP或GitHub来源')
            if src.version != expected_version:
                raise store.StoreConflict('来源已改变，请刷新')
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > store.MAX_ARCHIVE:
                raise ValueError('仓库ZIP不能超过20MB')
        parsed, rejected = store.scan_catalog(store.archive_files(bytes(raw)), source_id)
        if not parsed:
            raise ValueError('ZIP未包含可兼容条目；原有目录未修改' + ('：' + rejected[0]['reason'] if rejected else ''))
        origin = {'verification': 'user_archive', 'archive_sha256': hashlib.sha256(raw).hexdigest(),
                  'commit': '', 'note': '用户上传的ZIP，内容摘要可核对；没有验证其来自GitHub提交'}
        with session() as s:
            src = s.get(store.StoreSource, source_id)
            summary = store.apply_catalog(s, src, parsed, rejected, origin, user.id, expected_version)
            return {'source': source_out(s, src), 'summary': summary}

    @app.get('/api/skill-store/items')
    def catalog(q: str = Query('', max_length=200), source_id: str = '', category: str = '',
                kind: str = '', installed: bool = False, offset: int = Query(0, ge=0),
                limit: int = Query(24, ge=1, le=100), user=Depends(current_user)):
        if kind and kind != 'skill':
            raise ValueError('Skill商店只展示原生Skill；角色模板请使用 /api/role-templates/items')
        with session() as s:
            items = list(s.scalars(select(store.StoreItem).order_by(store.StoreItem.id)))
            results = []
            categories = {}
            for item in items:
                revision = s.get(store.StoreRevision, item.latest_revision)
                d = revision.details
                if store.revision_kind(revision) != 'skill':
                    continue
                categories[d['category']] = d['category_label']
                if source_id and source_id != item.source_id:
                    continue
                if category and category != d['category']:
                    continue
                if kind and kind != d['kind']:
                    continue
                if installed and not item.skill_id:
                    continue
                haystack = ' '.join([d['title'], d['description'], d['category_label'], item.path]).casefold()
                if q and q.casefold() not in haystack:
                    continue
                results.append(item)
            results.sort(key=lambda i: s.get(store.StoreRevision, i.latest_revision).details['title'].casefold())
            page = [store.item_out(s, i) for i in results[offset:offset + limit]]
            return {'items': page, 'total': len(results), 'offset': offset, 'has_more': offset + limit < len(results),
                    'categories': categories}

    @app.get('/api/skill-store/items/{item_id}')
    def detail(item_id: str, revision_id: str = '', user=Depends(current_user)):
        with session() as s:
            item, rev = get_item(s, item_id, revision_id or None)
            return store.item_out(s, item, rev, True)

    @app.post('/api/skill-store/items/{item_id}/install')
    def install(item_id: str, data: RevisionRequest, user=Depends(admin)):
        if not data.reviewed_source:
            raise ValueError('请先查看内容与许可证，并明确确认安装')
        try:
            with session() as s:
                item, rev = get_item(s, item_id, data.revision_id)
                if rev.digest != data.digest:
                    raise store.StoreConflict('安装摘要不匹配，请重新查看内容')
                row, revision, created = store.install(s, item, rev, user.id, data.expected_version)
                return {'skill': skills.revision_out(row, revision, True), 'created': created,
                        'item': store.item_out(s, item, rev, True)}
        except IntegrityError as exc:
            # Rollback already performed by session(); never leave partial installs.
            raise store.StoreConflict('发生并发安装或名称冲突，请刷新后重试') from exc

    @app.post('/api/skill-store/items/{item_id}/bind')
    def bind(item_id: str, data: BindRequest, user=Depends(admin)):
        with session() as s:
            item, rev = get_item(s, item_id, data.revision_id)
            installed = s.get(store.StoreInstall, rev.id)
            if not installed:
                raise ValueError('请先安装并审核此版本')
            return store.bind(s, installed.skill_id, installed.skill_version, data.agent_id,
                              data.expected_agent_version, data.mode, data.parameters, user.id)

    @app.post('/api/skill-store/items/{item_id}/uninstall')
    def uninstall(item_id: str, data: SyncRequest, user=Depends(admin)):
        with session() as s:
            # Legacy role installs may still be safely retired without deleting history.
            item, _ = get_item(s, item_id, native_only=False)
            row = s.get(Skill, item.skill_id) if item.skill_id else None
            if not row:
                raise ValueError('条目尚未安装')
            if row.latest_version != data.expected_version:
                raise store.StoreConflict('本地版本已改变')
            bound = [a.config['name'] for a in s.scalars(select(Agent))
                     if any(b['skill_id'] == row.id for b in a.config.get('skills', []))]
            if bound:
                raise store.StoreConflict('请先解除成员绑定：' + '、'.join(bound))
            active = []
            for run in s.scalars(select(Run).where(Run.status.notin_(['completed', 'cancelled']))):
                if any(b['skill_id'] == row.id for a in run.snapshot.get('agents', {}).values() for b in a.get('skills', [])):
                    active.append(run.id)
            if active:
                raise store.StoreConflict('在途或待处置运行仍引用此Skill，暂不卸载')
            row.enabled = False
            emit(s, None, 'skill_store.uninstalled', {'actor': user.id, 'item_id': item.id, 'skill_id': row.id,
                                                    'effect': '保留历史版本，停止后续绑定'})
            return {'disabled': True, 'history_preserved': True}

    @app.post('/api/skill-store/publish', status_code=201)
    def publish(data: PublishRequest, user=Depends(admin)):
        with session() as s:
            row = s.get(Skill, data.skill_id)
            revision = s.get(SkillRevision, f'{data.skill_id}:{data.version}')
            if not row or not row.enabled or not revision or revision.status != 'approved':
                raise ValueError('只发布已启用、已审核的本地Skill版本')
            if revision.digest != data.digest:
                raise store.StoreConflict('发布内容摘要不匹配')
            store.require_native_skill(s, row.id, revision.version)
            src = s.get(store.StoreSource, 'team-library')
            if not src.enabled:
                raise ValueError('团队来源已停用')
            files = copy.deepcopy(revision.files)
            old_origin = files.pop('assets/forge-store-origin.json', None)
            if old_origin:
                files['references/previous-store-origin.json'] = old_origin
            candidates = []
            # Preserve previously published items when publishing just one skill.
            item_id = store.hash_json([src.id, row.id + '/SKILL.md'])[:32]
            for existing in s.scalars(select(store.StoreItem).where(store.StoreItem.source_id == src.id, store.StoreItem.available == True)):
                if existing.id == item_id:
                    continue
                r = s.get(store.StoreRevision, existing.latest_revision)
                contents = copy.deepcopy(r.files)
                contents.pop('assets/forge-store-origin.json', None)
                candidates.append({'path': existing.path, 'files': contents, 'details': r.details})
            candidates.append({'path': row.id + '/SKILL.md', 'files': files, 'details': {
                'kind': 'skill', 'title': data.title.strip(), 'description': revision.manifest['description'],
                'category': data.category, 'category_label': store.CATEGORY_NAMES.get(data.category, data.category),
                'adapter': 'team-publish-v1', 'license': revision.manifest.get('license', '团队内部使用'),
                'warnings': ['团队发布，安装产生独立版本；不自动同步到已绑定Agent。'],
                'scripts': [p for p in files if p.startswith('scripts/')],
                'declared_tools': revision.manifest.get('allowed-tools', ''),
            }})
            store.apply_catalog(s, src, candidates, [], {'verification': 'team_reviewed',
                'publisher': user.id, 'original_skill_id': row.id, 'original_version': revision.version,
                'original_digest': revision.digest}, user.id, src.version)
            return store.item_out(s, s.get(store.StoreItem, item_id), full=True)
