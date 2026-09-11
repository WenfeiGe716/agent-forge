"""Transactional desktop workspace preparation. Never writes to the legacy workspace.

The caller seals returned secrets with macOS safeStorage, then atomically renames
the staging directory. A failed/unfinished preparation is never a usable workspace.
"""
import base64
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import stat
import tempfile

SECRET_KEYS = ('ENCRYPTION_KEY', 'ADMIN_PASSWORD', 'SANDBOX_TOKEN')
TERMINAL = ('completed', 'cancelled', 'needs_attention')
COPY_SETTINGS = ('SANDBOX_URL', 'MAX_ACTIVE_RUNS', 'MAX_ACTIVE_ACTIVITIES', 'MAX_PROJECT_MB')


def private_json(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def _plain_path(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('桌面数据路径必须是明确的绝对路径')
    # Canonical system prefixes (/var -> /private/var) are fine; the selected
    # leaf and stored workspace must not themselves redirect to another root.
    if path.is_symlink():
        raise ValueError('不支持符号链接形式的数据目录')
    return path.resolve()


def _source_path(root, value, label):
    path = _plain_path(value)
    if not path.is_relative_to(root) or path == root:
        raise ValueError(f'{label}位于所选工作空间之外；请先在原环境中准备自包含备份')
    return path


def _file_version(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _copy_regular_at(directory_fd, name, target, before):
    """Copy a bounded, unchanged ordinary inode without following a mutable path."""
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError('迁移目录包含链接或特殊文件；请准备仅含普通文件的备份')
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(name, flags, dir_fd=directory_fd), 'rb') as source:
        opened = os.fstat(source.fileno())
        if _file_version(opened) != _file_version(before) or opened.st_nlink != 1:
            raise ValueError('迁移期间文件发生变化，请停止源环境中的任务后重试')
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700 if before.st_mode & 0o111 else 0o600), 'wb') as copied:
            remaining = opened.st_size
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk: raise ValueError('迁移期间文件发生变化，请重试')
                copied.write(chunk)
                remaining -= len(chunk)
            if source.read(1): raise ValueError('迁移期间文件发生变化，请重试')
            copied.flush()
            os.fsync(copied.fileno())
        after = os.fstat(source.fileno())
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (_file_version(opened) != _file_version(after) or _file_version(opened) != _file_version(named) or
                after.st_nlink != 1 or named.st_nlink != 1):
            raise ValueError('迁移期间文件发生变化，请重试')


def _snapshot_database(source, staging):
    """SQLite may create WAL sidecars even in mode=ro; never open the source DB.

    Copy the database and its optional committed WAL first, verify that neither
    changed while copying, then let SQLite recover only this private copy.
    """
    snapshot = Path(tempfile.mkdtemp(prefix='.database-', dir=staging))
    names = (source.name, source.name + '-wal', source.name + '-journal')
    expected = {}
    parent = source.parent
    parent_info = parent.stat(follow_symlinks=False)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened_parent = os.fstat(parent_fd)
        if (parent_info.st_dev, parent_info.st_ino) != (opened_parent.st_dev, opened_parent.st_ino):
            raise ValueError('数据库目录已变化，请重试')
        for name in names:
            try: expected[name] = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError: expected[name] = None
        if expected[source.name] is None:
            raise ValueError('原工作空间数据库不存在')
        if expected[source.name + '-journal'] is not None:
            raise ValueError('原数据库存在未完成的写入日志；请关闭原环境并准备完整备份后重试')
        for name in names[:2]:
            if expected[name] is not None:
                _copy_regular_at(parent_fd, name, snapshot / name, expected[name])
    finally:
        os.close(parent_fd)

    def verify():
        try:
            current_parent = parent.stat(follow_symlinks=False)
            if ((parent_info.st_dev, parent_info.st_ino) != (current_parent.st_dev, current_parent.st_ino) or
                    not stat.S_ISDIR(current_parent.st_mode)):
                raise ValueError('数据库目录已变化，请重试')
            for name, before in expected.items():
                try: after = (parent / name).stat(follow_symlinks=False)
                except FileNotFoundError: after = None
                if ((before is None) != (after is None) or before is not None and
                        (_file_version(before) != _file_version(after) or after.st_nlink != 1)):
                    raise ValueError('导入期间原环境发生写入；请停止操作后重新导入')
        except OSError as exc:
            raise ValueError('导入期间原数据库路径发生变化，请重试') from exc

    verify()
    return snapshot / source.name, verify


