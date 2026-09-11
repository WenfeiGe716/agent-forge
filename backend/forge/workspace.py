"""File snapshots and patches, not arbitrary host shell execution."""
import fnmatch
import hashlib
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from .config import settings

IGNORE_DIRS = {'.git', '.hg', '.svn', '.ssh', '.aws', '.azure', '.gcloud', '.codex', '.docker', '.kube', '.local', '.config', 'node_modules', '__pycache__', '.venv', 'venv', '.idea', '.next', 'coverage'}
SECRET_PATTERNS = ['.env', '.env.*', '*.pem', '*.key', '*.p12', '*.pfx', 'id_rsa*', 'id_ed25519*', '*credentials*', '.npmrc', '.pypirc', '.netrc']
MAX_SNAPSHOT_FILES = 30000

def excluded(path: Path) -> bool:
    return any(part in IGNORE_DIRS or any(fnmatch.fnmatch(part.lower(), p) for p in SECRET_PATTERNS) for part in path.parts)

def safe_path(root: Path, relative: str, *, allow_missing: bool = False) -> Path:
    if not relative or '\\' in relative or '\x00' in relative:
        raise ValueError('无效路径')
    p = Path(relative)
    if p.is_absolute() or '..' in p.parts or excluded(p):
        raise ValueError('路径越界或属于禁止访问的文件')
    target = root / p
    walk = root
    for part in p.parts:
        walk = walk / part
        if walk.is_symlink(): raise ValueError('不允许通过符号链接访问文件')
    if not target.resolve().is_relative_to(root.resolve()): raise ValueError('路径越界')
    if target.exists():
        info = target.stat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or info.st_nlink > 1 and target.is_file():
            raise ValueError('不允许特殊文件或硬链接')
    elif not allow_missing:
        raise ValueError('文件不存在')
    return target


def _relative_file_parts(relative: str) -> tuple[str, ...]:
    """Use the same lexical/secret policy as safe_path, without resolving names."""
    if not isinstance(relative, str) or not relative or '\\' in relative or '\x00' in relative:
        raise ValueError('无效路径')
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or excluded(path) or not path.parts:
        raise ValueError('路径越界或属于禁止访问的文件')
    return path.parts


def _identity(info):
    return info.st_dev, info.st_ino


def _file_version(info):
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _regular_file(info, max_bytes=None):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('不允许特殊文件、符号链接或硬链接')
    if max_bytes is not None and info.st_size > max_bytes:
        raise ValueError('文件超过单次读写上限')
    return info


def _file_at(parent_fd, name, *, missing=False, max_bytes=None):
    try:
        return _regular_file(os.stat(name, dir_fd=parent_fd, follow_symlinks=False), max_bytes)
    except FileNotFoundError:
        if missing: return None
        raise


