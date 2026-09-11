"""Durable human decisions, directed input, meetings and actionable provenance.
All visuals are projections. No animation calls this module to mutate execution.
"""
import copy, hashlib, json, re
from sqlalchemy import select, update, func
from .db import (session, Run, Task, Step, Agent, DecisionRequest, HumanInstruction,
                 InstructionReceipt, Meeting, ActionItem, Outbox, emit, now)
from .schema import DecisionQuestion, IntakeResult, Plan

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def can_communicate(agents,sender,recipient):
    if sender==recipient:return True
    a,b=agents.get(sender),agents.get(recipient)
    if not a or not b:return False
    policy=a.get('communication') or {'scope':'run'}
    mode=policy.get('scope','run')
    if mode=='run':return True
    if mode=='department':return bool(a.get('department')) and a.get('department')==b.get('department')
    if mode=='custom':return recipient in policy.get('recipients',[])
    if mode=='reporting':return recipient in a.get('reporting_chain',[]) or sender in b.get('reporting_chain',[])
    return False

def require_communication(agents,sender,recipient):
    if not can_communicate(agents,sender,recipient): raise ValueError('通信范围不允许该成员直接联系接收者；请管理员调整策略后创建新运行')

def validate_meeting(agents,participants):
    if len(participants)<2 or len(set(participants))!=len(participants) or not set(participants)<=set(agents):
        raise ValueError('会议需至少两位不同的已授权运行成员')
    for a in participants:
        for b in participants: require_communication(agents,a,b)

def effective_graph(run):
    return (run.report or {}).get('meeting_graph') or run.snapshot.get('orchestration')

def request_decision(run_id,key,operation,arguments):
    question=DecisionQuestion.model_validate(arguments).model_dump()
    rid=hashlib.sha256((run_id+':decision:'+operation).encode()).hexdigest()
    bound=digest({'run_id':run_id,'task_key':key,'question':question})
    with session() as s:
        s.execute(update(Run).where(Run.id==run_id).values(updated_at=Run.updated_at))
        run=s.get(Run,run_id);task=s.get(Task,run_id+':'+key)
        if not run or not task or run.status in ('completed','cancelled','needs_attention'):raise ValueError('不能在已结束运行提问')
        old=s.get(DecisionRequest,rid)
        if old:
            if old.digest!=bound:raise ValueError('同一决策操作不得改变问题')
            return decision_out(old)
        count=s.scalar(select(func.count()).select_from(DecisionRequest).where(DecisionRequest.run_id==run_id))
        if count>=100:raise ValueError('本次运行的人工决策请求数量已达上限100')
        req=DecisionRequest(id=rid,run_id=run_id,task_key=key,agent_id=task.agent_id,digest=bound,question=question)
        s.add(req);s.flush()
        emit(s,run_id,'human.decision.requested',{'request_id':rid,'task_key':key,'agent_id':task.agent_id,'digest':bound,**question})
        return decision_out(req)

def decision_out(d):
    return {'id':d.id,'request_id':d.id,'run_id':d.run_id,'task_key':d.task_key,'agent_id':d.agent_id,'digest':d.digest,
            'status':d.status,'question':d.question,'answer':d.answer,'answered_by':d.answered_by,'created_at':d.created_at,'answered_at':d.answered_at}

def answer_request(s,run,request_id,data,user):
    row=s.get(DecisionRequest,request_id)
    if not row or row.run_id!=run.id: raise ValueError('决策请求不存在')
    if data.digest!=row.digest:raise ValueError('问题摘要已变化，请重新读取')
    answer={'choice_id':data.choice_id,'text':data.text.strip(),'reject':data.reject}
    if row.status!='pending':
        if row.answer==answer:return decision_out(row)
        raise ValueError('问题已回答，不能覆盖历史决定')
    if run.status in ('completed','cancelled','needs_attention'):raise ValueError('运行已结束，不能回答旧问题')
    choices={x['id']:x['label'] for x in row.question['options']}
    if not data.reject:
        if data.choice_id and data.choice_id not in choices:raise ValueError('不存在的决策选项')
        if data.text.strip() and not row.question['allow_text']:raise ValueError('此问题不接受自由文本')
        if not data.choice_id and not data.text.strip():raise ValueError('请选择选项或输入回答')
    changed=s.execute(update(DecisionRequest).where(DecisionRequest.id==row.id,DecisionRequest.status=='pending').values(status='rejected' if data.reject else 'answered',answer=answer,answered_by=user.id,answered_at=now()).execution_options(synchronize_session=False))
    if changed.rowcount!=1:raise ValueError('决策已被其他用户回答，请刷新')
    s.expire(row);s.refresh(row)
    s.add(Outbox(run_id=run.id,action='respond',payload={'request_id':row.id,'digest':row.digest,'rejected':data.reject}))
    emit(s,run.id,'human.decision.answered',{'request_id':row.id,'task_key':row.task_key,'agent_id':row.agent_id,'actor':user.id,'digest':row.digest,'answer':answer,'choice_label':choices.get(data.choice_id,'')})
    return decision_out(row)