def copy_private_tree(source, target):
    """Copy ordinary files only, preserving Git snapshots but never following links."""
    def copy_dir(directory_fd, output):
        output.mkdir(mode=0o700)
        with os.scandir(directory_fd) as entries:
          for entry in entries:
            dest = output / entry.name
            before = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(before.st_mode):
                child_fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                        raise ValueError('迁移期间目录发生变化，请重试')
                    copy_dir(child_fd, dest)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
                _copy_regular_at(directory_fd, entry.name, dest, before)
            else:
                raise ValueError('迁移目录包含链接或特殊文件；请准备仅含普通文件的备份')
    root_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        copy_dir(root_fd, target)
    finally:
        os.close(root_fd)


def _tables(connection):
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _no_active_runs(connection):
    if 'runs' in _tables(connection):
        count = connection.execute('SELECT COUNT(*) FROM runs WHERE status NOT IN (?,?,?)', TERMINAL).fetchone()[0]
        if count:
            raise ValueError(f'原工作空间还有 {count} 个未结束任务；请先完成或取消，再导入，避免重复执行')


def prepare(data_dir, import_from=None):
    destination = _plain_path(data_dir)
    if destination.exists():
        raise ValueError('桌面工作空间已存在，不会覆盖或重置现有配置')
    source = _plain_path(import_from) if import_from else None
    # This must precede mkdir/mkdtemp: even a rejected migration cannot create
    # a staging parent inside the source workspace.
    if source is not None and (destination.is_relative_to(source) or source.is_relative_to(destination)):
        raise ValueError('桌面数据目录与导入来源不能相互包含')
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.forge-prepare-', dir=destination.parent))
    try:
        config = {'SANDBOX_URL': 'http://127.0.0.1:8091', 'MAX_ACTIVE_RUNS': '2', 'MAX_ACTIVE_ACTIVITIES': '4'}
        secret_values = {'ENCRYPTION_KEY': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
                         'ADMIN_PASSWORD': secrets.token_urlsafe(24), 'SANDBOX_TOKEN': secrets.token_urlsafe(36)}
        counts = {}
        if import_from:
            legacy_config = _source_path(source, str(source / '.local/config.json'), '原配置')
            if legacy_config.is_symlink() or not legacy_config.is_file():
                raise ValueError('请选择包含 .local/config.json 的原 Agent Forge 项目目录')
            values = json.loads(legacy_config.read_text())
            if not all(isinstance(values.get(k), str) and values[k] for k in SECRET_KEYS):
                raise ValueError('原工作空间缺少凭据密钥；不能通过生成新密钥修复迁移')
            from cryptography.fernet import Fernet
            Fernet(values['ENCRYPTION_KEY'].encode())
            url = values.get('DATABASE_URL', '')
            if not url.startswith('sqlite:////') or '?' in url:
                raise ValueError('自动迁移仅支持工作空间内的本地 SQLite 数据库')
            db_path = _source_path(source, url[len('sqlite:///'):], '数据库')
            data_path = _source_path(source, values['DATA_ROOT'], '任务数据')
            import_path = _source_path(source, values['IMPORT_ROOT'], '项目导入目录')
            if not db_path.is_file() or not data_path.is_dir() or not import_path.is_dir():
                raise ValueError('原工作空间的数据库、任务数据或导入目录缺失')
            if any(sensitive.is_relative_to(tree) for tree in (data_path, import_path) for sensitive in (legacy_config, db_path)):
                raise ValueError('任务数据或项目导入目录包含原凭据配置或数据库；请先准备分离的完整备份')
            database_copy, verify_database = _snapshot_database(db_path, staging)
            with closing(sqlite3.connect(database_copy)) as original:
                _no_active_runs(original)
                with closing(sqlite3.connect(staging / 'forge.db')) as copied:
                    original.backup(copied)
                    _no_active_runs(copied)
                    tables = _tables(copied)
                    if 'provider_revisions' in tables:
                        from cryptography.fernet import InvalidToken
                        try:
                            for row in copied.execute("SELECT encrypted_key FROM provider_revisions WHERE encrypted_key != ''"):
                                Fernet(values['ENCRYPTION_KEY'].encode()).decrypt(row[0].encode())
                        except (InvalidToken, TypeError, AttributeError) as exc:
                            raise ValueError('原加密密钥无法解密供应商凭据；请提供同一工作空间的完整备份') from exc
                    for name in ('agents', 'providers', 'model_profiles', 'projects', 'runs', 'skills'):
                        if name in tables:
                            counts[name] = copied.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
                    # Browser sessions are machine-entry authority, not transferable data.
                    if 'sessions' in tables:
                        copied.execute('DELETE FROM sessions')
                    copied.commit()
                    if copied.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('数据库完整性检查未通过')
                os.chmod(staging / 'forge.db', 0o600)
                copy_private_tree(data_path, staging / 'data')
                copy_private_tree(import_path, staging / 'imports')
                verify_database()
            shutil.rmtree(database_copy.parent)
            secret_values = {key: values[key] for key in SECRET_KEYS}
            config.update({key: str(values[key]) for key in COPY_SETTINGS if key in values})
        else:
            (staging / 'data').mkdir(mode=0o700)
            (staging / 'imports').mkdir(mode=0o700)
        config.update({'desktop_schema': 1, 'migrated': bool(import_from)})
        private_json(staging / 'config.json', config)
        private_json(staging / 'migration.json', {'schema': 1, 'migrated': bool(import_from), 'counts': counts,
                     'note': '独立副本；原工作空间未修改，既有运行快照不改写。浏览器登录会话不迁移。'})
        return {'ok': True, 'staging_dir': str(staging), 'secrets': secret_values,
                'migrated': bool(import_from), 'counts': counts}
    except Exception:
        shutil.rmtree(staging)
        raise


