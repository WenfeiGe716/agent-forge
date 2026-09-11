import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Literal
import httpx
from pydantic import Field, field_validator
from .config import settings
from .schema import Strict, Plan, GoalReview, DiscussionContribution, DecisionQuestion, IntakeResult, AutonomousFinish, TeamAssignment, TeamDiscussion
from .workspace import list_files, read_workspace_file, write_workspace_file, delete_workspace_file

def obj(properties, required):
    return {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}

S = {'type': 'string'}
DEFINITIONS = {
    'inspect_team': {'name': 'inspect_team', 'description': 'Inspect the frozen authorized team capability directory: responsibilities, Skills, own tool permissions and actual assignments. Omit member_id for summaries, or provide a member ID for the full description. Never grants another member\'s tools or exposes other runs.', 'parameters': obj({'member_id': S}, [])},
    'assign_work': {'name': 'assign_work', 'description': 'Coordinator only: assign concrete work to a member of this authorized team, with its goal and acceptance requirements. The scheduler wakes the member and delivers their actual result. Work uses only the recipient\'s existing permissions. Do not delegate duplicate work.', 'parameters': TeamAssignment.model_json_schema()},
    'start_discussion': {'name': 'start_discussion', 'description': 'Coordinator only: convene a necessary discussion with an explicit subset of at least two authorized members, including yourself. Does not prescribe rounds or change permissions. Use targeted send_message for simple questions.', 'parameters': TeamDiscussion.model_json_schema()},
    'end_discussion': {'name': 'end_discussion', 'description': 'Coordinator only: close this run\'s actual discussion after its participants finish and record its conclusion. Never means the whole task is complete.', 'parameters': obj({'meeting_id': S, 'summary': S}, ['meeting_id', 'summary'])},
    'inspect_environment': {'name':'inspect_environment','description':'Locate any task-relevant executable names in the ACTUAL selected local/container environment and project dependency bin directories. Does not execute candidate tools, install them, or grant permissions. Choose names based on the real project; no fixed application-tool catalog.','parameters':obj({'executables':{'type':'array','items':{'type':'string'},'minItems':1,'maxItems':60}},['executables'])},
    'request_decision': {'name':'request_decision','description':'Ask the authorized user an explicit question with options/context. Must be the only tool in this turn. The assigned task waits durably; do NOT guess the answer. No secrets or credentials.', 'parameters':DecisionQuestion.model_json_schema()},
    'request_tool_installation': {'name': 'request_tool_installation', 'description': 'Request administrator review of a fixed offline tool package/version for THIS task. Must be the only tool in this turn; wait for the administrator. No package URLs, network or host installation.', 'parameters': obj({'package_id': S, 'version': S, 'reason': S}, ['package_id', 'version', 'reason'])},
    'install_approved_tool': {'name': 'install_approved_tool', 'description': 'Install an already administrator-approved offline package in THIS task network-disabled sandbox. Never execute before approval.', 'parameters': obj({'installation_id': S}, ['installation_id'])},
    'run_installed_tool': {'name': 'run_installed_tool', 'description': 'Call an installed, digest-verified offline tool in THIS task sandbox on one workspace file.', 'parameters': obj({'installation_id': S, 'operation': {'type': 'string', 'enum': ['text_metrics']}, 'path': S}, ['installation_id', 'operation', 'path'])},
    'request_execution_permission': {'name': 'request_execution_permission', 'description': 'Request approval for ONE exact native command needing outbound network or extra exact file access. Call alone and wait. No directory/root/credential grants; never changes project permissions.', 'parameters': obj({'command': S, 'network': {'type': 'boolean'}, 'paths': {'type': 'array', 'items': obj({'path': S, 'access': {'type': 'string', 'enum': ['read', 'write']}}, ['path', 'access'])}, 'reason': S}, ['command', 'network', 'paths', 'reason'])},
    'run_approved_command': {'name': 'run_approved_command', 'description': 'Run one exact approved command once in THIS task. The approved command and permissions cannot be changed; repeated calls return the recorded outcome.', 'parameters': obj({'permission_id': S}, ['permission_id'])},
    'activate_skill': {'name':'activate_skill','description':'Load an assigned approved Skill into this task context. It never changes permissions.','parameters':obj({'name':S},['name'])},
    'read_skill_resource': {'name':'read_skill_resource','description':'Read one approved text resource from an assigned Skill.','parameters':obj({'name':S,'path':S},['name','path'])},
    'run_skill_script': {'name':'run_skill_script','description':'Run an explicitly granted single-file Skill script through the frozen project execution backend and permission profile. Python/JS/shell interpreter must exist there; scripts do not grant new permissions.','parameters':obj({'name':S,'path':S,'arguments':{'type':'array','items':S}},['name','path','arguments'])},
    'send_message': {'name': 'send_message', 'description': 'Send bounded collaboration data to another participant in THIS run. Delivered at their next turn; never changes permissions or wakes completed tasks.', 'parameters': obj({'recipient_id': S, 'body': S}, ['recipient_id', 'body'])},
    'list_files': {'name': 'list_files', 'description': 'List relative project files. Secret/config and cache paths are excluded.', 'parameters': obj({}, [])},
    'read_file': {'name': 'read_file', 'description': 'Read a UTF-8 file in this task workspace.', 'parameters': obj({'path': S}, ['path'])},
    'write_file': {'name': 'write_file', 'description': 'Create or replace a UTF-8 file in this isolated task workspace.', 'parameters': obj({'path': S, 'content': S}, ['path', 'content'])},
    'delete_file': {'name': 'delete_file', 'description': 'Delete one regular file in this isolated task workspace.', 'parameters': obj({'path': S}, ['path'])},
    'run_command': {'name': 'run_command', 'description': 'Execute in the frozen project execution mode. Native ask/auto uses Seatbelt, project-only writes and no network; container uses its configured sandbox. Only explicitly human-selected full access removes OS restrictions. No inherited platform credentials.', 'parameters': obj({'command': S}, ['command'])},
}