@contextmanager
def _workspace_file_parent(root: Path, relative: str, *, create=False, expected_identity=None):
    """Pin each directory descriptor; never follow a mutable child pathname.

    The caller supplies a trusted workspace root. Its ancestors and the parent
    of that root must not be writable by the task process; otherwise a directory
    could be moved outside the authority boundary despite holding its dirfd.
    We additionally reject observed root/parent-name replacements before IO.
    """
    parts = _relative_file_parts(relative)
    if not all(hasattr(os, flag) for flag in ('O_DIRECTORY', 'O_NOFOLLOW', 'O_CLOEXEC')):
        raise ValueError('当前平台不支持安全的目录描述符文件访问')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptors, chain = [], []
    try:
        root_fd = os.open(root, flags)
        descriptors.append(root_fd)
        root_info = os.fstat(root_fd)
        if expected_identity is not None and _identity(root_info) != expected_identity:
            raise ValueError('授权目录已变化，请重新选择目录')
        parent_fd = root_fd
        for part in parts[:-1]:
            try:
                child_fd = os.open(part, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                if not create: raise
                try: os.mkdir(part, mode=0o755, dir_fd=parent_fd)
                except FileExistsError: pass
                child_fd = os.open(part, flags, dir_fd=parent_fd)
            descriptors.append(child_fd)
            chain.append((parent_fd, part, os.fstat(child_fd)))
            parent_fd = child_fd

        def verify():
            current_root = os.stat(root, follow_symlinks=False)
            if not stat.S_ISDIR(current_root.st_mode) or _identity(current_root) != _identity(root_info):
                raise ValueError('工作区根目录已变化，拒绝继续文件操作')
            for parent, name, opened in chain:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISDIR(current.st_mode) or _identity(current) != _identity(opened):
                    raise ValueError('文件父目录已变化，拒绝继续文件操作')

        verify()
        yield parent_fd, parts[-1], verify
    finally:
        for fd in reversed(descriptors): os.close(fd)


def _byte_limit(max_bytes):
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError('文件读写大小上限无效')


def _read_regular_at(parent_fd, name, expected, max_bytes, verify):
    """Read bytes and mode from the same bounded, no-follow-validated inode."""
    _regular_file(expected, max_bytes)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent_fd)
    try:
        opened = _regular_file(os.fstat(fd), max_bytes)
        if _file_version(opened) != _file_version(expected):
            raise ValueError('文件在打开前已变化，拒绝读取')
        verify()
        raw = bytearray()
        while len(raw) <= max_bytes:
            chunk = os.read(fd, min(65536, max_bytes + 1 - len(raw)))
            if not chunk: break
            raw.extend(chunk)
        if len(raw) > max_bytes: raise ValueError('文件超过单次读取上限')
        after = _regular_file(os.fstat(fd), max_bytes)
        current = _file_at(parent_fd, name, max_bytes=max_bytes)
        if _file_version(after) != _file_version(opened) or _file_version(current) != _file_version(opened) or len(raw) != opened.st_size:
            raise ValueError('文件在读取期间已变化，拒绝返回不一致内容')
        verify()
        return bytes(raw), opened.st_mode
    finally:
        os.close(fd)


def read_workspace_file(root: Path, relative: str, max_bytes: int) -> bytes:
    """Read only the regular, single-link inode validated on this same fd."""
    _byte_limit(max_bytes)
    try:
        with _workspace_file_parent(root, relative) as (parent_fd, name, verify):
            expected = _file_at(parent_fd, name, max_bytes=max_bytes)
            return _read_regular_at(parent_fd, name, expected, max_bytes, verify)[0]
    except OSError as exc:
        raise ValueError('文件不存在或不能安全读取（禁止链接和特殊路径）') from exc


def write_workspace_file(root: Path, relative: str, data: bytes, max_bytes: int) -> None:
    """Replace one file atomically inside an already-open verified parent."""
    _byte_limit(max_bytes)
    if not isinstance(data, bytes): raise ValueError('写入内容必须为字节')
    if len(data) > max_bytes: raise ValueError('文件超过单次写入上限')
    try:
        with _workspace_file_parent(root, relative, create=True) as (parent_fd, name, verify):
            expected = _file_at(parent_fd, name, missing=True)
            mode = expected.st_mode & 0o777 if expected else 0o644
            temp_name, fd, owned = None, None, None
            try:
                for attempt in range(8):
                    candidate = '.forge-write-' + secrets.token_hex(16)
                    try:
                        fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=parent_fd)
                    except FileExistsError: continue
                    temp_name = candidate
                    break
                if fd is None: raise ValueError('无法创建唯一安全临时文件')
                owned = _regular_file(os.fstat(fd))
                remaining = memoryview(data)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0: raise ValueError('文件写入未完成')
                    remaining = remaining[written:]
                os.fsync(fd)
                completed = _regular_file(os.fstat(fd), max_bytes)
                named_temp = _file_at(parent_fd, temp_name, max_bytes=max_bytes)
                if _identity(completed) != _identity(owned) or _file_version(named_temp) != _file_version(completed) or completed.st_size != len(data):
                    raise ValueError('临时文件已变化，拒绝替换')
                current = _file_at(parent_fd, name, missing=True)
                if (current is None) != (expected is None) or current is not None and _file_version(current) != _file_version(expected):
                    raise ValueError('目标文件已变化，拒绝覆盖其他变更')
                verify()
                os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                temp_name = None
                committed = _file_at(parent_fd, name, max_bytes=max_bytes)
                if _identity(committed) != _identity(owned): raise ValueError('目标文件在替换后发生变化')
                verify()
            finally:
                # Never unlink a different entry if the temporary name changed.
                try:
                    if temp_name is not None and owned is not None:
                        try:
                            current = os.stat(temp_name, dir_fd=parent_fd, follow_symlinks=False)
                            if _identity(current) == _identity(owned): os.unlink(temp_name, dir_fd=parent_fd)
                        except FileNotFoundError: pass
                finally:
                    if fd is not None: os.close(fd)
    except OSError as exc:
        raise ValueError('文件不能安全写入（禁止链接和特殊路径）') from exc