def decision_execution_context(s, decision, task):
    """Rehydrate typed references from the scoped ledger, never from user prose.

    Legacy saved histories may already have lost these fields. A decision ID is
    not executable authority; installation/command tools still validate the grant.
    """
    if decision.task_key != task.key or decision.agent_id != task.agent_id:
        return {}
    from .tool_installations import ToolInstallation
    from .execution_permissions import ExecutionPermission
    scope = lambda model: (model.run_id == decision.run_id, model.task_key == task.key,
                           model.agent_id == task.agent_id, model.decision_id == decision.id)
    installation = s.scalar(select(ToolInstallation).where(*scope(ToolInstallation)))
    if installation:
        original = installation
        if installation.status == 'superseded':
            installation = s.get(ToolInstallation, installation.result.get('reused_installation_id', ''))
            if (not installation or installation.run_id != original.run_id or installation.task_key != original.task_key
                    or installation.agent_id != original.agent_id or installation.manifest != original.manifest):
                return {}
        result = {'installation_id': installation.id, 'installation_status': installation.status,
                  'package_id': installation.manifest['id'], 'version': installation.manifest['version'],
                  'source_sha256': installation.manifest['sha256'], 'scope': 'this run/task/agent only'}
        if original.id != installation.id:
            result['reused_approval_request_id'] = installation.decision_id
        if installation.status == 'approved':
            result['next_tool'] = {'name': 'install_approved_tool', 'arguments': {'installation_id': installation.id}}
        return result
    permission = s.scalar(select(ExecutionPermission).where(*scope(ExecutionPermission)))
    if permission:
        result = {'permission_id': permission.id, 'permission_status': permission.status,
                  'command': permission.command, 'network': permission.network, 'paths': permission.paths,
                  'scope': 'one exact command in this run/task/agent only'}
        if permission.status == 'approved':
            result['next_tool'] = {'name': 'run_approved_command', 'arguments': {'permission_id': permission.id}}
        return result
    return {}


def prepare_context(run,task,messages):
    """Return bounded pending human guidance and replace only answered tool requests.
    Receipts are committed after a successful provider reply, not before a request.
    """
    messages=copy.deepcopy(messages);inclusions=[]
    with session() as s:
        decisions=list(s.scalars(select(DecisionRequest).where(DecisionRequest.run_id==run.id).order_by(DecisionRequest.created_at,DecisionRequest.id)))
        answered={d.id:d for d in decisions if d.status in ('answered', 'superseded')}
        execution={rid:decision_execution_context(s,d,task) for rid,d in answered.items()}
        for message in messages:
            if message['role']!='tool':continue
            try:body=json.loads(message['content'])
            except (ValueError,TypeError):continue
            rid=body.get('request_id') if isinstance(body,dict) else None
            if rid in answered:
                message['content']=json.dumps({'request_id':rid,'status':answered[rid].status,
                    'question':answered[rid].question,'answer':answered[rid].answer,**execution[rid]},ensure_ascii=False)
        guidance=[]
        for d in decisions:
            receipt='decision:'+d.id+':'+task.id
            if d.id in answered and not s.get(InstructionReceipt,receipt):
                guidance.append({'decision_id':d.id,'question':d.question,'answer':d.answer,
                    'decision_status':d.status,'authorized_user':d.answered_by,**execution[d.id]});inclusions.append((receipt,d.id,'human.decision.included'))
        for h in s.scalars(select(HumanInstruction).where(HumanInstruction.run_id==run.id).order_by(HumanInstruction.created_at,HumanInstruction.id)):
            receipt='instruction:'+h.id+':'+task.id
            if task.agent_id in h.recipients and not s.get(InstructionReceipt,receipt):
                guidance.append({'instruction_id':h.id,'body':h.body,'authorized_user':h.author_id});inclusions.append((receipt,h.id,'human.instruction.included'))
        if guidance:messages.append({'role':'user','content':'Authorized user answers and supplementary guidance. They do not expand tool permissions or replace approved acceptance scope:\n'+json.dumps({'human_guidance':guidance},ensure_ascii=False)})
    return messages,inclusions

def mark_context(run_id,key,agent_id,inclusions):
    with session() as s:
        for ident,source,kind in inclusions:
            if not s.get(InstructionReceipt,ident):
                s.add(InstructionReceipt(id=ident,instruction_id=source,task_id=run_id+':'+key))
                emit(s,run_id,kind,{'task_key':key,'agent_id':agent_id,'source_id':source,'summary':'用户决定/说明已进入本次实际模型请求'})

