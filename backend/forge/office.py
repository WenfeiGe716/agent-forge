"""Read-only global office projection and display settings.
No visual activity writes business task state or invokes a model/tool.
"""
from datetime import datetime, timezone
from sqlalchemy import select, func
from .db import Agent, Project, Run, Task, Event, PlatformConfig, Meeting, emit, now

OFFICE_DEFAULTS = {
    'idle_enabled': True, 'idle_grace_s': 35, 'idle_stagger_s': 25,
    'idle_cooldown_s': 30, 'walk_speed': 2.3,
    'activities': ['drink', 'phone', 'restroom', 'nap', 'stretch'],
    'durations_s': {'drink': 9, 'phone': 13, 'restroom': 14, 'nap': 16, 'stretch': 7},
}
FINISHED = {'completed', 'cancelled'}
WORK_PHASES = {'agent.started': 'thinking', 'model.completed': 'thinking', 'model.fallback': 'thinking'}
TOOL_PHASES = {'read_file': 'reading', 'list_files': 'reading', 'search_files': 'reading',
               'write_file': 'writing', 'apply_patch': 'writing', 'run_command': 'testing',
               'send_message': 'communicating'}
LABELS = {'idle':'空闲', 'waiting':'等待分派', 'approval':'等待审批', 'paused':'已暂停',
          'attention':'需人工处理', 'thinking':'思考中', 'reading':'阅读代码',
          'writing':'修改代码', 'testing':'执行命令', 'reviewing':'代码审查',
          'communicating':'发送消息', 'reserved':'其他任务占用'}


def settings_out(s):
    row=s.get(PlatformConfig,'office_visual')
    if row:
        return {**OFFICE_DEFAULTS, **row.value}
    return {**OFFICE_DEFAULTS, 'revision': 0, 'epoch': '2026-01-01T00:00:00+00:00'}


def save_settings(s, patch, actor):
    allowed=set(OFFICE_DEFAULTS)
    if not isinstance(patch,dict) or set(patch)-allowed:
        raise ValueError('未知场景配置字段')
    current=settings_out(s)
    value={**current, **patch}
    if type(value['idle_enabled']) is not bool:
        raise ValueError('idle_enabled必须为布尔值')
    for key,low,high in [('idle_grace_s',0,3600),('idle_stagger_s',0,600),('idle_cooldown_s',0,3600),('walk_speed',.3,5)]:
        x=value[key]
        if isinstance(x,bool) or not isinstance(x,(int,float)) or not low<=x<=high:
            raise ValueError(f'{key}必须在{low}至{high}之间')
    activities=value['activities']
    if not isinstance(activities,list) or len(set(activities))!=len(activities) or any(a not in OFFICE_DEFAULTS['activities'] for a in activities):
        raise ValueError('空闲动作必须为不重复的受支持动作列表')
    durations=value['durations_s']
    if not isinstance(durations,dict) or set(durations)!=set(OFFICE_DEFAULTS['activities']):
        raise ValueError('必须配置全部空闲动作时长')
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not 2<=v<=300 for v in durations.values()):
        raise ValueError('空闲动作时长必须在2至300秒之间')
    value['revision']=current.get('revision',0)+1
    value['epoch']=current.get('epoch') or now()
    row=s.get(PlatformConfig,'office_visual')
    if row: row.value=value
    else: s.add(PlatformConfig(id='office_visual',value=value))
    emit(s,None,'office.settings.updated',{'actor':actor,'fields':list(patch),'revision':value['revision']})
    return value


def event_out(e):
    return {'id':e.id,'run_id':e.run_id,'kind':e.kind,'created_at':e.created_at,'data':e.data}


def project_visible(p,user):
    return p is not None and (user.role=='admin' or p.owner_id==user.id or user.id in p.config.get('members',[]))


