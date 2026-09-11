"""Authenticated collaboration and Skill APIs, registered before the static fallback."""
import copy,json
from fastapi import Depends,HTTPException,Request,Response,Header,Query
from sqlalchemy import select,update
from .db import (session,Skill,SkillRevision,Agent,Run,Task,Event,HumanInstruction,
                 ActionItem,DecisionRequest,emit,uid,now)
from .schema import SkillInput,HumanAnswer,HumanMessage
from . import skills,engagement

def register(app):
    from .api import current_user,admin,developer,run_access,event_out

    @app.get('/api/skills')
    def list_skills(user=Depends(current_user)):
        with session() as s:
            return [skills.revision_out(row,s.get(SkillRevision,f'{row.id}:{row.latest_version}')) for row in s.scalars(select(Skill).order_by(Skill.name))]

    @app.post('/api/skills',status_code=201)
    def create_skill(data:SkillInput,user=Depends(admin)):
        with session() as s:
            row,rev=skills.save(s,data.files,user.id)
            return skills.revision_out(row,rev,True)

    @app.post('/api/skills/import',status_code=201)
    async def import_skill(request:Request,user=Depends(admin)):
        content=bytearray()
        async for part in request.stream():
            content.extend(part)
            if len(content)>skills.MAX_BUNDLE:raise ValueError('压缩包超过1.5MB')
        with session() as s:
            row,rev=skills.save(s,skills.import_zip(bytes(content)),user.id)
            return skills.revision_out(row,rev,True)

    @app.get('/api/skills/{skill_id}')
    def get_skill(skill_id:str,version:int|None=None,user=Depends(current_user)):
        with session() as s:
            row=s.get(Skill,skill_id)
            if not row:raise HTTPException(404,'Skill不存在')
            rev=s.get(SkillRevision,f'{row.id}:{version or row.latest_version}')
            if not rev:raise HTTPException(404,'Skill版本不存在')
            return {**skills.revision_out(row,rev,True),'versions':[{'version':r.version,'status':r.status,'digest':r.digest} for r in s.scalars(select(SkillRevision).where(SkillRevision.skill_id==row.id).order_by(SkillRevision.version.desc()))]}

    @app.put('/api/skills/{skill_id}')
    def revise_skill(skill_id:str,data:SkillInput,user=Depends(admin)):
        with session() as s:
            row,rev=skills.save(s,data.files,user.id,skill_id,data.expected_version)
            return skills.revision_out(row,rev,True)

    @app.post('/api/skills/{skill_id}/review')
    def review_skill(skill_id:str,data:dict,user=Depends(admin)):
        if set(data)!={'version','digest','action'} or data['action'] not in ('approve','revoke'):raise ValueError('需指定version、digest和approve/revoke')
        with session() as s:
            row=s.get(Skill,skill_id);rev=s.get(SkillRevision,f'{skill_id}:{data["version"]}')
            if not row or not rev:raise HTTPException(404,'Skill版本不存在')
            if data['digest']!=rev.digest:raise HTTPException(409,'Skill内容摘要不匹配，请刷新后确认')
            if data['action']=='approve':
                from .skill_store import require_native_skill
                require_native_skill(s, row.id, rev.version)
            rev.status='approved' if data['action']=='approve' else 'revoked';rev.approved_by=user.id
            emit(s,None,'skill.reviewed',{'actor':user.id,'skill_id':skill_id,'version':rev.version,'digest':rev.digest,'action':data['action']})
            return skills.revision_out(row,rev,True)

    @app.post('/api/skills/{skill_id}/enabled')
    def enable_skill(skill_id:str,data:dict,user=Depends(admin)):
        if set(data)!={'enabled'} or type(data['enabled']) is not bool:raise ValueError('enabled应为布尔值')
        with session() as s:
            row=s.get(Skill,skill_id)
            if not row:raise HTTPException(404,'Skill不存在')
            row.enabled=data['enabled'];emit(s,None,'skill.enabled',{'actor':user.id,'skill_id':row.id,'enabled':row.enabled})
            return {'enabled':row.enabled}

    @app.get('/api/skills/{skill_id}/export')
    def export_skill(skill_id:str,version:int|None=None,user=Depends(current_user)):
        with session() as s:
            row=s.get(Skill,skill_id)
            if not row:raise HTTPException(404,'Skill不存在')
            rev=s.get(SkillRevision,f'{skill_id}:{version or row.latest_version}')
            if not rev:raise HTTPException(404,'Skill版本不存在')
            return Response(content=skills.export_zip(rev),media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="{row.name}-v{rev.version}.zip"'})

    @app.post('/api/commands/resolve')
    def resolve_command(data:dict,user=Depends(developer)):
        if not isinstance(data.get('text'),str) or len(data['text'])>30000:raise ValueError('指令文本无效')
        with session() as s:return engagement.resolve_target(s,data['text'],data.get('entry_agent_id'))

    @app.get('/api/runs/{run_id}/collaboration')
    def collaboration(run_id:str,user=Depends(current_user)):
        with session() as s:return engagement.overview(s,run_access(s,run_id,user))

    @app.post('/api/runs/{run_id}/questions/{request_id}/answer')
    def answer(run_id:str,request_id:str,data:HumanAnswer,user=Depends(developer)):
        with session() as s:
            run = run_access(s,run_id,user)
            resource = engagement.decision_resources(s, run_id).get(request_id)
            if resource:
                route = '/api/tool-installations/' if resource['kind'] == 'tool_installation' else '/api/execution/permissions/'
                raise HTTPException(409, '这是工具或执行权限申请，请管理员在专用审批入口核对内容后处理；普通回答不能授权或拒绝此申请。入口：' + route + resource['resource_id'] + '/review')
            try:return engagement.answer_request(s,run,request_id,data,user)
            except ValueError as exc:
                if any(x in str(exc) for x in ('已回答','摘要','已被其他','已结束')):raise HTTPException(409,str(exc))
                raise

    @app.post('/api/runs/{run_id}/instructions',status_code=202)
    def instruction(run_id:str,data:HumanMessage,user=Depends(developer),idempotency_key:str|None=Header(None)):
        body=data.body.strip()
        if not body:raise ValueError('补充说明不能为空')
        with session() as s:
            run=run_access(s,run_id,user)
            if run.status in ('completed','cancelled','needs_attention','awaiting_acceptance'):raise HTTPException(409,'当前阶段不能追加说明；请新建后续运行')
            if len(set(data.recipients))!=len(data.recipients) or not set(data.recipients)<=set(run.snapshot['agents']):raise ValueError('收件人必须是本次运行参与者且不能重复')
            mid=engagement.digest([run.id,user.id,idempotency_key]) if idempotency_key else uid()
            existing=s.get(HumanInstruction,mid)
            if existing:
                if existing.body!=body or existing.recipients!=data.recipients:raise HTTPException(409,'重复键对应不同说明')
                return {'id':mid,'status':'persisted'}
            count=s.scalar(select(__import__('sqlalchemy').func.count()).select_from(HumanInstruction).where(HumanInstruction.run_id==run.id))
            if count>=100:raise ValueError('本次运行补充说明达到100条上限')
            h=HumanInstruction(id=mid,run_id=run.id,author_id=user.id,recipients=data.recipients,body=body);s.add(h)
            emit(s,run.id,'human.instruction.sent',{'instruction_id':mid,'actor':user.id,'recipients':data.recipients,'body':body,'summary':body[:300]})
            return {'id':mid,'status':'persisted','effect':'加入所选成员后续模型调用的上下文；不重启完成任务，不改变权限或已批准的验收范围'}

    @app.get('/api/runs/{run_id}/sessions')
    def sessions(run_id:str,agent_id:str='',q:str='',offset:int=Query(0,ge=0),limit:int=Query(60,ge=1,le=100),user=Depends(current_user)):
        if len(q)>200:raise ValueError('检索词过长')
        with session() as s:
            run=run_access(s,run_id,user)
            query=select(Task).where(Task.run_id==run_id)
            if agent_id:
                if agent_id not in run.snapshot['agents']:raise ValueError('成员不在本次运行')
                query=query.where(Task.agent_id==agent_id)
            rows=list(s.scalars(query.order_by(Task.started_at,Task.key)))
            if q:rows=[t for t in rows if q.casefold() in json.dumps({'title':t.title,'result':t.result,'messages':[{'content':m.get('content',''),'calls':m.get('calls',[])} for m in t.messages if m['role'] in ('assistant','tool')]},ensure_ascii=False).casefold()]
            items=[{'key':t.key,'agent_id':t.agent_id,'title':t.title,'kind':t.kind,'status':t.status,'meeting_id':t.spec.get('meeting_id'),'message_count':len([m for m in t.messages if m['role'] in ('assistant','tool')]),'summary':t.result.get('summary','')} for t in rows[offset:offset+limit]]
            return {'items':items,'total':len(rows),'has_more':len(rows)>offset+limit,'next_offset':offset+len(items)}

    @app.get('/api/runs/{run_id}/sessions/{key}')
    def session_detail(run_id:str,key:str,offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100),user=Depends(current_user)):
        with session() as s:
            run=run_access(s,run_id,user);task=s.get(Task,run_id+':'+key)
            if not task:raise HTTPException(404,'会话不存在')
            public=[]
            for index,m in enumerate(task.messages or []):
                # Never expose system text, provider-native thinking blocks or credentials.
                if m['role'] not in ('assistant','tool'):continue
                text=m.get('content','');calls=copy.deepcopy(m.get('calls',[]))
                for c in calls:
                    if 'content' in c.get('arguments',{}):c['arguments']['content']=f'<{len(c["arguments"]["content"])} chars; see actual project artifact>'
                public.append({'index':index,'role':m['role'],'content':text[:20000],'truncated':len(text)>20000,'calls':calls,'call_id':m.get('call_id')})
            return {'task_key':key,'agent_id':task.agent_id,'status':task.status,'spec':task.spec,'result':task.result,'items':public[offset:offset+limit],
                    'total':len(public),'has_more':len(public)>offset+limit,'next_offset':offset+min(limit,max(0,len(public)-offset))}

    @app.get('/api/runs/{run_id}/trace')
    def trace(run_id:str,after:int=Query(0,ge=0),agent_id:str='',meeting_id:str='',q:str='',communication_only:bool=False,limit:int=Query(100,ge=1,le=300),user=Depends(current_user)):
        if len(q)>200:raise ValueError('检索词过长')
        with session() as s:
            run=run_access(s,run_id,user)
            taskkeys={t.key for t in s.scalars(select(Task).where(Task.run_id==run_id)) if (not agent_id or t.agent_id==agent_id) and (not meeting_id or t.spec.get('meeting_id')==meeting_id)}
            criteria=[Event.run_id==run_id,Event.id>after]
            communication_kinds=('agent.message','agent.message.delivered','task.handoff','task.delegated','orchestration.handoff')
            if communication_only:criteria.append(Event.kind.in_(communication_kinds))
            source=list(s.scalars(select(Event).where(*criteria).order_by(Event.id).limit(1001)))
            page=[];cursor=after
            for e in source[:1000]:
                cursor=e.id;d=e.data
                if agent_id and not (agent_id in [d.get('agent_id'),d.get('from_agent_id'),d.get('to_agent_id'),d.get('host_id')] or agent_id in d.get('participants',[]) or agent_id in d.get('recipients',[]) or d.get('task_key') in taskkeys):continue
                if meeting_id and d.get('meeting_id')!=meeting_id and d.get('task_key') not in taskkeys:continue
                if q and q.casefold() not in (e.kind+' '+json.dumps(d,ensure_ascii=False)).casefold():continue
                page.append(event_out(e))
                if len(page)==limit:break
            remaining=[Event.run_id==run_id,Event.id>cursor]
            if communication_only:remaining.append(Event.kind.in_(communication_kinds))
            more=bool(s.scalar(select(Event.id).where(*remaining).limit(1)))
            return {'items':page,'next_cursor':cursor,'has_more':more}