def resolve_target(s,text,explicit=None):
    """Exact @name/@id or @{department}; ambiguity is an error, never guessed."""
    agents=list(s.scalars(select(Agent).where(Agent.config.is_not(None))))
    agents=[a for a in agents if a.config.get('enabled',True)]
    by_id={a.id:a for a in agents}
    targets=[]
    for token in re.findall(r'(?<!\S)@(?:\{([^}]+)\}|([^\s，,：:]+))',text):
        department,name=token
        matches=[a.id for a in agents if a.config.get('department')==department] if department else [a.id for a in agents if a.id==name or a.config['name']==name]
        if not matches or (not department and len(matches)!=1):raise ValueError('定向对象不存在或重名，请使用成员ID：'+(department or name))
        targets+=matches
    if explicit:
        if explicit not in by_id:raise ValueError('指令负责人不存在或已停用')
        if targets and explicit not in targets:raise ValueError('@对象与所选接收者不一致')
        entry=explicit
    elif targets:
        unique=list(dict.fromkeys(targets))
        planners=[aid for aid in unique if by_id[aid].config['role']=='planner']
        if len(unique)>1 and len(planners)!=1:raise ValueError('多个收件人需明确入口负责人，不按成员排序猜测')
        entry=unique[0] if len(unique)==1 else planners[0]
    else:
        choices=[a.id for a in agents if a.config['role']=='planner']
        if len(choices)!=1:raise ValueError('请选择唯一接收者，不能猜测负责人')
        entry=choices[0]
    return {'entry_agent_id':entry,'recipients':list(dict.fromkeys(targets or [entry])),
            'resolved':[{'id':aid,'name':by_id[aid].config['name'],'department':by_id[aid].config.get('department','')} for aid in dict.fromkeys(targets or [entry])]}

def open_meeting(data):
    from .engine import load,task_id
    run=load(data['run_id']);graph=copy.deepcopy(run.snapshot['orchestration']);host=run.snapshot.get('entry_agent_id') or graph['participants'][0]
    with session() as s:
        intake=s.get(Task,task_id(run.id,'intake')) if run.snapshot.get('collaboration_flow') else None
        charter=IntakeResult.model_validate(intake.result).model_dump() if intake else {'summary':run.title,'participants':graph['participants'],'agenda':graph['prompt']}
        if host not in charter['participants']:raise ValueError('会议必须包含指令负责人')
        if not set(charter['participants'])<=set(graph['participants']):raise ValueError('负责人只能召集本次授权协作池中的成员')
        validate_meeting(run.snapshot['agents'],charter['participants'])
        graph['participants']=charter['participants'];graph['prompt']=charter['agenda']
        if graph.get('assessor_id') not in graph['participants']:graph['assessor_id']=graph['participants'][-1]
        row=s.get(Run,run.id);row.report={**row.report,'meeting_graph':graph,'intake':charter}
        mid=run.id+':meeting';meeting=s.get(Meeting,mid)
        if not meeting:
            meeting=Meeting(id=mid,run_id=run.id,host_id=host,participants=graph['participants'],subject=run.title,agenda=charter['agenda'])
            s.add(meeting);emit(s,run.id,'meeting.opened',{'meeting_id':mid,'host_id':host,'participants':graph['participants'],'subject':run.title,'summary':charter['summary']})
        plan=s.get(Task,task_id(run.id,'plan'))
        if plan:plan.spec={**plan.spec,'intake':charter,'meeting_id':mid}
    return graph

def meeting_out(s,m,run):
    tasks=list(s.scalars(select(Task).where(Task.run_id==m.run_id)))
    speakers=[t.agent_id for t in tasks if t.spec.get('meeting_id')==m.id and t.status=='running']
    decisions=list(s.scalars(select(DecisionRequest).where(DecisionRequest.run_id==run.id,DecisionRequest.status=='pending')))
    state=m.status
    if state=='active':
        if run.status=='paused':state='paused'
        elif run.status in ('cancelled','needs_attention'):state=run.status
        elif decisions:state='awaiting_decision'
    return {'id':m.id,'run_id':m.run_id,'host_id':m.host_id,'participants':m.participants,'subject':m.subject,'agenda':m.agenda,'status':state,'speaker_ids':sorted(set(speakers)),
            'outcome':m.outcome,'created_at':m.created_at,'ended_at':m.ended_at,'source':'business'}

