"""Versioned Agent Skills. Registry content is subordinate procedure, never authority.
Supports SKILL.md + UTF-8 resources; scripts use the frozen local/container execution policy.
"""
import hashlib, io, json, re, shlex, stat, zipfile
from pathlib import PurePosixPath
from sqlalchemy import select, update
from .db import Skill, SkillRevision, Run, Task, session, emit

MAX_BUNDLE=1_500_000
TEXT_EXT={'.md','.txt','.json','.yaml','.yml','.py','.sh','.js','.ts','.csv','.toml','.xml','.html','.css'}

def checksum(files):
    return hashlib.sha256(json.dumps(files,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def parse_frontmatter(raw):
    if not raw.startswith('---\n') or '\n---\n' not in raw[4:]:
        raise ValueError('SKILL.md必须包含---分隔的YAML前置元数据')
    header, body=raw[4:].split('\n---\n',1)
    if len(header)>16000: raise ValueError('Skill元数据过长')
    # A bounded YAML subset keeps offline installs dependency-free. Scalar strings,
    # block scalars and one-level metadata mappings; aliases/tags/nested YAML refused.
    result={};lines=header.splitlines();i=0
    while i<len(lines):
        line=lines[i];i+=1
        if not line.strip() or line.lstrip().startswith('#'): continue
        m=re.fullmatch(r'([A-Za-z][\w-]*):(?:\s*(.*))?',line)
        if not m: raise ValueError('不支持的YAML格式：请使用标量或metadata字符串映射')
        key,value=m.group(1),m.group(2) or ''
        if key in result: raise ValueError('重复的元数据字段')
        if value in ('|','>','|-','>-'):
            parts=[]
            while i<len(lines) and (lines[i].startswith(' ') or not lines[i].strip()):
                parts.append(lines[i].strip());i+=1
            value=('\n' if value.startswith('|') else ' ').join(parts)
        elif not value and key=='metadata':
            value={}
            while i<len(lines) and lines[i].startswith(' '):
                item=lines[i].strip();i+=1
                if ':' not in item: raise ValueError('metadata必须是字符串映射')
                k,v=item.split(':',1); value[k.strip()]=v.strip().strip('"\'')
        elif value.startswith('"'):
            try:value=json.loads(value)
            except Exception:raise ValueError('双引号字段请使用合法JSON字符串转义')
        elif value.startswith("'"):
            if not value.endswith("'"): raise ValueError('字符串引号未闭合')
            value=value[1:-1].replace("''", "'")
        elif value[:1] in ('!','&','*','[','{'):
            raise ValueError('不支持YAML标签、别名或复杂对象')
        result[key]=value
    name=result.get('name','');desc=result.get('description','')
    if not isinstance(name,str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',name) or len(name)>64:
        raise ValueError('Skill名称应为1–64位小写字母、数字和单个连字符')
    if not isinstance(desc,str) or not desc.strip() or len(desc)>1024: raise ValueError('Skill描述须为1–1024字符')
    if not body.strip(): raise ValueError('SKILL.md正文不能为空')
    if len(str(result.get('compatibility','')))>500: raise ValueError('compatibility超过500字符')
    if 'allowed-tools' in result and not isinstance(result['allowed-tools'],str): raise ValueError('allowed-tools必须为字符串；不自动授予权限')
    return result,body.strip()

def validate_files(files):
    if not isinstance(files,dict) or not 1<=len(files)<=80 or 'SKILL.md' not in files: raise ValueError('Skill需要SKILL.md且文件数不能超过80')
    total=0;clean={}
    for name,text in files.items():
        p=PurePosixPath(name)
        if (not isinstance(text,str) or not name or len(name)>200 or '\\' in name or ':' in name or '\x00' in name or p.is_absolute()
            or any(x in ('..','.','') or x.startswith('.') for x in name.split('/')) or str(p)!=name
            or p.suffix.lower() not in TEXT_EXT): raise ValueError('不安全或不支持的Skill文件路径：'+str(name)[:200])
        size=len(text.encode('utf-8'));total+=size
        if size>200_000 or '\x00' in text or total>MAX_BUNDLE: raise ValueError('Skill文件过大或含二进制内容；本版只接收UTF-8文本资源')
        clean[name]=text.replace('\r\n','\n')
    manifest,body=parse_frontmatter(clean['SKILL.md'])
    return clean,{**manifest,'body':body,'files':[{'path':k,'bytes':len(v.encode()),'sha256':hashlib.sha256(v.encode()).hexdigest()} for k,v in sorted(clean.items())]}

def import_zip(raw):
    if len(raw)>MAX_BUNDLE: raise ValueError('压缩包超过1.5MB')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            entries=[x for x in z.infolist() if not x.is_dir()]
            if len(entries)>80 or sum(x.file_size for x in entries)>MAX_BUNDLE: raise ValueError('解压大小或文件数超限')
            files={}
            prefix=''
            if not any(x.filename=='SKILL.md' for x in entries):
                candidates=[x.filename for x in entries if x.filename.count('/')==1 and x.filename.endswith('/SKILL.md')]
                if len(candidates)!=1: raise ValueError('压缩包必须包含唯一根目录SKILL.md')
                prefix=candidates[0][:-8]
            for x in entries:
                if x.flag_bits&1 or stat.S_ISLNK(x.external_attr>>16): raise ValueError('禁止加密文件或符号链接')
                if not x.filename.startswith(prefix): raise ValueError('所有文件必须位于同一Skill根目录')
                name=x.filename[len(prefix):]
                if name in files: raise ValueError('压缩包包含重复文件')
                if x.file_size>200_000: raise ValueError('单文件超限')
                files[name]=z.read(x).decode('utf-8')
            clean,manifest=validate_files(files)
            if prefix and prefix.rstrip('/')!=manifest['name']: raise ValueError('Skill目录名必须与name一致')
            return clean
    except (zipfile.BadZipFile,UnicodeError) as exc: raise ValueError('无效ZIP或非UTF-8资源') from exc

def export_zip(rev):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        for name,text in sorted(rev.files.items()): z.writestr(rev.manifest['name']+'/'+name,text)
    return out.getvalue()

def save(s,files,actor,skill_id=None,expected=None):
    clean,manifest=validate_files(files)
    row=s.get(Skill,skill_id) if skill_id else None
    if skill_id and not row: raise ValueError('Skill不存在')
    if row:
        if row.name!=manifest['name']: raise ValueError('修订不能改变Skill名称，请另建Skill')
        if row.latest_version!=expected: raise ValueError('Skill版本冲突，请刷新')
        version=expected+1
        changed=s.execute(update(Skill).where(Skill.id==row.id,Skill.latest_version==expected).values(latest_version=version))
        if changed.rowcount!=1: raise ValueError('Skill版本冲突')
    else:
        if s.scalar(select(Skill).where(Skill.name==manifest['name'])): raise ValueError('Skill名称已存在，请编辑已有Skill')
        row=Skill(name=manifest['name']);s.add(row);s.flush();version=1
    rev=SkillRevision(id=f'{row.id}:{version}',skill_id=row.id,version=version,files=clean,manifest=manifest,digest=checksum(clean),status='draft')
    s.add(rev);s.flush();emit(s,None,'skill.revised',{'actor':actor,'skill_id':row.id,'version':version,'digest':rev.digest})
    return row,rev

def revision_out(row,rev,full=False):
    result={'id':row.id,'name':row.name,'enabled':row.enabled,'latest_version':row.latest_version,'version':rev.version,
            'digest':rev.digest,'status':rev.status,'description':rev.manifest['description'],'manifest':{k:v for k,v in rev.manifest.items() if k!='body'},'created_at':rev.created_at}
    if full:result['files']=rev.files
    return result

def freeze_bindings(s,bindings):
    frozen=[]
    for binding in bindings:
        if not binding.get('enabled',True): continue
        row=s.get(Skill,binding['skill_id']);rev=s.get(SkillRevision,f'{binding["skill_id"]}:{binding["version"]}')
        if not row or not row.enabled or not rev or rev.status!='approved': raise ValueError('Agent绑定的Skill版本不存在、未批准或已撤销')
        from .skill_store import require_native_skill
        require_native_skill(s, row.id, rev.version)
        frozen.append({**binding,'name':row.name,'description':rev.manifest['description'],'digest':rev.digest,'manifest':rev.manifest,'files':rev.files})
    return frozen

def check_live(s,frozen):
    row=s.get(Skill,frozen['skill_id']);rev=s.get(SkillRevision,f'{frozen["skill_id"]}:{frozen["version"]}')
    if not row or not row.enabled or not rev or rev.status!='approved' or rev.digest!=frozen['digest']:
        raise ValueError('Skill已停用或该版本已撤销；停止后续模型调用，不继续使用旧指令')

def catalog(agent):
    return [{k:f.get(k) for k in ('skill_id','name','version','description','mode','parameters','allow_scripts')} for f in agent.get('skills',[])]

def initial_context(agent):
    return {'available_skills':catalog(agent),'active_skills':[{'name':f['name'],'version':f['version'],'digest':f['digest'],'instructions':f['manifest']['body'],'parameters':f.get('parameters',{}),'resources':[x['path'] for x in f['manifest']['files']]} for f in agent.get('skills',[]) if f['mode']=='always']}

async def invoke(name,args,run_id,key,operation,root,runtime,image):
    from .tools import command
    with session() as s:
        run=s.get(Run,run_id);task=s.get(Task,run_id+':'+key);agent=run.snapshot['agents'][task.agent_id]
        match=next((f for f in agent.get('skills',[]) if f['name']==args.get('name')),None)
        if not match: raise ValueError('该Agent未绑定此Skill')
        check_live(s,match)
        if name=='activate_skill':
            if set(args)!={'name'}: raise ValueError('Skill激活参数无效')
            output={'skill':match['name'],'version':match['version'],'digest':match['digest'],'instructions':match['manifest']['body'],'parameters':match.get('parameters',{}),'resources':match['manifest']['files'],'authority':'procedure only; no new permissions'}
            emit(s,run_id,'skill.activated',{'agent_id':task.agent_id,'task_key':key,'skill_id':match['skill_id'],'name':match['name'],'version':match['version'],'digest':match['digest']})
            return output
        path=args.get('path')
        if not isinstance(path,str) or path not in match['files']: raise ValueError('Skill资源不存在')
        text=match['files'][path]
        if name=='read_skill_resource':
            if set(args)!={'name','path'}: raise ValueError('资源参数无效')
            emit(s,run_id,'skill.resource.read',{'agent_id':task.agent_id,'task_key':key,'name':match['name'],'version':match['version'],'path':path})
            return {'path':path,'content':text,'sha256':hashlib.sha256(text.encode()).hexdigest()}
        if name!='run_skill_script' or set(args)!={'name','path','arguments'}: raise ValueError('Skill工具参数无效')
        if task.kind not in ('develop', 'autonomous') or 'run_command' not in agent['tools'] or not match.get('allow_scripts'):
            raise ValueError('此阶段或绑定未授权Skill脚本执行；Skill不会提升角色权限')
        argv=args['arguments']
        if not path.startswith('scripts/') or not isinstance(argv,list) or len(argv)>30 or any(not isinstance(x,str) or len(x)>1000 or '\x00' in x for x in argv): raise ValueError('脚本路径或参数非法')
        ext=PurePosixPath(path).suffix
        interpreter={'.py':['python','-c'],'.js':['node','-e'],'.sh':['sh','-c']}.get(ext)
        if not interpreter:raise ValueError('本版脚本入口支持.py、.js、.sh；实际依赖须由项目镜像提供')
        # Single-file scripts intentionally execute with project cwd; no host materialization.
        line=shlex.join(interpreter+[text]+(['skill-script'] if ext=='.sh' else [])+argv)
        emit(s,run_id,'skill.script.started',{'agent_id':task.agent_id,'task_key':key,'name':match['name'],'version':match['version'],'path':path,'sha256':hashlib.sha256(text.encode()).hexdigest()})
    output=await command(run_id,key,operation,line,runtime,image)
    with session() as s:emit(s,run_id,'skill.script.completed',{'agent_id':task.agent_id,'task_key':key,'name':match['name'],'version':match['version'],'path':path,'exit_code':output.get('exit_code')})
    return output