def serve_environment(message):
    root = _plain_path(message['data_dir'])
    if not (root / 'secrets.enc').is_file():
        raise ValueError('工作空间尚未完成钥匙串保护；不会创建替代密钥')
    values = json.loads((root / 'config.json').read_text())
    keys = message.get('secrets', {})
    if not all(isinstance(keys.get(k), str) and keys[k] for k in SECRET_KEYS):
        raise ValueError('无法解密工作空间密钥')
    port = message['port']
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('桌面本地端口无效')
    if not all(isinstance(message.get(k), str) and len(message[k]) >= 32 for k in ('desktop_token', 'control_token')):
        raise ValueError('桌面进程凭据无效')
    env = {k: str(values[k]) for k in COPY_SETTINGS if k in values}
    env.update({k: keys[k] for k in SECRET_KEYS})
    env.update({'DATABASE_URL': 'sqlite:///' + str(root / 'forge.db'), 'DATA_ROOT': str(root / 'data'),
                'IMPORT_ROOT': str(root / 'imports'), 'ORCHESTRATOR': 'local', 'DISABLE_DISPATCH': 'false',
                'FORGE_LOCAL_EXECUTION': 'true', 'FORGE_VALIDATION_MODE': 'false', 'SECURE_COOKIE': 'false',
                'ALLOWED_ORIGINS': f'http://127.0.0.1:{port}', 'FORGE_DESKTOP_TOKEN': message['desktop_token'],
                'FORGE_DESKTOP_CONTROL_TOKEN': message['control_token']})
    return env
