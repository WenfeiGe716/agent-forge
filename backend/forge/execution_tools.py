"""Discover real CLI capabilities in the selected execution environment.

Executable names come from the task, never from a fixed tool-package catalog.
Discovery is not installation, permission approval, or a claim of compatibility.
"""
import shlex

from fastapi import Depends, HTTPException
from pydantic import Field
from sqlalchemy import select

from .db import Run, Task, Event, session, emit
from .schema import Strict
from .workspace import task_root, safe_path


class EnvironmentQuery(Strict):
    executables: list[str] = Field(min_length=1, max_length=60)


def discovery_command(names: list[str], root: str) -> str:
    import re
    if not names or len(names)>60 or any(not isinstance(n,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,79}',n) for n in names):
        raise ValueError('指定 1–60 个可执行文件名称；名称不含路径、参数或 shell 展开')
    if len(set(names))!=len(names): raise ValueError('工具名称不能重复')
    bins=':'.join(root+'/'+p for p in ('.venv/bin','venv/bin','node_modules/.bin','.forge-tools/bin'))
    lines=['PATH='+shlex.quote(bins)+':"$PATH"', 'export PATH']
    for name in names:
        quoted=shlex.quote(name)
        # command -v is a shell builtin: it locates but never runs the candidate.
        lines.append('printf "%s\\t" '+quoted+'; command -v '+quoted+' 2>/dev/null || printf "\\n"')
    command='\n'.join(lines)
    if len(command)>4000: raise ValueError('探测请求太长，请分批检测工具')
    return command


async def inspect_environment(run_id, key, operation, arguments, runtime, image):
    query=EnvironmentQuery.model_validate(arguments)
    with session() as s:
        run,task=s.get(Run,run_id),s.get(Task,run_id+':'+key)
        if not run or not task or task.kind not in ('develop', 'autonomous') or 'run_command' not in run.snapshot['agents'][task.agent_id]['tools']:
            raise ValueError('环境探测只用于当前已授予命令工具的执行成员')
        mode=run.snapshot['project'].get('execution_mode','container')
        aid=task.agent_id
    root=task_root(run_id,key)
    command=discovery_command(query.executables,str(root.resolve()) if mode=='local' else '/workspace')
    from .tools import command as execute
    receipt=await execute(run_id,key,operation,command,runtime,image)
    if receipt.get('exit_code')!=0 or receipt.get('timed_out') or receipt.get('truncated'):
        return {'error':'环境探测未完成，不能据此判断工具缺失或可用','receipt':receipt}
    lines=receipt.get('output','').splitlines()
    if len(lines)!=len(query.executables): raise ValueError('环境探测回执不完整')
    found=[]
    for expected,line in zip(query.executables,lines):
        name,separator,path=line.partition('\t')
        if not separator or name!=expected: raise ValueError('环境探测回执与请求工具不匹配')
        found.append({'name':name,'available':bool(path),'resolved_path':path or None,'version_verified':False})
    manifests=[]
    for name in ('package.json','pyproject.toml','requirements.txt','uv.lock','poetry.lock','go.mod','Cargo.toml','pom.xml','Makefile'):
        try:
            file=safe_path(root,name)
            if file.is_file(): manifests.append(name)
        except ValueError: pass
    result={'execution_mode':mode,'cwd':receipt.get('cwd',str(root) if mode=='local' else '/workspace'),
            'executables':found,'project_manifests':manifests,'installation_performed':False,
            'note':'只定位实际可执行文件，不运行其版本命令、不保证兼容性。读取项目声明后选择实际命令；缺失工具按三档权限申请安装，安装后重新探测。',
            'execution_id':receipt.get('execution_id')}
    with session() as s:
        emit(s,run_id,'execution.tools.discovered',{'task_key':key,'agent_id':aid,**result})
    return result


def register(app):
    from .api import current_user,run_access

    @app.get('/api/execution/tools')
    def discoveries(run_id: str='',user=Depends(current_user)):
        with session() as s:
            if run_id: run_access(s,run_id,user)
            elif user.role!='admin': raise HTTPException(403,'请选择有权限的运行')
            query=select(Event).where(Event.kind=='execution.tools.discovered').order_by(Event.id.desc())
            if run_id: query=query.where(Event.run_id==run_id)
            rows=s.scalars(query.limit(50))
            return {'items':[{'event_id':e.id,'run_id':e.run_id,'created_at':e.created_at,**e.data} for e in rows],
                    'policy':'按任务请求发现本机或容器中的实际CLI和项目依赖；不是固定工具白名单。发现不自动安装或扩权。'}
