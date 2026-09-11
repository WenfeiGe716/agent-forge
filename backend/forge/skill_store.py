"""Local Skill Store: immutable catalog, bounded imports and explicit installation.
Remote documents are data. No git hooks, installers, imports or package scripts run.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import io
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy import Boolean, Integer, JSON, String, Text, select, update
from sqlalchemy.orm import Mapped, mapped_column

from . import skills
from .db import Base, Agent, Run, Skill, SkillRevision, emit, now, uid
from .schema import AgentInput, SkillBinding

MAX_ARCHIVE = 20_000_000
MAX_EXPANDED = 60_000_000
MAX_ENTRIES = 8000
MAX_ITEMS = 1500
ADAPTER = 'forge-role-document-v2'
CATEGORY_NAMES = {
    'engineering': '软件工程', 'design': '设计与体验', 'testing': '测试与质量',
    'project-management': '项目管理', 'product': '产品', 'security': '安全',
    'game-development': '游戏开发', 'spatial-computing': '空间计算', 'support': '支持',
    'specialized': '专业能力', 'marketing': '市场', 'academic': '学术',
    'finance': '财务', 'sales': '销售', 'research': '研究', 'team': '团队自建',
}


class StoreSource(Base):
    __tablename__ = 'skill_store_sources'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    config: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync: Mapped[str] = mapped_column(String(40), default='')
    resolved_ref: Mapped[str] = mapped_column(String(100), default='')
    last_error: Mapped[str] = mapped_column(Text, default='')
    summary: Mapped[dict] = mapped_column(JSON, default=dict)


class StoreItem(Base):
    __tablename__ = 'skill_store_items'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(32), index=True)
    path: Mapped[str] = mapped_column(String(500))
    latest_revision: Mapped[str] = mapped_column(String(64))
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    skill_id: Mapped[str] = mapped_column(String(32), default='')
    installed_version: Mapped[int] = mapped_column(Integer, default=0)


class StoreRevision(Base):
    __tablename__ = 'skill_store_revisions'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(32), index=True)
    files: Mapped[dict] = mapped_column(JSON)
    details: Mapped[dict] = mapped_column(JSON)
    origin: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class StoreInstall(Base):
    __tablename__ = 'skill_store_installs'
    revision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(32), index=True)
    skill_id: Mapped[str] = mapped_column(String(32), index=True)
    skill_version: Mapped[int] = mapped_column(Integer)
    digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class StoreConflict(ValueError):
    pass


def hash_json(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                      separators=(',', ':')).encode()).hexdigest()


def repository(value: str) -> str:
    """Only public GitHub repository identifiers. Not a general URL-fetch endpoint."""
    parsed = urlsplit(value.strip())
    if (parsed.scheme != 'https' or parsed.netloc.lower() != 'github.com'
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError('当前在线源只接受 https://github.com/组织/仓库；内网仓库使用ZIP导入')
    path = parsed.path.strip('/')
    if path.endswith('.git'):
        path = path[:-4]
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', path):
        raise ValueError('请输入仓库根地址，不含tree、blob或额外路径')
    if any(x in ('.', '..') or x.startswith('.') for x in path.split('/')):
        raise ValueError('仓库名称无效')
    return 'https://github.com/' + path


def source_config(name: str, kind: str, url: str = '', ref: str = 'main') -> dict:
    if not name.strip() or len(name) > 100 or kind not in ('github', 'archive'):
        raise ValueError('来源名称或类型无效')
    if not ref or len(ref) > 180 or any(ord(x) < 32 for x in ref) or ref.startswith('-'):
        raise ValueError('分支、标签或提交引用无效')
    return {'name': name.strip(), 'kind': kind, 'repository': repository(url) if kind == 'github' else '', 'ref': ref}


async def _get_limited(client: httpx.AsyncClient, url: str, limit: int) -> bytes:
    async with client.stream('GET', url) as r:
        if r.status_code in (301, 302, 303, 307, 308):
            raise ValueError('来源返回重定向；请确认仓库地址，未继续转发请求')
        if r.status_code == 403:
            raise ValueError('GitHub访问受限或匿名请求限流；请稍后同步或上传仓库ZIP')
        if r.status_code == 404:
            raise ValueError('公开仓库或引用不存在；私有仓库请使用ZIP导入')
        r.raise_for_status()
        content = bytearray()
        async for part in r.aiter_bytes():
            content.extend(part)
            if len(content) > limit:
                raise ValueError('远程响应超过商店下载大小上限')
        return bytes(content)


async def download_github(config: dict) -> tuple[bytes, dict]:
    repo = repository(config['repository']).split('github.com/', 1)[1]
    async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10), follow_redirects=False,
                                 headers={'User-Agent': 'Agent-Forge-Skill-Store/0.10.1',
                                          'Accept': 'application/vnd.github+json'}) as client:
        commit = json.loads(await _get_limited(client,
            f'https://api.github.com/repos/{repo}/commits/{quote(config["ref"], safe="")}', 1_000_000))
        sha = commit.get('sha', '')
        if not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise ValueError('GitHub未返回可锁定的提交ID')
        raw = await _get_limited(client, f'https://codeload.github.com/{repo}/zip/{sha}', MAX_ARCHIVE)
    return raw, {'repository': config['repository'], 'requested_ref': config['ref'],
                 'commit': sha, 'verification': 'github_commit',
                 'archive_sha256': hashlib.sha256(raw).hexdigest()}


def archive_files(raw: bytes) -> dict[str, str]:
    """Read documents only, without extracting any archive path to the filesystem."""
    if len(raw) > MAX_ARCHIVE:
        raise ValueError('仓库ZIP不能超过20MB')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            infos = z.infolist()
            if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_EXPANDED:
                raise ValueError('仓库文件数或解压总大小超限')
            names = set()
            entries = []
            for info in infos:
                name = info.filename.rstrip('/') if info.is_dir() else info.filename
                if (not name or len(name) > 500 or name.startswith('/') or '\\' in name or ':' in name
                        or '\x00' in name or any(p in ('..', '.', '') for p in name.split('/'))
                        or info.flag_bits & 1 or stat.S_ISLNK(info.external_attr >> 16)):
                    raise ValueError('仓库包含不安全路径、符号链接或加密条目')
                if name in names:
                    raise ValueError('仓库包含重复路径')
                names.add(name)
                if not info.is_dir():
                    entries.append(info)
            # GitHub archives have one enclosing directory; normalize only that common root.
            first = {i.filename.split('/')[0] for i in entries}
            prefix = next(iter(first)) + '/' if len(first) == 1 and all('/' in i.filename for i in entries) else ''
            result = {}
            for info in entries:
                name = info.filename[len(prefix):]
                if any(p.startswith('.') for p in name.split('/')):
                    continue
                base = PurePosixPath(name).name
                # Nontext resources are recorded as blockers for native skills, not silently dropped.
                if info.file_size > 200_000:
                    result[name] = None
                    continue
                is_doc = PurePosixPath(name).suffix.lower() in skills.TEXT_EXT or base.upper() in ('LICENSE', 'LICENCE', 'NOTICE')
                if not is_doc:
                    result[name] = None
                    continue
                try:
                    result[name] = z.read(info).decode('utf-8-sig').replace('\r\n', '\n')
                except UnicodeError:
                    result[name] = None
            return result
    except zipfile.BadZipFile as exc:
        raise ValueError('无效的仓库ZIP') from exc


def slug(value: str) -> str:
    clean = re.sub('[^a-z0-9]+', '-', value.lower()).strip('-')
    return (clean or 'role')[:48].strip('-')


def _role(raw: str, path: str, source_id: str) -> tuple[dict, dict]:
    """Parse the agency-agents Markdown profile without executing its conversion scripts."""
    if not raw.startswith('---\n') or '\n---\n' not in raw[4:]:
        raise ValueError('不是带元数据的角色定义')
    header, body = raw[4:].split('\n---\n', 1)
    if len(header) > 16000:
        raise ValueError('角色元数据超限')
    m = re.search(r'^name:\s*(.+)$', header, flags=re.M)
    if not m:
        raise ValueError('角色缺少name')
    title = m.group(1).strip().strip('"\'')
    if not title or len(title) > 160:
        raise ValueError('角色名称过长')
    canonical = ('agency-' if source_id == 'agency-agents' else 'role-') + slug(PurePosixPath(path).stem)
    canonical = canonical[:64].rstrip('-')
    normalized = re.sub(r'^name:.*$', 'name: ' + canonical, header, flags=re.M)
    manifest, _ = skills.parse_frontmatter('---\n' + normalized + '\n---\n' + body)
    description = manifest['description']
    # A role description is not an executable Skill. Keep its original document,
    # not a generated SKILL.md that would conflate identity with capabilities.
    return {'ROLE.md': raw}, {
        'kind': 'role_template', 'title': title, 'description': description,
        'category': path.split('/')[0] if '/' in path else 'specialized',
        'adapter': ADAPTER, 'original_metadata': {k: v for k, v in manifest.items() if k not in ('body',)},
        'warnings': ['角色模板定义职责，可作为成员配置或团队方案的编辑素材，不是可安装的 Skill。',
                     '原文可能含自述能力、过时指标或身份指令，均不能代替实测和平台策略。'],
    }


def scan_catalog(files: dict, source_id: str) -> tuple[list[dict], list[dict]]:
    license_text = next((files.get(p) for p in ('LICENSE', 'LICENSE.md', 'LICENSE.txt', 'LICENCE') if files.get(p)), '')
    candidates, rejected = [], []
    roots = [p[:-8] for p in sorted(files) if p == 'SKILL.md' or p.endswith('/SKILL.md')]
    for root in roots:
        path = root + 'SKILL.md'
        subset = {k[len(root):]: v for k, v in files.items() if k.startswith(root)}
        # Nested skill roots are separate packages, never a parent's resources.
        if any(other != root and other.startswith(root) for other in roots):
            rejected.append({'path': path, 'reason': '嵌套Skill根目录需拆包，不自动合并'})
            continue
        try:
            if any(v is None for v in subset.values()):
                raise ValueError('包含本版不支持的二进制或超限资源')
            # License files without suffix are normalized for the existing bounded loader.
            for key in list(subset):
                if PurePosixPath(key).name.upper() in ('LICENSE', 'LICENCE', 'NOTICE'):
                    if key + '.txt' in subset:
                        raise ValueError('许可证路径冲突')
                    subset[key + '.txt'] = subset.pop(key)
            clean, manifest = skills.validate_files(subset)
            if root and PurePosixPath(root.rstrip('/')).name != manifest['name']:
                raise ValueError('Skill目录名与name不一致')
            candidates.append({'path': path, 'files': clean, 'details': {
                'kind': 'skill', 'title': manifest['name'], 'description': manifest['description'],
                'category': str(manifest.get('metadata', {}).get('category', 'team')),
                'adapter': 'native-skill-v1', 'warnings': [],
            }})
        except (ValueError, AttributeError, TypeError) as exc:
            rejected.append({'path': path, 'reason': str(exc)[:300]})
    ignore = ('scripts/', 'integrations/', 'examples/', 'node_modules/', 'docs/')
    for path, text in sorted(files.items()):
        if (not isinstance(text, str) or not path.endswith('.md') or path.endswith('SKILL.md')
                or path.startswith(ignore) or any(path.startswith(root) for root in roots)
                or not text.startswith('---\n')):
            continue
        try:
            package, details = _role(text, path, source_id)
            candidates.append({'path': path, 'files': package, 'details': details})
        except ValueError as exc:
            rejected.append({'path': path, 'reason': str(exc)[:300]})
    if len(candidates) > MAX_ITEMS:
        raise ValueError('单来源条目超过1500项')
    for c in candidates:
        own = next((text for name, text in c['files'].items()
                    if PurePosixPath(name).name.upper() in ('LICENSE.TXT', 'LICENSE.MD', 'LICENCE.TXT')), '')
        license_value = own or license_text
        if license_value:
            # Always preserve repository-level notice as well as any per-package license.
            if license_text:
                key = 'references/forge-upstream-license.txt'
                if key in c['files'] and c['files'][key] != license_text:
                    raise ValueError('保留的许可证资源路径冲突')
                c['files'][key] = license_text
            c['details']['license'] = 'MIT' if 'MIT License' in license_value else '查看原许可证'
        else:
            c['details']['license'] = '未提供许可证文本'
            c['details']['warnings'].append('来源未附许可证文本；安装前请确认使用权限。')
        scripts = [p for p in c['files'] if p.startswith('scripts/')]
        c['details']['scripts'] = scripts
        if scripts:
            c['details']['warnings'].append('包含脚本，商店安装与绑定默认不授权执行。')
        # Capture risky declarations as information, not as granted permissions.
        manifest = {}
        if c['details']['kind'] == 'skill':
            manifest, _ = skills.parse_frontmatter(c['files']['SKILL.md'])
        c['details']['declared_tools'] = c['details'].get('original_metadata', {}).get('allowed-tools', manifest.get('allowed-tools', ''))
        c['details']['category_label'] = CATEGORY_NAMES.get(c['details']['category'], c['details']['category'])
        if c['details']['kind'] == 'skill':
            c['files'], _ = skills.validate_files(c['files'])
    return candidates, rejected


def apply_catalog(s, source: StoreSource, candidates: list, rejected: list,
                  origin: dict, actor: str, expected_version: int) -> dict:
    if source.version != expected_version or not source.enabled:
        raise StoreConflict('来源已修改或停用，请刷新后重试')
    changed = s.execute(update(StoreSource).where(StoreSource.id == source.id,
        StoreSource.version == expected_version, StoreSource.enabled == True).values(version=expected_version + 1))
    if changed.rowcount != 1:
        raise StoreConflict('另一个同步已先提交，请刷新')
    if not candidates and rejected:
        raise ValueError('没有可导入条目；' + rejected[0]['reason'])
    visible = set()
    added = updated = same = 0
    for c in candidates:
        item_id = hash_json([source.id, c['path']])[:32]
        revision_id = hash_json([item_id, c['files'], c['details']])
        visible.add(item_id)
        item = s.get(StoreItem, item_id)
        if not item:
            item = StoreItem(id=item_id, source_id=source.id, path=c['path'], latest_revision=revision_id)
            s.add(item)
            added += 1
        elif item.latest_revision != revision_id:
            updated += 1
        else:
            same += 1
        rev = s.get(StoreRevision, revision_id)
        if not rev:
            original = {**origin, 'source_name': source.config['name'], 'source_id': source.id,
                        'path': c['path'], 'adapter': c['details']['adapter']}
            package = copy.deepcopy(c['files'])
            if 'assets/forge-store-origin.json' in package:
                raise ValueError('包使用了商店保留的来源路径')
            package['assets/forge-store-origin.json'] = json.dumps(original, ensure_ascii=False, indent=2)
            if c['details']['kind'] == 'skill':
                package, _ = skills.validate_files(package)
            rev = StoreRevision(id=revision_id, item_id=item_id, files=package,
                details=c['details'], origin=original, digest=skills.checksum(package))
            s.add(rev)
        item.latest_revision = revision_id
        item.available = True
    missing = 0
    for item in s.scalars(select(StoreItem).where(StoreItem.source_id == source.id)):
        if item.id not in visible:
            # An incompatible updated file is unavailable, not replaced with a partial package.
            item.available = False
            missing += 1
    source.last_sync = now()
    source.last_error = ''
    source.resolved_ref = origin.get('commit') or origin.get('archive_sha256', '')
    source.summary = {'total': len(candidates), 'added': added, 'updated': updated,
                      'counts': {kind: sum(c['details']['kind'] == kind for c in candidates)
                                 for kind in ('skill', 'role_template')},
                      'unchanged': same, 'unavailable': missing, 'rejected': rejected[:100],
                      'rejected_count': len(rejected), 'verification': origin.get('verification')}
    emit(s, None, 'skill_store.synced', {'actor': actor, 'source_id': source.id,
         'resolved_ref': source.resolved_ref, 'summary': source.summary})
    s.flush()
    return source.summary


def _has_role_origin(files):
    for path, raw in files.items():
        if path.endswith(('forge-store-origin.json', 'previous-store-origin.json')):
            try:
                origin = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(origin, dict) and (origin.get('kind') == 'role_template'
                    or str(origin.get('adapter', '')).startswith('forge-role-')):
                return True
    return False


def revision_kind(revision):
    # Older releases allowed publishing an installed role wrapper under the Skill
    # label. Classify its preserved provenance without mutating immutable history.
    if revision.details.get('kind') == 'role_template' or _has_role_origin(revision.files):
        return 'role_template'
    return revision.details.get('kind')


def require_native_revision(revision):
    if revision_kind(revision) != 'skill':
        raise ValueError('这是角色模板，不是Skill；请在角色模板库中加入团队设计草稿')


def require_native_skill(s, skill_id, version):
    """Legacy role imports retain history, but cannot become new approved Skills.

    Check install lineage across the Skill, not just its newest editable files;
    removing a provenance resource must not launder a role import into a Skill.
    """
    for installation in s.scalars(select(StoreInstall).where(StoreInstall.skill_id == skill_id)):
        revision = s.get(StoreRevision, installation.revision_id)
        if revision and revision_kind(revision) == 'role_template':
            raise ValueError('此旧版Skill来源是角色模板；历史保留，请改用角色模板库创建团队草稿')
    for revision in s.scalars(select(SkillRevision).where(SkillRevision.skill_id == skill_id)):
        if _has_role_origin(revision.files):
            raise ValueError('此内容来自角色模板，不能作为原生Skill审批、绑定或再次发布')


def seed_sources(s) -> None:
    """Catalog only. Startup never networks, installs skills, binds agents or calls models."""
    if not s.get(StoreSource, 'agency-agents'):
        s.add(StoreSource(id='agency-agents', config=source_config('Agency Agents', 'github',
              'https://github.com/msitarzewski/agency-agents', 'main')))
    if not s.get(StoreSource, 'team-library'):
        s.add(StoreSource(id='team-library', config={'name': '团队发布', 'kind': 'team', 'repository': '', 'ref': ''}))
    if s.get(StoreSource, 'forge-builtins'):
        return
    source = StoreSource(id='forge-builtins', config={'name': 'Agent Forge 内置', 'kind': 'builtin', 'repository': '', 'ref': '0.10.1'}, version=1, enabled=True)
    s.add(source)
    s.flush()
    root = Path(__file__).resolve().parent / 'store_builtin'
    candidates = []
    for p in sorted(root.glob('*.zip')):
        raw = skills.import_zip(p.read_bytes())
        _, manifest = skills.validate_files(raw)
        candidates.append({'path': manifest['name'] + '/SKILL.md', 'files': raw, 'details': {
            'title': {'requirements-clarification': '需求澄清与决策', 'evidence-code-review': '证据驱动代码审查',
                      'regression-test-design': '回归测试设计'}.get(manifest['name'], manifest['name']),
            'kind': 'skill', 'description': manifest['description'], 'category': 'engineering',
            'category_label': '软件工程', 'adapter': 'native-skill-v1', 'license': '随平台分发',
            'warnings': [], 'scripts': [], 'declared_tools': manifest.get('allowed-tools', ''),
        }})
    apply_catalog(s, source, candidates, [], {'verification': 'bundled', 'release': '0.10.1'},
                  'bootstrap', 1)


def item_out(s, item, revision=None, full=False):
    source = s.get(StoreSource, item.source_id)
    rev = revision or s.get(StoreRevision, item.latest_revision)
    local = s.get(Skill, item.skill_id) if item.skill_id else None
    install = s.get(StoreInstall, rev.id)
    installed = s.get(SkillRevision, f'{install.skill_id}:{install.skill_version}') if install else None
    bindings = []
    if local:
        for agent in s.scalars(select(Agent)):
            for b in agent.config.get('skills', []):
                if b['skill_id'] == local.id:
                    bindings.append({'agent_id': agent.id, 'name': agent.config['name'],
                                     'version': b['version'], 'enabled': b.get('enabled', True)})
    result = {'id': item.id, 'source_id': source.id, 'source_name': source.config['name'],
              'source_enabled': source.enabled, 'path': item.path, 'available': item.available,
              'latest_revision': item.latest_revision, 'revision_id': rev.id, 'digest': rev.digest,
              **rev.details, 'origin': rev.origin, 'created_at': rev.created_at,
              'skill_id': local.id if local else None, 'local_latest_version': local.latest_version if local else None,
              'local_enabled': local.enabled if local else None,
              'installed_version': install.skill_version if install else None,
              'installed_status': installed.status if installed else None,
              'installed_digest': installed.digest if installed else None,
              'update_available': bool(local and not s.get(StoreInstall, item.latest_revision)),
              'bindings': bindings}
    if full:
        result['files'] = rev.files
        result['versions'] = [{'revision_id': r.id, 'digest': r.digest, 'origin': r.origin,
                               'created_at': r.created_at} for r in s.scalars(select(StoreRevision)
                               .where(StoreRevision.item_id == item.id).order_by(StoreRevision.created_at.desc()))]
        base = s.get(SkillRevision, f'{local.id}:{local.latest_version}') if local else None
        differences = []
        if base:
            for path in sorted(set(base.files) | set(rev.files)):
                if base.files.get(path) != rev.files.get(path):
                    differences.append({'path': path, 'diff': '\n'.join(difflib.unified_diff(
                        base.files.get(path, '').splitlines(), rev.files.get(path, '').splitlines(),
                        fromfile='installed/' + path, tofile='store/' + path, n=3))[:12000]})
        result['diff'] = differences[:80]
    return result


def install(s, item, rev, actor, expected_version=None):
    require_native_revision(rev)
    source = s.get(StoreSource, item.source_id)
    if not source.enabled or not item.available:
        raise StoreConflict('来源已停用或条目已从来源下架，不安装；已锁定版本保留')
    existing = s.get(StoreInstall, rev.id)
    if existing:
        row = s.get(Skill, existing.skill_id)
        revision = s.get(SkillRevision, f'{existing.skill_id}:{existing.skill_version}')
        return row, revision, False
    # Acquire a conditional write lock before creating the local revision.
    locked = s.execute(update(StoreItem).where(StoreItem.id == item.id,
        StoreItem.skill_id == item.skill_id, StoreItem.installed_version == item.installed_version)
        .values(installed_version=item.installed_version))
    if locked.rowcount != 1:
        raise StoreConflict('另一个安装已先提交，请刷新后确认')
    files = copy.deepcopy(rev.files)
    manifest, _ = skills.parse_frontmatter(files['SKILL.md'])
    if item.skill_id:
        row = s.get(Skill, item.skill_id)
        if row.latest_version != item.installed_version:
            raise StoreConflict('已安装Skill存在本地修订。请先发布为团队副本，不自动覆盖本地修改')
        if expected_version != row.latest_version:
            raise StoreConflict('已安装版本变化，请刷新后确认更新')
        # Catalog uses its native name; installed namespace is stable across revisions.
        if row.name != manifest['name']:
            files['SKILL.md'] = re.sub(r'^name:.*$', 'name: ' + row.name, files['SKILL.md'], count=1, flags=re.M)
        row, revision = skills.save(s, files, actor, row.id, row.latest_version)
    else:
        # Name collisions do not overwrite or adopt someone else's Skill.
        name = manifest['name']
        if s.scalar(select(Skill).where(Skill.name == name)):
            name = name[:51].rstrip('-') + '-' + item.id[:10]
            files['SKILL.md'] = re.sub(r'^name:.*$', 'name: ' + name, files['SKILL.md'], count=1, flags=re.M)
        row, revision = skills.save(s, files, actor)
    item.skill_id = row.id
    item.installed_version = row.latest_version
    s.add(StoreInstall(revision_id=rev.id, item_id=item.id, skill_id=row.id,
                       skill_version=revision.version, digest=revision.digest))
    emit(s, None, 'skill_store.installed', {'actor': actor, 'item_id': item.id,
         'revision_id': rev.id, 'source_id': item.source_id, 'origin': rev.origin,
         'skill_id': row.id, 'version': revision.version, 'digest': revision.digest})
    s.flush()
    return row, revision, True


def bind(s, skill_id, version, agent_id, expected_agent_version, mode, parameters, actor):
    require_native_skill(s, skill_id, version)
    row = s.get(Skill, skill_id)
    rev = s.get(SkillRevision, f'{skill_id}:{version}')
    agent = s.get(Agent, agent_id)
    if not agent or not agent.config.get('enabled', True):
        raise ValueError('目标成员不存在或已停用')
    if not row or not row.enabled or not rev or rev.status != 'approved':
        raise ValueError('绑定前需要启用Skill并批准所选版本')
    if agent.version != expected_agent_version:
        raise StoreConflict('成员配置已变化，请刷新后确认绑定')
    binding = SkillBinding(skill_id=skill_id, version=version, mode=mode,
                           parameters=parameters, allow_scripts=False).model_dump()
    config = copy.deepcopy(agent.config)
    config['skills'] = [b for b in config.get('skills', []) if b['skill_id'] != skill_id] + [binding]
    config = AgentInput.model_validate(config).model_dump()
    skills.freeze_bindings(s, config['skills'])
    result = s.execute(update(Agent).where(Agent.id == agent.id, Agent.version == expected_agent_version)
                       .values(config=config, version=expected_agent_version + 1))
    if result.rowcount != 1:
        raise StoreConflict('成员配置发生并发修改')
    emit(s, None, 'skill_store.bound', {'actor': actor, 'agent_id': agent.id,
         'skill_id': skill_id, 'version': version, 'digest': rev.digest,
         'mode': mode, 'allow_scripts': False})
    return {'agent_id': agent.id, 'version': expected_agent_version + 1,
            'binding': binding, 'effect': '仅后续新运行；已有运行的Skill快照保持不变'}
