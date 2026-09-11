from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')

class Runtime(Strict):
    max_steps: int = Field(24, ge=1, le=500)
    max_agent_messages: int = Field(200, ge=0, le=5000)
    max_model_calls: int = Field(100, ge=1, le=10000)
    task_timeout_s: int = Field(3600, ge=30, le=86400)
    call_timeout_s: int = Field(120, ge=5, le=1800)
    command_timeout_s: int = Field(120, ge=1, le=3600)
    max_parallel: int = Field(3, ge=1, le=32)
    max_tasks: int = Field(20, ge=1, le=100)
    max_rework_rounds: int = Field(2, ge=0, le=10)
    cpu: float = Field(1, ge=.1, le=32)
    memory_mb: int = Field(1024, ge=128, le=65536)
    max_file_bytes: int = Field(200000, ge=1024, le=5000000)
    max_context_chars: int = Field(120000, ge=4096, le=1000000)

DEFAULT_RUNTIME = Runtime().model_dump()
ROLES = {'planner': {'max_steps': 8}, 'developer': {}, 'tester': {}, 'reviewer': {'max_steps': 12}}
TOOLS_BY_ROLE = {
    'planner': ['list_files', 'read_file', 'send_message', 'finish'],
    'developer': ['list_files', 'read_file', 'write_file', 'delete_file', 'run_command', 'send_message', 'finish'],
    'tester': ['list_files', 'read_file', 'write_file', 'delete_file', 'run_command', 'send_message', 'finish'],
    'reviewer': ['list_files', 'read_file', 'send_message', 'finish'],
}

def validate_runtime_patch(patch: dict[str, Any]) -> dict[str, Any]:
    Runtime.model_validate(DEFAULT_RUNTIME | patch)
    if any(v is None for v in patch.values()):
        raise ValueError('运行参数不允许 null；清除覆盖请删除该字段')
    return patch

def resolve_runtime(layers: list[tuple[str, dict]]) -> tuple[dict, dict]:
    result, sources = DEFAULT_RUNTIME.copy(), {k: 'platform' for k in DEFAULT_RUNTIME}
    for name, values in layers:
        validate_runtime_patch(values)
        for key, value in values.items():
            result[key], sources[key] = value, name
    return Runtime.model_validate(result).model_dump(), sources

AGENT_RUNTIME_KEYS = {'max_steps','task_timeout_s','call_timeout_s','command_timeout_s','cpu','memory_mb','max_file_bytes','max_context_chars'}

def validate_agent_runtime_patch(patch: dict) -> dict:
    validate_runtime_patch(patch)
    if set(patch) - AGENT_RUNTIME_KEYS:
        raise ValueError('这些字段属于整次运行，不能在Agent或角色层覆盖: ' + ', '.join(sorted(set(patch)-AGENT_RUNTIME_KEYS)))
    return patch

class Login(Strict):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=512)

class UserCreate(Strict):
    username: str = Field(min_length=2, max_length=80, pattern=r'^[a-zA-Z0-9_.-]+$')
    password: str = Field(min_length=12, max_length=512)
    role: Literal['admin', 'developer', 'viewer'] = 'developer'

class ProviderInput(Strict):
    name: str = Field(min_length=1, max_length=100)
    protocol: Literal['openai_chat', 'anthropic_messages', 'openai_responses', 'google_gemini', 'custom_http']
    protocol_config: dict[str, Any] = Field(default_factory=dict)
    base_url: str = Field(min_length=1, max_length=1500)
    api_key: str | None = Field(None, max_length=4096)
    external: bool = True
    enabled: bool = True

class ModelInput(Strict):
    enabled: bool = True
    name: str = Field(min_length=1, max_length=120)
    provider_id: str
    model_id: str = Field(min_length=1, max_length=200)
    allowed_parameters: list[str] = Field(default_factory=lambda: ['temperature', 'top_p', 'max_tokens'])
    parameters: dict[str, Any] = Field(default_factory=lambda: {'max_tokens': 4096})
    supports_tools: bool = True
    input_cost_per_million: float | None = Field(None, ge=0, allow_inf_nan=False)
    output_cost_per_million: float | None = Field(None, ge=0, allow_inf_nan=False)
    context_window: int | None = Field(None, ge=1, le=1_000_000_000)

class SkillBinding(Strict):
    skill_id: str = Field(min_length=1, max_length=32)
    version: int = Field(ge=1)
    mode: Literal['on_demand', 'always'] = 'on_demand'
    enabled: bool = True
    allow_scripts: bool = False
    parameters: dict[str, Any] = Field(default_factory=dict)
    @model_validator(mode='after')
    def bounded(self):
        import json
        if len(json.dumps(self.parameters, ensure_ascii=False)) > 4000:
            raise ValueError('Skill参数超过4000字符')
        return self

