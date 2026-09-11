"""Run-scoped, bounded agent messages. Text is data, never an authorization mechanism."""
import hashlib
from sqlalchemy import select, update, func
from .db import session, Run, Task, AgentMessage, emit, now


def send_message(run_id: str, task_key: str, operation: str, recipient: str, body: str, request_reply: bool | None = None) -> dict:
    if not body.strip() or len(body) > 3000:
        raise ValueError('协作消息须为1至3000字符')
    message_id = hashlib.sha256((run_id + ':' + operation).encode()).hexdigest()
    with session() as s:
        # Lock the run before counting to enforce the quota across concurrent senders.
        s.execute(update(Run).where(Run.id == run_id).values(updated_at=Run.updated_at))
        old = s.get(AgentMessage, message_id)
        task = s.get(Task, run_id + ':' + task_key)
        run = s.get(Run, run_id)
        if not task or not run or recipient not in run.snapshot['agents']:
            raise ValueError('消息接收者必须属于当前运行的参与团队')
        autonomous = run.snapshot.get('execution_strategy') == 'autonomous'
        if request_reply is not None and not autonomous:
            raise ValueError('只有团队自治运行支持消息唤醒')
        wake = autonomous and request_reply is not False
        if autonomous:
            modes = task.spec.get('message_wake_modes', {})
            if message_id in modes and modes[message_id] != wake:
                raise ValueError('同一消息操作不能改变是否请求回复')
            task.spec = {**task.spec, 'message_wake_modes': {**modes, message_id: wake}}
        from .engagement import require_communication
        require_communication(run.snapshot['agents'],task.agent_id,recipient)
        if recipient == task.agent_id:
            raise ValueError('不能向自己发送协作消息')
        if old:
            if old.recipient_id != recipient or old.body != body or old.sender_id != task.agent_id or old.task_key != task_key:
                raise ValueError('同一消息操作ID不能对应不同内容')
            from .autonomy import queue_message
            wake_key = queue_message(s, run, task, old, wake) if autonomous else None
            return {'message_id': old.id, 'status': 'queued' if not old.delivered_at else 'included_in_context', 'wake_task_key': wake_key}
        if run.status in ('completed', 'cancelled', 'needs_attention'):
            raise ValueError('运行已结束，不能继续发送消息')
        maximum = run.snapshot['runtime'].get('max_agent_messages', 200)
        count = s.scalar(select(func.count()).select_from(AgentMessage).where(AgentMessage.run_id == run_id))
        if count >= maximum:
            raise ValueError('达到本次运行的协作消息上限')
        message = AgentMessage(id=message_id, run_id=run_id, task_key=task_key, sender_id=task.agent_id, recipient_id=recipient, body=body)
        s.add(message)
        emit(s, run_id, 'agent.message', {'message_id': message_id, 'task_key': task_key,
             'from_agent_id': task.agent_id, 'to_agent_id': recipient, 'body': body, 'summary': body[:300]})
        from .autonomy import queue_message
        wake_key = queue_message(s, run, task, message, wake) if autonomous else None
        return {'message_id': message_id, 'status': 'queued', 'wake_task_key': wake_key,
                'note': '已排入接收者的新工作分配；不会重放其已完成操作。' if wake_key else '将在接收者下一次调用前加入上下文；信息通知不强制唤醒或回复。'}


def pending_messages(run_id: str, agent_id: str, limit: int = 30, allowed_task_keys: list[str] | None = None) -> list[dict]:
    with session() as s:
        run = s.get(Run, run_id)
        if not run or agent_id not in run.snapshot['agents']:
            raise ValueError('成员不属于当前运行')
        query=select(AgentMessage).where(AgentMessage.run_id == run_id,AgentMessage.recipient_id == agent_id,AgentMessage.delivered_at == '')
        if allowed_task_keys is not None: query=query.where(AgentMessage.task_key.in_(allowed_task_keys))
        rows=list(s.scalars(query.order_by(AgentMessage.created_at,AgentMessage.id).limit(limit)))
        return [{'message_id': m.id, 'from_agent_id': m.sender_id, 'from_name': run.snapshot['agents'][m.sender_id]['name'],
                 'task_key': m.task_key, 'body': m.body} for m in rows]


def mark_included(run_id: str, agent_id: str, task_key: str, messages: list[dict]) -> None:
    with session() as s:
        for item in messages:
            m = s.get(AgentMessage, item['message_id'])
            if m and m.run_id == run_id and m.recipient_id == agent_id and not m.delivered_at:
                m.delivered_at = now()
                emit(s, run_id, 'agent.message.delivered', {'message_id': m.id, 'task_key': task_key,
                     'from_agent_id': m.sender_id, 'to_agent_id': agent_id, 'summary': '协作消息已加入接收者本轮请求上下文'})