def close_meeting(run_id,discussion):
    with session() as s:
        m=s.get(Meeting,run_id+':meeting')
        if not m:return
        m.status='concluded' if discussion['stop_reason']=='goal_satisfied' else 'needs_attention';m.ended_at=now();m.outcome={'stop_reason':discussion['stop_reason'],'goal_checkpoint':discussion.get('goal_checkpoint',{})}
        emit(s,run_id,'meeting.closed',{'meeting_id':m.id,'status':m.status,'stop_reason':discussion['stop_reason']})
        for turn in discussion['turns']:
            for index,item in enumerate(turn.get('action_items',[])):
                if item['owner_id'] not in m.participants:raise ValueError('行动项负责人必须是会议参与者')
                aid=digest([run_id,turn['key'],index])
                if not s.get(ActionItem,aid):
                    s.add(ActionItem(id=aid,run_id=run_id,meeting_id=m.id,owner_id=item['owner_id'],specification=item,origin={'contribution_key':turn['key'],'agent_id':turn['agent_id']},status='proposed'))
                    emit(s,run_id,'action.proposed',{'action_id':aid,'meeting_id':m.id,'agent_id':item['owner_id'],'title':item['title'],'source_task_key':turn['key']})

def plan_actions(s,run,plan,proposal_only=False):
    mid=run.id+':meeting' if s.get(Meeting,run.id+':meeting') else ''
    for item in plan['tasks']:
        aid=run.id+':action:'+item['key']
        if s.get(ActionItem,aid):continue
        s.add(ActionItem(id=aid,run_id=run.id,meeting_id=mid,owner_id=item['agent_id'],task_key='' if proposal_only else item['key'],specification=item,status='proposed' if proposal_only else 'pending_approval',origin={'plan_digest':digest(plan),'meeting_id':mid,'plan_task_key':'action_plan' if proposal_only else 'plan'}))
        emit(s,run.id,'action.created',{'action_id':aid,'meeting_id':mid,'agent_id':item['agent_id'],'task_key':item['key'],'title':item['title'],'executable':not proposal_only})

def prepare_action_plan(data):
    from .engine import create_task,load
    run=load(data['run_id']);host=run.snapshot['entry_agent_id']
    return create_task(run.id,'action_plan','action_plan',host,'将会议决议整理为行动项',{'description':'Produce action items with exact owners, dependency keys and concrete acceptance criteria. These are proposals, not claims of implementation.', 'discussion':data['discussion'],'meeting_id':run.id+':meeting'})

def save_action_plan(data):
    with session() as s:
        run=s.get(Run,data['run_id']);task=s.get(Task,run.id+':action_plan');plan_actions(s,run,task.result,True)
        return {'actions':len(task.result['tasks'])}

def action_out(s,a,run):
    t=s.get(Task,run.id+':'+a.task_key) if a.task_key else None
    state=a.status
    if a.task_key:
        if run.status=='cancelled':state='cancelled'
        elif run.status=='completed':state='accepted'
        elif run.status=='awaiting_approval':state='pending_approval'
        elif t and t.status=='integrated':state='delivered'
        elif t and t.status in ('running','waiting_decision','paused','failed','needs_attention','done'):state=t.status
        elif run.status in ('running','verifying','reviewing','awaiting_acceptance'):state='ready'
    return {'id':a.id,'run_id':a.run_id,'meeting_id':a.meeting_id,'owner_id':a.owner_id,'task_key':a.task_key,'specification':a.specification,'status':state,'origin':a.origin,'created_at':a.created_at,'result':t.result if t else {}}

def decision_resources(s, run_id):
    """Route human action by durable authority records, never question wording."""
    from .tool_installations import ToolInstallation
    from .execution_permissions import ExecutionPermission
    resources = {}
    for model, kind in ((ToolInstallation, 'tool_installation'), (ExecutionPermission, 'execution_permission')):
        for row in s.scalars(select(model).where(model.run_id == run_id)):
            resources[row.decision_id] = {'kind': kind, 'resource_id': row.id}
    return resources


def overview(s,run):
    resources = decision_resources(s, run.id)
    return {'run_id':run.id,'entry_agent_id':run.snapshot.get('entry_agent_id',run.snapshot.get('planner_id')),
            'decisions':[{**decision_out(d), **resources.get(d.id, {'kind': 'question', 'resource_id': d.id})} for d in s.scalars(select(DecisionRequest).where(DecisionRequest.run_id==run.id).order_by(DecisionRequest.created_at))],
            'meetings':[meeting_out(s,m,run) for m in s.scalars(select(Meeting).where(Meeting.run_id==run.id))],
            'actions':[action_out(s,a,run) for a in s.scalars(select(ActionItem).where(ActionItem.run_id==run.id).order_by(ActionItem.created_at,ActionItem.id))],
            'instructions':[{'id':h.id,'body':h.body,'recipients':h.recipients,'author_id':h.author_id,'created_at':h.created_at} for h in s.scalars(select(HumanInstruction).where(HumanInstruction.run_id==run.id).order_by(HumanInstruction.created_at))]}
