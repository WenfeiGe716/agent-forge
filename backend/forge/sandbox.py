"""Narrow, authenticated broker. Only this service holds the Docker socket.
The agent sees neither this token nor Docker control. Bind mounts are derived from IDs.
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException
from pydantic import Field
from .schema import Strict
from .config import settings

app = FastAPI(title='Agent Forge sandbox broker', docs_url=None, redoc_url=None, openapi_url=None)
HOST_ROOT = Path(os.getenv('HOST_DATA_ROOT', str(settings.data_root))).resolve()
RUNTIME = os.getenv('SANDBOX_RUNTIME', 'runsc')
IMAGES = {name.strip() for name in os.getenv('SANDBOX_IMAGES', 'agent-forge/sandbox:local').split(',') if name.strip()}
UID = os.getenv('SANDBOX_UID', '1000')
GID = os.getenv('SANDBOX_GID', '1000')
MAX_CONTAINERS = int(os.getenv('MAX_SANDBOXES', '4'))
slots = asyncio.Semaphore(MAX_CONTAINERS)

class Command(Strict):
    run_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    task_key: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,63}$')
    operation_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    image: str
    command: str = Field(min_length=1, max_length=4000)
    timeout_s: int = Field(120, ge=1, le=3600)
    cpu: float = Field(1, ge=.1, le=32)
    memory_mb: int = Field(1024, ge=128, le=65536)

def docker_args(c: Command) -> list[str]:
    if c.image not in IMAGES: raise ValueError('镜像未被管理员加入 SANDBOX_IMAGES')
    relative = Path('runs') / c.run_id / 'tasks' / c.task_key
    local = settings.data_root / relative
    if not local.is_dir() or local.is_symlink(): raise ValueError('任务工作区不存在或不安全')
    for p in [local, *local.parents]:
        if p == settings.data_root.parent: break
        if p.is_symlink(): raise ValueError('任务路径不得包含符号链接')
    host = HOST_ROOT / relative
    return ['docker', 'run', '--rm', '--pull=never', '--name', 'forge-' + c.operation_id[:24],
            '--runtime=' + RUNTIME, '--network=none', '--read-only', '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--pids-limit=128', '--cpus=' + str(c.cpu),
            '--memory=' + str(c.memory_mb) + 'm', '--memory-swap=' + str(c.memory_mb) + 'm',
            '--ulimit', 'nofile=1024:1024', '--ulimit', 'fsize=104857600:104857600',
            '--user', UID + ':' + GID, '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m',
            '--env', 'HOME=/tmp', '--workdir', '/workspace',
            '--mount', f'type=bind,source={host},target=/workspace',
            '--mount', f'type=bind,source={host}/.git,target=/workspace/.git,readonly',
            '--entrypoint', '/bin/sh', c.image, '-lc', c.command]

async def capture_bounded(proc, maximum=24000):
    output, size = [], 0
    while True:
        chunk = await proc.stdout.read(4096)
        if not chunk: break
        if size < maximum:
            output.append(chunk[:maximum-size])
        size += len(chunk)
    await proc.wait()
    return b''.join(output).decode(errors='replace'), size > maximum

async def remove_container(name):
    try:
        p = await asyncio.create_subprocess_exec('docker', 'rm', '-f', name, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(p.wait(), 10)
    except Exception:
        pass

@app.get('/health')
async def health():
    return {'service': 'sandbox-broker', 'runtime': RUNTIME, 'network': 'none', 'images': sorted(IMAGES)}

@app.post('/execute')
async def execute(c: Command, x_sandbox_token: str = Header('')):
    if not settings.sandbox_token or not hmac.compare_digest(x_sandbox_token, settings.sandbox_token):
        raise HTTPException(401, 'Unauthenticated broker request')
    folder = settings.data_root / 'broker' / c.run_id
    folder.mkdir(parents=True, exist_ok=True)
    record = folder / (c.operation_id + '.json')
    fingerprint = hashlib.sha256(c.model_dump_json().encode()).hexdigest()
    # Exclusive creation distinguishes completed calls from unknown in-flight calls after a crash.
    try:
        fd = os.open(record, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as f: json.dump({'state': 'started', 'fingerprint': fingerprint}, f)
    except FileExistsError:
        old = json.loads(record.read_text())
        if old.get('fingerprint') != fingerprint: raise HTTPException(409, '同一操作ID的参数发生变化')
        if old.get('state') == 'completed': return old['result']
        raise HTTPException(409, '上一次命令结果不确定，拒绝自动重放')
    proc = None
    try:
        args = docker_args(c)
        async with slots:
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                output, truncated = await asyncio.wait_for(capture_bounded(proc), c.timeout_s)
                result = {'exit_code': proc.returncode, 'output': output, 'truncated': truncated, 'timed_out': False}
            except asyncio.TimeoutError:
                await remove_container('forge-' + c.operation_id[:24])
                if proc.returncode is None: proc.kill()
                await proc.wait()
                result = {'exit_code': 124, 'output': '命令超过运行时限，已请求终止容器。', 'truncated': False, 'timed_out': True}
        tmp = record.with_suffix('.tmp')
        tmp.write_text(json.dumps({'state': 'completed', 'fingerprint': fingerprint, 'result': result}))
        tmp.replace(record)
        return result
    except (ValueError, OSError) as exc:
        raise HTTPException(503, str(exc)) from exc
    finally:
        if proc is not None and proc.returncode is None:
            await remove_container('forge-' + c.operation_id[:24])
            proc.kill()
            await proc.wait()

@app.get('/readiness')
async def readiness(x_sandbox_token: str = Header('')):
    """Inspect fixed operator configuration; never run model-provided commands.
    A reachable HTTP process alone does not establish a working Docker runtime.
    """
    if not settings.sandbox_token or not hmac.compare_digest(x_sandbox_token,settings.sandbox_token):
        raise HTTPException(401,'Unauthenticated broker request')
    missing=[]
    async def inspect(args):
        proc=None
        try:
            proc=await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
            output,_=await asyncio.wait_for(capture_bounded(proc,40000),4)
            if proc.returncode: return None
            return json.loads(output)
        except (OSError,ValueError,asyncio.TimeoutError):
            return None
        finally:
            if proc is not None and proc.returncode is None:
                proc.kill();await proc.wait()
    info=await inspect(['docker','info','--format','{{json .Runtimes}}'])
    if not isinstance(info,dict): missing.append('Docker服务不可用或无访问权限')
    elif RUNTIME not in info: missing.append('未注册隔离运行时 '+RUNTIME)
    images=[]
    for image in sorted(IMAGES):
        details=await inspect(['docker','image','inspect','--format','{{json .Id}}',image]) if info else None
        images.append({'name':image,'available':bool(details)})
    if not any(i['available'] for i in images):missing.append('未准备可执行的项目镜像')
    return {'service':'sandbox-broker','ready':not missing,'runtime':RUNTIME,'images':images,'missing':missing,'network':'none'}