def delete_workspace_file(root: Path, relative: str) -> bool:
    """Unlink only a no-follow-validated regular entry in a pinned directory."""
    try:
        with _workspace_file_parent(root, relative) as (parent_fd, name, verify):
            expected = _file_at(parent_fd, name, missing=True)
            if expected is None: return False
            verify()
            current = _file_at(parent_fd, name)
            if _file_version(current) != _file_version(expected):
                raise ValueError('目标文件已变化，拒绝删除其他变更')
            os.unlink(name, dir_fd=parent_fd)
            return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError('文件不能安全删除（禁止链接和特殊路径）') from exc

def _walk_regular_entries(root: Path, *, include_metadata=False, expected_identity=None):
    """Yield pinned parent descriptors, never traverse a mutable pathname.

    include_metadata is private to the platform-owned .git snapshot copy. It
    bypasses only the content-name exclusion list, never link/type checks.
    This is a bounded traversal, not an atomic multi-file snapshot.
    """
    entries = 0
    try:
        with _workspace_file_parent(root, 'snapshot-entry', expected_identity=expected_identity) as (root_fd, _, verify_root):
            def walk(directory_fd, relative, verify, depth):
                nonlocal entries
                if depth > 64: raise ValueError('工作区目录深度超过安全上限')
                verify()
                names = []
                with os.scandir(directory_fd) as scan:
                    for entry in scan:
                        entries += 1
                        if entries > MAX_SNAPSHOT_FILES * 4:
                            raise ValueError('工作区目录项数超过安全上限')
                        names.append(entry.name)
                for name in sorted(names):
                    rel = relative / name
                    if not include_metadata and excluded(rel): continue
                    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory_fd)
                        try:
                            if _identity(os.fstat(child_fd)) != _identity(info):
                                raise ValueError('目录在打开前已变化，拒绝遍历')

                            def verify_child(parent=directory_fd, child=name, expected=info, verify_parent=verify):
                                verify_parent()
                                current = os.stat(child, dir_fd=parent, follow_symlinks=False)
                                if not stat.S_ISDIR(current.st_mode) or _identity(current) != _identity(expected):
                                    raise ValueError('目录在遍历期间已变化，拒绝继续')

                            yield from walk(child_fd, rel, verify_child, depth + 1)
                            verify_child()
                        finally:
                            os.close(child_fd)
                    else:
                        _regular_file(info)
                        verify()
                        yield rel, directory_fd, name, info, verify
                        verify()
                verify()

            yield from walk(root_fd, Path(), verify_root, 0)
    except OSError as exc:
        raise ValueError('工作区遍历失败（文件已变化或包含链接/特殊路径）') from exc


def list_files(root: Path, limit: int = 1500) -> list[str]:
    if type(limit) is not int or limit < 1: raise ValueError('文件列表上限无效')
    output = []
    entries = _walk_regular_entries(root)
    try:
        for rel, *_ in entries:
            output.append(str(rel))
            if len(output) >= limit: break
        return output
    finally:
        entries.close()

def git(root: Path, *args: str, check=True, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    # No user-supplied hooks, credential helpers, filters, pagers or system config.
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0', 'GIT_AUTHOR_NAME': 'Agent Forge', 'GIT_AUTHOR_EMAIL': 'agent@localhost', 'GIT_COMMITTER_NAME': 'Agent Forge', 'GIT_COMMITTER_EMAIL': 'agent@localhost'}
    cmd = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'core.quotePath=false', '-c', 'commit.gpgsign=false', '-c', 'safe.directory=' + str(root), *args]
    result = subprocess.run(cmd, cwd=root, env=env, input=input_bytes, capture_output=True, timeout=45)
    if check and result.returncode:
        raise ValueError('Git操作失败: ' + result.stderr.decode(errors='replace')[-1200:])
    return result