class ImplementationFinish(Strict):
    summary: str = Field(min_length=1)
    notes: str

    @field_validator('summary')
    @classmethod
    def meaningful_summary(cls, value):
        if not value.strip(): raise ValueError('缺少任务交付摘要')
        return value


class ReviewFinish(Strict):
    summary: str = Field(min_length=1)
    verdict: Literal['pass', 'rework']
    issues: list[str]


def finish_model(kind):
    return {'autonomous': AutonomousFinish, 'intake': IntakeResult, 'plan': Plan, 'action_plan': Plan,
            'discussion': DiscussionContribution, 'goal_review': GoalReview,
            'review': ReviewFinish}.get(kind, ImplementationFinish)


def finish_schema(kind):
    """Inline our acyclic local definitions so nested fields are visible upstream.

    This changes representation only: required fields, types, enums and limits
    still come from the exact model used to validate the finish submission.
    """
    schema = finish_model(kind).model_json_schema()
    definitions = schema.get('$defs', {})

    def expand(value, seen=()):
        if isinstance(value, list): return [expand(item, seen) for item in value]
        if not isinstance(value, dict): return value
        if '$ref' in value:
            ref = value['$ref']
            if not ref.startswith('#/$defs/') or ref in seen:
                raise ValueError('finish schema 含未支持的递归或外部引用')
            target = definitions[ref.removeprefix('#/$defs/')]
            return expand({**target, **{k: v for k, v in value.items() if k != '$ref'}}, (*seen, ref))
        return {key: expand(item, seen) for key, item in value.items()
                if key != '$defs' and not (key == 'title' and isinstance(item, str))}

    return expand(schema)


def validate_finish(kind, arguments):
    return finish_model(kind).model_validate(arguments, strict=True).model_dump()


def tool_definitions(allowed: list[str], kind: str) -> list[dict]:
    definitions = [copy.deepcopy(DEFINITIONS[k]) for k in allowed if k != 'finish']
    if kind == 'autonomous':
        for definition in definitions:
            if definition['name'] == 'send_message':
                definition['description'] = 'Contact a teammate directly in THIS run. Set request_reply=true for concrete help/work: a fresh activation wakes the recipient and their result returns to you. Set false for information only; no automatic acknowledgement loop. Never changes permissions. Finish with waiting if your own work depends on the reply.'
                definition['parameters'] = obj({'recipient_id': S, 'body': S, 'request_reply': {'type': 'boolean'}}, ['recipient_id', 'body', 'request_reply'])
        description = 'End ONLY this activation: done=your work delivered; waiting=await delegated work or colleague reply; ready=coordinator nominates complete run deliverables for HUMAN acceptance; blocked=explain a real unresolved blocker. Include actual deliverables (relative file paths), evidence_task_keys of completed integrated work, summary and notes. Never invent files or treat waiting/budget exhaustion as success.'
    elif kind == 'intake':
        description='After resolving missing user decisions, submit the exact authorized subset to convene and a concrete meeting agenda. Include yourself. Do not invent participants or pretend work is already done.'
    elif kind in ('plan','action_plan'):
        description = 'Submit summary and tasks for human approval. Each task uses key, title, description, agent_id, depends_on, acceptance; never id, assignee or dependencies. Use exact supplied agent IDs and dependency task keys. Do not assign the planner or reviewer as a developer. This proposal does not authorize execution.'
    elif kind == 'discussion':
        description = 'Submit findings and all unresolved issues. ready is ignored; the goal gate and human acceptance decide completion.'
    elif kind == 'goal_review':
        description = 'Evaluate every immutable criterion against the exact supplied transcript_digest. Cite recorded task evidence, not invented references. File evidence needs a read_file receipt with exact path and sha256. Declare unresolved issues or need for human input. This is a candidate for human review, not task success.'
    elif kind == 'review':
        description = 'Submit exactly summary (string), verdict (pass or rework), and issues (array of strings), grounded in actual files and the supplied test report. Never treat missing tests as passed.'
    else:
        description = 'Submit exactly summary (nonempty string) and notes (string) to finish this implementation subtask. This does NOT mark the entire run accepted.'
    definitions.append({'name': 'finish', 'description': description, 'parameters': finish_schema(kind)})
    return definitions