def office_snapshot(s,user,after=None,limit=200):
    """Project ALL active runs, not the selected run / first page of the run list.
    Hidden projects expose only a coarse reservation bit; never IDs or messages.
    Read scopes are bounded by the current local-team dataset, not truncated lists.
    """
    stamp=now(); config=settings_out(s)
    agents=list(s.scalars(select(Agent).order_by(Agent.id)))
    projects={p.id:p for p in s.scalars(select(Project))}
    runs=list(s.scalars(select(Run).order_by(Run.updated_at,Run.id)))
    visible_run_ids=[r.id for r in runs if project_visible(projects.get(r.project_id),user)]
    active=[r for r in runs if r.status not in FINISHED]
    active_ids=[r.id for r in active]
    tasks=list(s.scalars(select(Task).where(Task.run_id.in_(active_ids)))) if active_ids else []
    # Grouping on the indexed run_id prevents replaying every historical event.
    latest_per_agent={}
    if active_ids:
        last_rows=s.execute(select(Event.run_id,Event.data,Event.id,Event.kind,Event.created_at)
            .where(Event.run_id.in_(active_ids)).order_by(Event.id)).all()
        for rid,data,eid,kind,created in last_rows:
            aid=data.get('agent_id')
            if aid and (kind in WORK_PHASES or kind in {'tool.started','tool.completed','task.finished'}):
                latest_per_agent[(rid,aid)]={'id':eid,'kind':kind,'data':data,'at':created}
    records={}
    for agent in agents:
        records[agent.id]={'agent_id':agent.id,'busy':False,'phase':'idle','label':'空闲',
            'idle_since':config.get('epoch',stamp),'run_id':None,'task_key':None,
            'evidence_ids':[], 'scope':'global','tool':None,'run_count':0,'updated_at':stamp}
    rank={'attention':80,'paused':75,'reserved':70,'approval':60,'testing':100,'writing':100,
          'reading':95,'reviewing':95,'communicating':92,'thinking':90,'waiting':20,'idle':0}
    for run in runs:
        members=set(run.snapshot.get('agents',{}))
        if run.snapshot.get('execution_strategy') == 'autonomous' and run.status not in FINISHED:
            run_tasks = [t for t in tasks if t.run_id == run.id and t.kind == 'autonomous']
            members = {t.agent_id for t in run_tasks if t.status not in ('done', 'integrated')
                or (t.result.get('outcome') == 'waiting' and not t.spec.get('resumed_by'))}
            if run.status in ('queued', 'awaiting_acceptance'):
                members.add(run.snapshot['entry_agent_id'])
            for meeting in s.scalars(select(Meeting).where(Meeting.run_id == run.id, Meeting.status == 'active')):
                members.update(meeting.participants)
        if run.snapshot.get('collaboration_flow') and run.status in ('queued','intake','awaiting_decision','discussing'):
            from .engagement import effective_graph
            m=s.get(Meeting,run.id+':meeting')
            if m and m.status=='active': members=set(m.participants)
            elif not m: members={run.snapshot['entry_agent_id']}
        if run.status in FINISHED:
            for aid in members:
                if aid in records and not records[aid]['busy'] and project_visible(projects.get(run.project_id),user):
                    records[aid]['idle_since']=max(records[aid]['idle_since'],run.updated_at)
            continue
        known=project_visible(projects.get(run.project_id),user)
        for aid in members:
            if aid not in records: continue
            old=records[aid]; old['run_count']+=int(known)
            atasks=sorted([t for t in tasks if t.run_id==run.id and t.agent_id==aid],key=lambda t:(t.started_at,t.key,t.id))
            event=latest_per_agent.get((run.id,aid))
            running=next((t for t in atasks if t.status=='running' and event and event['data'].get('task_key')==t.key),None) or next((t for t in atasks if t.status=='running'),None)
            phase='waiting'; evidence=[]; tool=None
            if not known: phase='reserved'
            elif run.status=='needs_attention': phase='attention'
            elif run.status=='paused': phase='paused'
            elif run.status.startswith('awaiting_'): phase='approval'
            elif running:
                phase='reviewing' if running.kind=='review' else 'thinking'
                if event and event['data'].get('task_key')==running.key:
                    evidence=[event['id']]
                    if event['kind']=='tool.started':
                        tool=event['data'].get('tool');phase=TOOL_PHASES.get(tool,'thinking')
                    elif event['kind']=='tool.completed': phase='thinking'
                    elif event['kind']=='task.finished': phase='waiting'
            elif run.status=='planning' and run.snapshot.get('planner_id')==aid: phase='thinking'
            elif run.status=='reviewing' and run.snapshot.get('agents',{}).get(aid,{}).get('role')=='reviewer': phase='reviewing'
            candidate={'busy':True,'phase':phase,'label':('目标证据复核' if known and running and running.kind=='goal_review' and phase=='thinking' else LABELS[phase]), 'idle_since':None,
                'run_id':run.id if known else None,
                'task_key':running.key if running and known else None,
                'evidence_ids':evidence if known else [],
                'scope':'authorized' if known else 'reserved', 'tool':tool if known else None,
                'updated_at':run.updated_at if known else stamp}
            if not old['busy'] or rank[phase]>rank[old['phase']] or (rank[phase]==rank[old['phase']] and (candidate['run_id'] or '')<(old['run_id'] or '')):
                old.update(candidate)
    events=[];more=False
    max_visible=s.scalar(select(func.max(Event.id)).where(Event.run_id.in_(visible_run_ids))) if visible_run_ids else 0
    if after is not None and visible_run_ids:
        fetched=list(s.scalars(select(Event).where(Event.run_id.in_(visible_run_ids),Event.id>after).order_by(Event.id).limit(limit+1)))
        more=len(fetched)>limit;events=[event_out(e) for e in fetched[:limit]]
    cursor=events[-1]['id'] if events else (max_visible or 0) if after is None else after
    # Unknown states conservatively reserve the actor: no idle animation by inference.
    from .engagement import meeting_out
    meetings=[meeting_out(s,m,next(r for r in runs if r.id==m.run_id)) for m in s.scalars(select(Meeting).where(Meeting.run_id.in_(visible_run_ids))) if m.status=='active'] if visible_run_ids else []
    return {'meetings':meetings,'server_time':stamp,'cursor':cursor,'has_more':more,'events':events,
            'presence':list(records.values()),'settings':config,'complete':True,
            'read_only_projection':True}