def import_project(relative: str, project_id: str) -> str:
    source = safe_path(settings.import_root, relative)
    if not source.is_dir(): raise ValueError('导入路径必须是目录')
    return import_directory_snapshot(source, project_id, expected_identity=_identity(source.stat()))


def create_empty_project_snapshot(project_id: str) -> str:
    """Create an empty, git-backed project workspace for tasks started from scratch."""
    if not re.fullmatch('[a-f0-9]{32}', project_id):
        raise ValueError('无效项目标识')
    target = settings.data_root / 'projects' / project_id
    if target.exists():
        raise ValueError('项目工作副本已存在')
    target.mkdir(parents=True)
    try:
        git(target, 'init', '-b', 'main')
        git(target, 'commit', '--allow-empty', '-m', 'Created empty project workspace')
        return git(target, 'rev-parse', 'HEAD').stdout.decode().strip()
    except BaseException as exc:
        shutil.rmtree(target, ignore_errors=True)
        if isinstance(exc, OSError):
            raise ValueError('无法创建空白项目工作副本') from exc
        raise


def import_directory_snapshot(source: Path, project_id: str, *, expected_identity: tuple[int, int]) -> str:
    """Copy a previously authorized directory using the same no-follow IO as export.

    The caller supplies directory authority, never a raw renderer-provided path.
    Bind the open root to its authorized inode before reading any source bytes.
    """
    if not re.fullmatch('[a-f0-9]{32}', project_id): raise ValueError('无效项目标识')
    target = settings.data_root / 'projects' / project_id
    if target.exists(): raise ValueError('项目工作副本已存在')
    target.mkdir(parents=True)
    total = 0
    try:
        custom_ignore = []
        with _workspace_file_parent(source, '.forgeignore', expected_identity=expected_identity) as (parent_fd, name, verify):
            ignore_info = _file_at(parent_fd, name, missing=True, max_bytes=1024 * 1024)
            if ignore_info is not None:
                raw, _ = _read_regular_at(parent_fd, name, ignore_info, 1024 * 1024, verify)
                custom_ignore = [line.strip() for line in raw.decode(errors='replace').splitlines() if line.strip() and not line.startswith('#')]
        count = 0
        maximum = settings.max_project_mb * 1024 * 1024
        entries = _walk_regular_entries(source, expected_identity=expected_identity)
        try:
            for rel, parent_fd, name, info, verify in entries:
                if any(fnmatch.fnmatch(str(rel), pat) for pat in custom_ignore): continue
                count += 1
                if info.st_size > maximum - total or count > MAX_SNAPSHOT_FILES:
                    raise ValueError('项目超过配置的导入大小或文件数上限')
                raw, mode = _read_regular_at(parent_fd, name, info, maximum - total, verify)
                total += len(raw)
                dest = target / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with dest.open('xb') as copied: copied.write(raw)
                dest.chmod(0o755 if mode & 0o111 else 0o644)
        finally:
            entries.close()
        git(target, 'init', '-b', 'main')
        git(target, 'add', '-A')
        git(target, 'commit', '--allow-empty', '-m', 'Imported project snapshot')
        return git(target, 'rev-parse', 'HEAD').stdout.decode().strip()
    except BaseException as exc:
        shutil.rmtree(target, ignore_errors=True)
        if isinstance(exc, OSError):
            raise ValueError('导入目录无法安全读取（目录已变化或包含链接/特殊路径）') from exc
        raise

def run_root(run_id: str) -> Path:
    if not re.fullmatch('[a-f0-9]{32}', run_id): raise ValueError('无效run_id')
    return settings.data_root / 'runs' / run_id

def task_root(run_id: str, key: str) -> Path:
    if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,63}', key): raise ValueError('无效任务标识')
    return run_root(run_id) / 'tasks' / key

def copy_repo(source: Path, dest: Path) -> None:
    if dest.exists(): return
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_name(dest.name + '.staging')
    if temp.exists(): shutil.rmtree(temp)
    shutil.copytree(source, temp, symlinks=True)
    temp.rename(dest)
    # A copied index contains inode/ctime values from the source filesystem.
    # Refresh metadata before using --index patches; unchanged contents are preserved.
    git(dest, 'update-index', '--refresh')

