"""Native project execution with an OS-enforced Seatbelt policy and durable receipts.

Missing/unsupported isolation never falls back to unrestricted execution. Only a
human-confirmed, frozen `permission_profile=full` deliberately omits Seatbelt.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile

from sqlalchemy import JSON, String, update
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, Run, Task, session, emit, now
from .config import settings
from . import workspace

SANDBOX = '/usr/bin/sandbox-exec'
SYSTEM_READ_ROOTS = ('/System', '/usr', '/bin', '/sbin', '/Library/Apple', '/Library/Developer/CommandLineTools',
                     '/opt/homebrew/Cellar', '/opt/homebrew/opt', '/opt/homebrew/bin', '/opt/homebrew/lib',
                     '/opt/homebrew/etc/openssl@3', '/private/var/db/dyld', '/private/var/db/timezone')
BASE_POLICY = '''(version 1)
(deny default)
(allow process-exec process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))
(allow sysctl-read (sysctl-name-prefix "hw.")
  (sysctl-name "kern.argmax") (sysctl-name "kern.ostype") (sysctl-name "kern.osrelease")
  (sysctl-name "kern.osversion") (sysctl-name "kern.osproductversion") (sysctl-name "kern.version")
  (sysctl-name "kern.maxfilesperproc") (sysctl-name "kern.maxproc") (sysctl-name "kern.usrstack64")
  (sysctl-name "sysctl.proc_cputype") (sysctl-name "vm.loadavg"))
(allow file-read-metadata)
(allow file-read* (literal "/") (literal "/dev/null") (literal "/dev/random") (literal "/dev/urandom") (literal "/private/etc/ssl/openssl.cnf"))
(allow file-write-data (literal "/dev/null"))
(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))
'''


class NativeExecution(Base):
    __tablename__ = 'native_executions'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    task_key: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default='started')
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


def quote(path):
    return json.dumps(str(path), ensure_ascii=False)


def canonical_grants(paths):
    if not isinstance(paths, list) or len(paths) > 12: raise ValueError('最多允许 12 个明确的外部文件路径')
    result = []
    for item in paths:
        if not isinstance(item, dict) or set(item) != {'path', 'access'} or item['access'] not in ('read', 'write'):
            raise ValueError('外部路径需提供 path 和 read/write 访问方式')
        raw = item['path']
        if not isinstance(raw, str) or not raw or len(raw) > 1000 or any(ord(c) < 32 for c in raw): raise ValueError('路径无效')
        path = Path(raw)
        if not path.is_absolute() or '..' in path.parts: raise ValueError('额外权限必须是绝对精确文件路径')
        if any(part in {'.ssh', '.aws', '.azure', '.gcloud', '.codex', '.config', '.local'} for part in path.parts) or workspace.excluded(path):
            raise ValueError('额外许可不能授予凭据/平台配置路径；不会通过审批偷偷扩大为完全访问')
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            if current.is_symlink(): raise ValueError('许可路径不允许符号链接，请明确填写真实路径')
        resolved = path.resolve()
        if resolved.is_relative_to(settings.data_root) or resolved.is_relative_to(Path(__file__).resolve().parents[2]):
            raise ValueError('不能给命令授予平台数据、代码或其他运行目录权限')
        if path.exists() and (not path.is_file() or path.stat().st_nlink != 1): raise ValueError('额外许可仅支持普通文件，不支持目录、设备或硬链接')
        if item['access'] == 'read' and not path.is_file(): raise ValueError('只读许可需要已存在的普通文件')
        if not path.parent.is_dir(): raise ValueError('额外文件的父目录必须已存在')
        result.append({'path': str(resolved), 'access': item['access']})
    if len({x['path'] for x in result}) != len(result): raise ValueError('额外许可路径不能重复')
    return result


def policy(root, home, paths=(), network=False, temporary_grant=False):
    lines = [BASE_POLICY]
    if paths or network or temporary_grant:
        # A temporary grant must not survive in a daemon after this command.
        # Seatbelt does not revoke descendant grants, so elevated commands are
        # deliberately single-process. Commands requiring children fail closed.
        lines.append('(deny process-fork)')
    for path in SYSTEM_READ_ROOTS: lines.append('(allow file-read* (subpath ' + quote(path) + '))')
    for path in (root, home):
        lines.append('(allow file-read* file-write* (subpath ' + quote(path) + '))')
    lines.append('(deny file-write* (subpath ' + quote(root / '.git') + '))')
    # Imported projects exclude secrets, but commands may create new files later.
    for name in ('.env', '.ssh', '.aws', '.azure', '.gcloud', '.codex', '.npmrc', '.pypirc', '.netrc'):
        lines.append('(deny file-read* file-write* (subpath ' + quote(root / name) + '))')
    sensitive = r'.*/([.]env([.][^/]*)?|[.]ssh(/.*)?|[.]aws(/.*)?|[.]codex(/.*)?|[.]npmrc|[.]netrc|[^/]*credentials[^/]*|[^/]*[.](pem|key|p12|pfx))$'
    lines.append('(deny file-read* file-write* (require-all (subpath ' + quote(root) + ') (regex #' + quote(sensitive) + ')))')
    for item in paths:
        op = 'file-read* file-write*' if item['access'] == 'write' else 'file-read*'
        lines.append('(allow ' + op + ' (literal ' + quote(item['path']) + '))')
    if network:
        # The approved command may make outbound IP connections, not use Unix
        # sockets to control Docker or local application authorities.
        lines += ['(allow network-outbound (remote tcp) (remote udp))',
                  '(allow system-socket (require-all (socket-domain AF_SYSTEM) (socket-protocol 2)))',
                  '(allow mach-lookup (global-name "com.apple.SystemConfiguration.DNSConfiguration") (global-name "com.apple.SystemConfiguration.configd") (global-name "com.apple.networkd") (global-name "com.apple.SecurityServer") (global-name "com.apple.trustd.agent"))',
                  '(allow file-read* (literal "/private/etc/resolv.conf") (literal "/private/etc/hosts") (literal "/private/etc/services") (literal "/private/etc/ssl/cert.pem"))']
    return '\n'.join(lines)


def clean_environment(home):
    # Never pass model/provider keys, API session cookies, proxy settings or
    # user startup files to a command. PATH does not include user home bins.
    return {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
            'HOME': str(home), 'TMPDIR': str(home), 'XDG_CONFIG_HOME': str(home), 'XDG_CACHE_HOME': str(home),
            'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8', 'PYTHONNOUSERSITE': '1',
            'PIP_CONFIG_FILE': '/dev/null', 'PIP_DISABLE_PIP_VERSION_CHECK': '1', 'OPENSSL_CONF': '/dev/null',
            'npm_config_userconfig': '/dev/null', 'npm_config_cache': str(home / 'npm-cache'),
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0'}


async def terminate_group(proc):
    # Every normal child inherits this new process group, including background
    # jobs. Kill it even when the shell exits before its children close stdout.
    try: os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError: pass
    await proc.wait()


async def capture(proc, maximum=24000):
    output, total = [], 0
    while True:
        chunk = await proc.stdout.read(4096)
        if not chunk: break
        if total < maximum: output.append(chunk[:maximum-total])
        total += len(chunk)
    await proc.wait()
    return b''.join(output).decode(errors='replace'), total > maximum


async def spawn(root, command, runtime, permission_profile='ask', *, paths=(), network=False, temporary_grant=False):
    if not settings.local_execution_enabled: raise RuntimeError('此安装未启用本机执行；请在本地客户端启动时明确启用')
    if sys.platform != 'darwin' or (permission_profile != 'full' and not Path(SANDBOX).is_file()):
        raise RuntimeError('本机执行需要可用的 macOS Seatbelt；不支持的平台不会自动无隔离执行')
    with tempfile.TemporaryDirectory(prefix='forge-command-home-') as temporary:
        home = Path(temporary).resolve()
        env = clean_environment(home)
        seconds = max(1, int(runtime.get('command_timeout_s', 120)))
        # Shell builtins set inherited limits without Python preexec_fn, which
        # can deadlock when an ASGI process has other active threads.
        shell = ['/bin/sh', '-c', 'ulimit -c 0; ulimit -n 256; ulimit -f 102400; ulimit -t ' + str(seconds + 1) + '; exec /bin/sh -c "$1"', 'forge-command', command]
        args = shell if permission_profile == 'full' else [SANDBOX, '-p', policy(root, home, paths, network, temporary_grant), *shell]
        # The clean shell cannot source a user's profile. Native mode enforces
        # CPU-time/file-size limits, not container CPU-core/memory quotas.
        proc = await asyncio.create_subprocess_exec(*args, cwd=root, env=env, start_new_session=True,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            text, truncated = await asyncio.wait_for(capture(proc), runtime.get('command_timeout_s', 120))
            return {'exit_code': proc.returncode, 'output': text, 'truncated': truncated, 'timed_out': False}
        except asyncio.TimeoutError:
            await terminate_group(proc)
            return {'exit_code': 124, 'output': '命令超时，已终止执行进程组及继承的子进程。', 'truncated': False, 'timed_out': True}
        finally:
            await terminate_group(proc)


async def readiness(permission_profile='ask'):
    if not settings.local_execution_enabled: return {'ready': False, 'runtime': 'disabled', 'detail': '此安装未启用本机执行；请使用明确启用本机权限的本地启动器'}
    if sys.platform != 'darwin': return {'ready': False, 'runtime': 'unavailable', 'detail': '本机受控执行目前支持 macOS Seatbelt；其他系统请选容器'}
    if permission_profile == 'full': return {'ready': True, 'runtime': 'native-full', 'detail': '用户明确选择完全访问：命令可访问当前账号文件和网络；仍清理环境凭据并限制超时'}
    if not Path(SANDBOX).is_file(): return {'ready': False, 'runtime': 'seatbelt', 'detail': '系统缺少 sandbox-exec；拒绝静默取消安全约束'}
    try:
        with tempfile.TemporaryDirectory(prefix='forge-seatbelt-probe-') as temp:
            result = await spawn(Path(temp).resolve(), '/usr/bin/true', {'command_timeout_s': 3})
        return {'ready': result['exit_code'] == 0, 'runtime': 'seatbelt',
                'detail': 'macOS Seatbelt 启动验证通过；默认项目内写入、禁网，尚未调用模型' if result['exit_code'] == 0 else 'Seatbelt 启动验证失败；未降级执行'}
    except (OSError, RuntimeError): return {'ready': False, 'runtime': 'seatbelt', 'detail': 'Seatbelt 启动验证失败；未降级执行'}


async def execute(run_id, task_key, operation, command, runtime, *, paths=(), network=False, permission_id=None):
    if not isinstance(command, str) or not command.strip() or len(command) > 4000: raise ValueError('命令必须为 1–4000 字符')
    root = workspace.task_root(run_id, task_key)
    if not root.is_dir() or root.is_symlink(): raise ValueError('任务工作区不存在或不安全')
    for parent in root.parents:
        if parent == settings.data_root.parent: break
        if parent.is_symlink(): raise ValueError('任务父路径不允许符号链接')
    root = root.resolve()
    grants = canonical_grants(list(paths))
    with session() as s:
        run = s.get(Run, run_id)
        if not run or run.snapshot['project'].get('execution_mode', 'container') != 'local': raise ValueError('冻结运行未选择本机执行')
        task = s.get(Task, run_id + ':' + task_key)
        if not task or run.status in ('completed', 'cancelled', 'needs_attention') or task.status in ('done', 'cancelled', 'needs_attention'):
            raise ValueError('命令不能执行：运行或任务已结束，或任务不存在')
        profile = run.snapshot['project'].get('permission_profile', 'ask')
        if profile not in ('ask', 'auto', 'full'): raise ValueError('冻结权限策略无效')
        if profile != 'full' and (grants or network) and not permission_id: raise ValueError('联网和越界路径需要精确审批许可')
        if permission_id:
            from .execution_permissions import validate_grant
            validate_grant(s, permission_id, run_id, task_key, command, grants, network)
    execution_id = hashlib.sha256((run_id + ':' + task_key + ':' + operation).encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps({'command': command, 'profile': profile, 'paths': grants, 'network': network,
        'permission_id': permission_id, 'runtime': runtime}, sort_keys=True).encode()).hexdigest()
    with session() as s:
        s.execute(update(Run).where(Run.id == run_id).values(updated_at=Run.updated_at))
        previous = s.get(NativeExecution, execution_id)
        if previous:
            if previous.fingerprint != fingerprint: raise ValueError('同一执行操作不能更换命令或权限')
            if previous.state != 'completed': raise RuntimeError('本机命令先前结果不确定，不自动重放')
            return previous.result
        s.add(NativeExecution(id=execution_id, run_id=run_id, task_key=task_key, fingerprint=fingerprint))
        emit(s, run_id, 'execution.native.started', {'task_key': task_key, 'execution_id': execution_id, 'command': command,
             'permission_profile': profile, 'network': profile == 'full' or network, 'paths': grants, 'permission_id': permission_id,
             'cwd': str(root)})
    result = await spawn(root, command, runtime, profile, paths=grants, network=network, temporary_grant=bool(permission_id))
    result.update({'runtime': 'native-full' if profile == 'full' else 'seatbelt', 'cwd': str(root),
                   'execution_mode': 'local', 'permission_profile': profile, 'execution_id': execution_id,
                   'network': 'unrestricted' if profile == 'full' else 'approved-outbound' if network else 'denied'})
    with session() as s:
        row = s.get(NativeExecution, execution_id); row.state, row.result = 'completed', result
        emit(s, run_id, 'execution.native.completed', {'task_key': task_key, 'execution_id': execution_id, 'result': result})
    return result