class CommunicationPolicy(Strict):
    scope: Literal['run', 'department', 'reporting', 'custom'] = 'run'
    recipients: list[str] = Field(default_factory=list, max_length=100)

class OfficePosition(Strict):
    x: float = Field(0, ge=-12, le=12, allow_inf_nan=False)
    z: float = Field(0, ge=-8, le=8, allow_inf_nan=False)
    floor: int = Field(1, ge=1, le=10)
    zone: str = Field('', max_length=80)

class RoleTemplateReference(Strict):
    item_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    revision_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')


class AgentInput(Strict):
    job_title: str = Field('', max_length=120)
    office_position: OfficePosition | None = None
    tool_permissions: list[str] | None = None
    design_ref: dict | None = None
    role_template_ref: RoleTemplateReference | None = None
    department: str = Field("", max_length=80)
    communication: CommunicationPolicy = Field(default_factory=CommunicationPolicy)
    skills: list[SkillBinding] = Field(default_factory=list, max_length=12)
    enabled: bool = True
    reports_to: str | None = None
    peer_reception: Literal['sit', 'stand'] = 'sit'
    name: str = Field(min_length=1, max_length=80)
    role: Literal['planner', 'developer', 'tester', 'reviewer']
    model_profile_id: str | None = None
    fallback_model_ids: list[str] = Field(default_factory=list, max_length=5)
    instructions: str = Field('', max_length=20000)
    runtime: dict[str, Any] = Field(default_factory=dict)
    model_parameters: dict[str, Any] = Field(default_factory=dict)
    @model_validator(mode='after')
    def check(self):
        validate_agent_runtime_patch(self.runtime)
        if self.tool_permissions is not None:
            if set(self.tool_permissions) - set(TOOLS_BY_ROLE[self.role] + ['request_decision']):
                raise ValueError('成员权限不能超过该角色允许的工具范围')
            if 'finish' not in self.tool_permissions:
                raise ValueError('成员权限必须包含提交结果工具finish')
        if len({x.skill_id for x in self.skills}) != len(self.skills):
            raise ValueError("同一Agent不能重复绑定Skill")
        return self

class ProjectInput(Strict):
    execution_mode: Literal['local', 'container'] = 'local'
    permission_profile: Literal['ask', 'auto', 'full'] = 'ask'
    full_access_confirmed: bool = Field(False, exclude=True)
    archived: bool = False
    name: str = Field(min_length=1, max_length=120)
    import_path: str = Field('blank', min_length=1, max_length=1000)
    blank: bool = False
    desktop_directory_token: str | None = Field(None, min_length=20, max_length=200, exclude=True)
    instructions: str = Field('', max_length=20000)
    sandbox_image: str = Field('agent-forge/sandbox:local', min_length=1, max_length=250, pattern=r'^[\w][\w./:@-]*$')
    check_commands: list[str] = Field(default_factory=list, max_length=20)
    # New projects opt in to external model providers unless explicitly disabled.
    # Existing projects are handled by the update endpoint, which preserves this
    # setting when the field is omitted (including legacy configs where it is absent).
    allow_external_models: bool = True
    runtime: dict[str, Any] = Field(default_factory=dict)
    members: list[str] = Field(default_factory=list)
    @model_validator(mode='after')
    def check(self):
        validate_runtime_patch(self.runtime)
        if self.permission_profile == 'full' and (self.execution_mode != 'local' or not self.full_access_confirmed):
            raise ValueError('完全访问仅用于本机执行，必须明确确认可访问当前账号所有文件和网络的风险')
        if any(not c.strip() or len(c) > 4000 for c in self.check_commands):
            raise ValueError('验收命令不能为空或超过4000字符')
        return self

class GoalCriterion(Strict):
    id: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,47}$')
    description: str = Field(min_length=3, max_length=2000)
    evidence_type: Literal['decision', 'code_reference'] = 'decision'

class GoalEvidence(Strict):
    task_key: str = Field(min_length=1, max_length=100)
    explanation: str = Field(min_length=3, max_length=2000)
    path: str = Field('', max_length=500)
    sha256: str = Field('', max_length=64)

class GoalAssessment(Strict):
    criterion_id: str = Field(min_length=1, max_length=48)
    status: Literal['met', 'unmet', 'blocked']
    rationale: str = Field(min_length=3, max_length=3000)
    evidence: list[GoalEvidence] = Field(default_factory=list, max_length=30)