def assert_safe_tree(root: Path) -> None:
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d != '.git']
        for name in dirs + files:
            p = Path(base) / name
            if p.is_symlink(): raise ValueError('成果包含符号链接，需人工处理')
            info = p.lstat()
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError('成果包含特殊文件')
            if p.is_file() and info.st_nlink > 1: raise ValueError('成果包含硬链接')

def patch_for(root: Path, base: str) -> bytes:
    """Export from a private inbound snapshot, never run Git in the live task.

    The caller must protect the task's parent and .git metadata from task writes.
    Controlled native/container profiles do so; explicit full host access does
    not provide this isolation. Individual files are validated while copying,
    but edits to different files need not represent one atomic point in time.
    """
    root = Path(root)
    staging = Path(tempfile.mkdtemp(prefix='.forge-export-', dir=root.parent))
    try:
        maximum = settings.max_project_mb * 1024 * 1024
        total = count = 0

        def copy_entries(source, destination, *, metadata=False):
            nonlocal total, count
            destination.mkdir(parents=True, exist_ok=True)
            entries = _walk_regular_entries(source, include_metadata=metadata)
            try:
                for rel, parent_fd, name, info, verify in entries:
                    count += 1
                    if count > MAX_SNAPSHOT_FILES or info.st_size > maximum - total:
                        raise ValueError('成果快照超过文件数或总大小上限')
                    raw, mode = _read_regular_at(parent_fd, name, info, maximum - total, verify)
                    total += len(raw)
                    target = destination / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # Only the host writes this private tree. Agents cannot access
                    # its sibling path under controlled execution profiles.
                    with target.open('xb') as copied: copied.write(raw)
                    target.chmod(0o755 if mode & 0o111 else 0o644)
            finally:
                entries.close()

        copy_entries(root, staging)
        copy_entries(root / '.git', staging / '.git', metadata=True)
        # Copying only files omits empty Git metadata directories, which are
        # required for recognizing otherwise valid repositories.
        (staging / '.git/objects').mkdir(exist_ok=True)
        (staging / '.git/refs').mkdir(exist_ok=True)
        git(staging, 'add', '-A', '--', '.')
        return git(staging, 'diff', '--cached', '--binary', '--no-ext-diff', '--no-textconv', base).stdout
    except OSError as exc:
        raise ValueError('成果快照生成失败，未在活动任务目录执行Git') from exc
    finally:
        # This exact mkdtemp directory is outside the task's write authority.
        shutil.rmtree(staging)

class IntegrationConflict(ValueError):
    """A deterministic pre-apply rejection: no patch/commit side effect began."""
    def __init__(self, detail):
        super().__init__('成果合并冲突，原补丁已保留')
        self.detail = detail[:1600]


def integrate(run_id: str, task_key: str, base: str) -> str:
    root = run_root(run_id)
    dest, source = root / 'integration', task_root(run_id, task_key)
    marker = root / 'artifacts' / f'{task_key}.integrated'
    if marker.exists(): return marker.read_text().strip()
    patch = patch_for(source, base)
    marker.parent.mkdir(exist_ok=True)
    (marker.parent / f'{task_key}.patch').write_bytes(patch)
    # Recovery can inspect the durable commit message before applying the patch again.
    message = f'forge-task:{task_key}'
    existing = git(dest, 'log', '--format=%H', '--grep=^' + re.escape(message) + '$').stdout.decode().strip()
    if existing:
        marker.write_text(existing.splitlines()[0]); return existing.splitlines()[0]
    if patch:
        checked = git(dest, 'apply', '--check', '--index', input_bytes=patch, check=False)
        if checked.returncode: raise IntegrationConflict(checked.stderr.decode('utf-8', errors='replace'))
        git(dest, 'apply', '--index', input_bytes=patch)
    git(dest, 'commit', '--allow-empty', '-m', message)
    commit = git(dest, 'rev-parse', 'HEAD').stdout.decode().strip()
    marker.write_text(commit)
    return commit