async def command(run_id: str, task_key: str, operation: str, text: str, runtime: dict, image: str) -> dict:
    from .db import session, Run
    with session() as s:
        run = s.get(Run, run_id)
        mode = run.snapshot['project'].get('execution_mode', 'container') if run else 'container'
    if mode == 'local':
        from .local_executor import execute
        return await execute(run_id, task_key, operation, text, runtime)
    if mode != 'container': raise ValueError('未知冻结执行模式')
    payload = {'run_id': run_id, 'task_key': task_key, 'operation_id': hashlib.sha256(operation.encode()).hexdigest(),
               'image': image, 'command': text, 'timeout_s': runtime['command_timeout_s'], 'cpu': runtime['cpu'], 'memory_mb': runtime['memory_mb']}
    try:
        async with httpx.AsyncClient(timeout=runtime['command_timeout_s'] + 120, trust_env=False) as client:
            r = await client.post(settings.sandbox_url + '/execute', headers={'x-sandbox-token': settings.sandbox_token}, json=payload)
            if r.status_code >= 300:
                raise RuntimeError(f'沙箱拒绝执行（HTTP {r.status_code}）；检查镜像、runsc及工作区，未降级到宿主机')
            return r.json()
    except httpx.HTTPError as exc:
        raise RuntimeError('沙箱通信失败；命令结果不确定，不自动重放') from exc

async def invoke(name: str, arguments: dict, allowed: list[str], root: Path, run_id: str, key: str, operation: str, runtime: dict, image: str) -> dict:
    if name not in allowed or name == 'finish': raise ValueError('工具不在该角色授权范围内')
    if name in ('inspect_team', 'assign_work', 'start_discussion', 'end_discussion'):
        from . import autonomy
        if name == 'inspect_team': return autonomy.inspect_team(run_id, key, arguments)
        return getattr(autonomy, name)(run_id, key, operation, arguments)
    if name == 'inspect_environment':
        from .execution_tools import inspect_environment
        return await inspect_environment(run_id,key,operation,arguments,runtime,image)
    if name=='request_decision':
        from .engagement import request_decision
        return request_decision(run_id,key,operation,arguments)
    if name in ('request_tool_installation', 'install_approved_tool', 'run_installed_tool'):
        from .tool_installations import invoke as installation_invoke
        return await installation_invoke(name, arguments, run_id, key, operation, runtime, image)
    if name in ('request_execution_permission', 'run_approved_command'):
        from .execution_permissions import invoke as permission_invoke
        return await permission_invoke(name, arguments, run_id, key, operation, runtime)
    if name in ('activate_skill','read_skill_resource','run_skill_script'):
        from .skills import invoke as skill_invoke
        return await skill_invoke(name,arguments,run_id,key,operation,root,runtime,image)
    if name == 'send_message':
        from .collaboration import send_message
        if not {'recipient_id', 'body'} <= set(arguments) or set(arguments) - {'recipient_id', 'body', 'request_reply'}:
            raise ValueError('消息参数不匹配')
        if not isinstance(arguments['recipient_id'], str) or not isinstance(arguments['body'], str) or ('request_reply' in arguments and type(arguments['request_reply']) is not bool):
            raise ValueError('消息参数类型不匹配')
        return send_message(run_id, key, operation, arguments['recipient_id'], arguments['body'], arguments.get('request_reply'))
    expected = {'list_files': set(), 'read_file': {'path'}, 'write_file': {'path', 'content'}, 'delete_file': {'path'}, 'run_command': {'command'}}[name]
    if set(arguments) != expected or any(not isinstance(v, str) for v in arguments.values()): raise ValueError('工具参数不匹配')
    if name == 'list_files': return {'files': list_files(root), 'limit': 1500}
    if name == 'run_command':
        return await command(run_id, key, operation, arguments['command'], runtime, image)
    if name == 'read_file':
        raw = read_workspace_file(root, arguments['path'], runtime['max_file_bytes'])
        return {'path': arguments['path'], 'content': raw.decode('utf-8'), 'sha256': hashlib.sha256(raw).hexdigest()}
    if name == 'delete_file':
        if not delete_workspace_file(root, arguments['path']): raise ValueError('文件不存在')
        return {'deleted': arguments['path']}
    content = arguments['content'].encode('utf-8')
    write_workspace_file(root, arguments['path'], content, runtime['max_file_bytes'])
    return {'written': arguments['path'], 'bytes': len(content)}