class GoalReview(Strict):
    transcript_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    summary: str = Field(min_length=3, max_length=8000)
    assessments: list[GoalAssessment] = Field(min_length=1, max_length=20)
    open_issues: list[str] = Field(default_factory=list, max_length=50)
    needs_human: bool = False

class ActionProposal(Strict):
    title: str = Field(min_length=1, max_length=180)
    description: str = Field(min_length=3, max_length=6000)
    owner_id: str = Field(min_length=1, max_length=32)
    acceptance: str = Field(min_length=3, max_length=3000)

class IntakeResult(Strict):
    summary: str = Field(min_length=3, max_length=4000)
    participants: list[str] = Field(min_length=2, max_length=30)
    agenda: str = Field(min_length=3, max_length=8000)

class DecisionQuestion(Strict):
    question: str = Field(min_length=3, max_length=4000)
    context: str = Field('', max_length=4000)
    options: list[dict[str, str]] = Field(default_factory=list, max_length=12)
    allow_text: bool = True
    @model_validator(mode='after')
    def choices(self):
        if not self.options and not self.allow_text: raise ValueError('至少提供选项或允许文字回答')
        ids=set()
        for x in self.options:
            if set(x) != {'id','label'} or not x['id'] or len(x['id'])>80 or not x['label'].strip() or len(x['label'])>1000: raise ValueError('选项必须包含有效的id和label')
            if x['id'] in ids: raise ValueError('选项ID重复')
            ids.add(x['id'])
        return self

class HumanAnswer(Strict):
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    choice_id: str = Field('', max_length=80)
    text: str = Field('', max_length=8000)
    reject: bool = False

class HumanMessage(Strict):
    recipients: list[str] = Field(min_length=1, max_length=30)
    body: str = Field(min_length=1, max_length=8000)

class SkillInput(Strict):
    files: dict[str, str]
    expected_version: int | None = Field(None, ge=1)

class DiscussionContribution(Strict):
    action_items: list[ActionProposal] = Field(default_factory=list, max_length=30)
    summary: str = Field(min_length=1, max_length=8000)
    notes: str = Field('', max_length=16000)
    open_issues: list[str] = Field(default_factory=list, max_length=50)
    # Accepted only for old protocol clients; never a stopping signal.
    ready: bool | None = None

class OrchestrationInput(Strict):
    mode: Literal['development', 'discussion'] = 'development'
    participants: list[str] = Field(default_factory=list, max_length=30)
    strategy: Literal['sequential', 'parallel'] = 'sequential'
    stop_when: Literal['goal_satisfied'] = 'goal_satisfied'
    prompt: str = Field('解决需求、形成有依据的方案，明确风险及可验收的交付要求。', min_length=1, max_length=8000)
    criteria: list[GoalCriterion] = Field(default_factory=lambda:[GoalCriterion(id='solution',description='给出可执行的方案并解释关键选择'),GoalCriterion(id='risks',description='明确风险、依赖、验证方式及未解决事项')], min_length=1, max_length=20)
    assessor_id: str | None = None
    stagnation_checks: int = Field(3, ge=2, le=20)
    legacy_stop_migrated: bool = False
    @model_validator(mode='before')
    @classmethod
    def migrate_stop(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            if 'rounds' in data or 'min_rounds' in data or data.get('stop_when') in ('max_rounds','unanimous_ready'):
                old_max, old_min = data.get('rounds',3), data.get('min_rounds',1)
                if not isinstance(old_max,int) or not isinstance(old_min,int) or not 1<=old_min<=old_max<=12:
                    raise ValueError('旧版轮次配置无效；请改用目标验收条件')
                data.pop('rounds',None); data.pop('min_rounds',None)
                data['stop_when']='goal_satisfied';data['legacy_stop_migrated']=True
        return data
    @model_validator(mode='after')
    def valid(self):
        if len(set(self.participants)) != len(self.participants):
            raise ValueError('讨论参与者不能重复')
        if (self.mode == 'discussion' or self.participants) and len(self.participants) < 2:
            raise ValueError('讨论至少需要两名成员')
        if len({c.id for c in self.criteria}) != len(self.criteria):
            raise ValueError('目标验收项ID不能重复')
        if self.assessor_id and self.assessor_id not in self.participants:
            raise ValueError('目标复核成员必须属于参与团队')
        return self

class OrchestrationTemplateInput(Strict):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field('', max_length=2000)
    enabled: bool = True
    graph: OrchestrationInput
    expected_version: int | None = Field(None, ge=1)

class DesignReference(Strict):
    id: str = Field(min_length=1, max_length=32)
    version: int = Field(ge=1)

class OfficeLayoutReference(DesignReference):
    version: int = Field(ge=1, strict=True)
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')

class RunInput(Strict):
    execution_strategy: Literal['autonomous'] = 'autonomous'
    run_project_checks: bool = False
    routing_ref: DesignReference | None = None
    team_ref: DesignReference | None = None
    office_team_id: str | None = Field(None, pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,63}$')
    office_layout_ref: OfficeLayoutReference | None = None
    workflow_ref: DesignReference | None = None
    routing_overrides: dict[str, str] = Field(default_factory=dict)
    entry_agent_id: str | None = None
    collaboration_flow: bool = False
    orchestration: OrchestrationInput | None = None
    template_id: str | None = None
    template_version: int | None = Field(None, ge=1)
    project_id: str
    title: str = Field(min_length=1, max_length=180)
    request: str = Field(min_length=3, max_length=30000)
    agent_ids: list[str] = Field(min_length=1, max_length=30)
    runtime: dict[str, Any] = Field(default_factory=dict)
    parent_run_id: str | None = None
    @model_validator(mode='after')
    def check(self):
        validate_runtime_patch(self.runtime)
        if self.execution_strategy == 'autonomous' and (self.orchestration is not None or self.template_id or self.workflow_ref):
            raise ValueError('团队自治不使用固定编排；请清除旧编排配置，由负责人按任务组织协作')
        if (self.office_team_id is None) != (self.office_layout_ref is None):
            raise ValueError('选择办公团队时必须同时提交当前已应用的组织布局版本')
        if self.office_team_id and self.team_ref:
            raise ValueError('办公团队与已发布团队模板不能同时选择')
        if self.workflow_ref and (self.orchestration is not None or self.template_id):
            raise ValueError('已发布工作流与其他编排配置不能同时选择')
        if self.routing_overrides and not self.routing_ref:
            raise ValueError('任务级模型覆盖需要选择已发布的路由策略')
        if set(self.routing_overrides) - set(ROLES):
            raise ValueError('任务级模型覆盖包含未知角色')
        if self.orchestration is not None and self.template_id:
            raise ValueError('请选择模板或临时编排，不能同时指定')
        if self.template_id and not self.template_version:
            raise ValueError('选择模板时必须指定模板版本')
        if self.template_version and not self.template_id:
            raise ValueError('模板版本必须对应模板ID')
        if len(set(self.agent_ids)) != len(self.agent_ids):
            raise ValueError('Agent不能重复')
        return self

class AutonomousFinish(Strict):
    summary: str = Field(min_length=1, max_length=8000)
    notes: str = Field('', max_length=16000)
    outcome: Literal['done', 'waiting', 'ready', 'blocked']
    deliverables: list[str] = Field(default_factory=list, max_length=100)
    evidence_task_keys: list[str] = Field(default_factory=list, max_length=100)
    @model_validator(mode='after')
    def meaningful(self):
        if not self.summary.strip(): raise ValueError('必须说明实际成果或阻塞原因')
        if len(set(self.deliverables)) != len(self.deliverables): raise ValueError('成果路径不能重复')
        if any(not p.strip() or len(p)>1000 for p in self.deliverables): raise ValueError('成果路径不能为空或过长')
        return self

class TeamAssignment(Strict):
    recipient_id: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=180)
    instruction: str = Field(min_length=3, max_length=6000)
    acceptance: str = Field(min_length=3, max_length=3000)

class TeamDiscussion(Strict):
    participants: list[str] = Field(min_length=2, max_length=30)
    agenda: str = Field(min_length=3, max_length=6000)
    @model_validator(mode='after')
    def unique(self):
        if len(set(self.participants)) != len(self.participants): raise ValueError('讨论成员不能重复')
        return self

class PlanItem(Strict):
    key: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,47}$')
    title: str = Field(min_length=1, max_length=180)
    description: str = Field(min_length=1, max_length=6000)
    agent_id: str
    depends_on: list[str] = Field(default_factory=list)
    acceptance: str = Field(min_length=1, max_length=3000)

class Plan(Strict):
    summary: str = Field(min_length=1, max_length=4000)
    tasks: list[PlanItem] = Field(min_length=1, max_length=100)
    @model_validator(mode='after')
    def dag(self):
        keys = {t.key for t in self.tasks}
        if len(keys) != len(self.tasks):
            raise ValueError('子任务key重复')
        done: set[str] = set()
        while len(done) < len(keys):
            ready = [t for t in self.tasks if t.key not in done and set(t.depends_on) <= done]
            if not ready:
                raise ValueError('依赖包含未知节点或循环')
            done.update(t.key for t in ready)
        return self

class Decision(Strict):
    digest: str
    approved: bool
    note: str = Field('', max_length=4000)

class Control(Strict):
    action: Literal['pause', 'resume', 'cancel']
