import {Json,Run} from '../types';

const stages:Record<string,string>={intake:'接收需求',plan:'制定计划',action_plan:'整理行动计划',discussion:'团队讨论',develop:'执行任务',verify:'项目验证',review:'独立审查'};
export function pendingIntegrationSubmissions(run:Run){
 return (run.tasks||[]).filter(task=>task.status==='done'&&Array.isArray(task.result?.deliverables)&&task.result.deliverables.length).map(task=>({key:task.key,member:run.snapshot?.agents?.[task.agent_id]?.name||task.agent_id,files:task.result.deliverables.filter((path:unknown)=>typeof path==='string')}));
}
export function summarizeRunFailure(run:Run){
 const error=run.error||'',task=run.tasks?.find(t=>['needs_attention','failed'].includes(t.status));
 const base={stage:stages[task?.kind||'']||'运行处理',member:task?.agent_id?run.snapshot?.agents?.[task.agent_id]?.name||task.agent_id:'',title:'任务已停止，需要检查后重新发起',reason:'执行未能完成。已有记录保留，当前任务不会自行继续。',next:'先查看阶段记录、工具结果和已有产物，再调整需求或配置，重新确认后发起新运行。'};
 if(/并行变更.*冲突|merge conflict|patch.*(?:does not apply|already exists)/i.test(error))return {...base,member:'',stage:'成果合并',title:'成果合并冲突，任务已停止',reason:'成员提交的文件与当前集成版本发生冲突。系统保留了补丁，没有覆盖已有文件；这不是模型连接错误，也不是等待审批。',next:'先查看成员提交与冲突文件，核对所选项目的约束是否符合本次目标。不要反复修改模型或直接重复整次任务；确认已有成果和剩余工作后再发起。'};
 if(/validation errors? for Plan\b/.test(error))return {...base,stage:'制定计划',title:'计划格式不符合要求，任务已停止',reason:'模型返回的计划缺少必要字段或使用了错误字段名，系统没有接受这份计划。这不是等待你批准，也不需要你手工修改那段报错。',next:'当前版本会把格式问题反馈给模型，在原有步数内要求修正。此历史运行不会自动恢复；请先检查已有记录，再使用最新配置重新发起。'};
 if(/模型调用超时|模型响应超时/i.test(error))return {...base,title:'模型响应超时，结果未确认',reason:'已等待到模型调用时限，但没有收到完整响应。模型请求可能已经被供应商接收，因此系统没有自动重放。',next:'先查看最后一条模型与工具记录；如果没有新的工具执行，再用原需求重试确认。若已有工具结果，负责人应先核对文件和成果，再继续剩余工作。'};
 if(/上下文.*上限|上下文.*超过/.test(error))return {...base,title:'成员上下文空间不足',reason:'任务记录仍然完整，但本轮模型请求超过了该成员的上下文配置上限，系统没有静默截断历史。',next:'先保留现有成果，缩小当前任务范围或提高该成员的上下文上限后，再从未完成工作继续。'};
 if(/Agent任务超过自动执行时限|超过本次运行的自动执行时限/.test(error))return {...base,title:'执行时限已到，任务尚未完成',reason:'成员或本次运行达到明确的时间上限；已完成的步骤和成果保留，未完成步骤没有被伪装成成功。',next:'查看最后一个成员的工具结果和文件状态，由负责人判断是否能在现有成果上继续；否则调整时限后重新发起剩余工作。'};
 if(/未确认完成|不自动重放|uncertain/i.test(error))return {...base,title:'上一步执行结果不确定，已停止保护现场',reason:'系统无法确认某个步骤是否已完成，自动重试可能重复修改文件或执行命令。',next:'先核对执行记录、文件与外部系统结果，确认哪些工作已完成；在新需求中明确剩余工作后再重新发起。'};
 if(/最大执行步数|最大模型调用|调用.*上限|返工上限|预算/.test(error))return {...base,title:'已达到执行限制，任务尚未完成',reason:'本次运行已用完步数、调用或返工额度，不能继续自动执行。',next:'检查已完成的工作和未解决的问题，缩小下一次任务范围；确需调整额度时，先编辑运行配置，再确认发起新运行。'};
 if(/未通过人工验收|人工验收未通过/.test(error))return {...base,stage:'人工验收',title:'本次成果未被接受',reason:'本次运行已按验收结果结束，已有成果与反馈仍然保留。',next:'核对验收意见和已有成果，把需要补充或修正的内容写入新需求后重新发起。'};
 if(/401|403|authentication|invalid.?api.?key|鉴权|密钥无效/i.test(error))return {...base,title:'模型连接或访问授权失败',reason:'供应商拒绝了本次请求，可能涉及凭据、账号权限或模型访问范围。',next:'在供应商与模型页面检查对应连接和可用型号，再检查成员绑定。不要把 API Key 填进任务描述。'};
 if(/参数|协议|工具调用|未绑定|模型.*停用|供应商.*停用/.test(error))return {...base,title:'模型或成员配置不满足执行要求',reason:'模型启用不代表可执行任务；主模型、备用模型、参数协议和工具能力都需要通过检查。',next:'打开成员与模型配置，按创建前检查列出的具体成员、模型和字段处理，再重新发起。'};
 if(/timeout|timed out|超时|连接失败|connecterror|429|502|503|504/i.test(error))return {...base,title:'模型或执行服务暂时无法完成请求',reason:'请求遇到超时、限流或连接故障，本次运行已停止。',next:'先核对最后一步和工具结果，确认连接恢复后再重新发起，避免重复执行已完成工作。'};
 if(/权限|许可|拒绝|denied|not permitted/i.test(error))return {...base,title:'操作被权限规则阻止',reason:'本次操作不在已确认的权限范围内。系统没有自动扩大权限。',next:'先检查被拒绝的操作是否确实必要；调整任务范围或在相应审批入口确认授权后，再发起新运行。'};
 return base;
}

// Reuse intent and explicit scope, never copy frozen credentials, model
// bindings, approval decisions, commands, or execution results into a new run.
export function runRecoverySeed(run:Run):Json{
 const snapshot=run.snapshot||{},design=snapshot.design||{};
 const seed:Json={execution_strategy:'autonomous',run_project_checks:false,project_id:run.project_id,title:run.title.slice(0,172)+' · 重新发起',request:run.request,agent_ids:Object.keys(snapshot.agents||{}),parent_run_id:run.id,collaboration_flow:false,entry_agent_id:snapshot.entry_agent_id||null};
 if(design.organization_team){const ref=design.organization_team.layout_ref||{};seed.office_team_id=design.organization_team.id;seed.office_layout_ref={id:ref.id,version:ref.version,digest:ref.digest};}
 return seed;
}

export default function RunRecovery({run,writer,onNewTask,onResume,onInspect,onConfigure}:{run:Run;writer:boolean;onNewTask:(seed:Json)=>void;onResume?:()=>void;onInspect?:()=>void;onConfigure?:()=>void}){
 if(run.status!=='needs_attention')return null;
 const info=summarizeRunFailure(run);
 const sameRunResume=run.status==='needs_attention' && (run.mode==='autonomous'||run.execution_strategy==='autonomous'||run.snapshot?.execution_strategy==='autonomous');
 const pending=info.stage==='成果合并'?pendingIntegrationSubmissions(run):[];
 return <section className="run-recovery" role="status" aria-label="任务停止原因与处理"><header><span>任务已停止 · 不是待审批</span><h3>{info.title}</h3></header><p className="run-recovery-stage">停在：{info.stage}{info.member?' · '+info.member:''}</p><p>{info.reason}</p>{!!pending.length&&<details open className="run-recovery-submissions"><summary>尚未集成的成员提交</summary><p className="microcopy">以下是待合并提交中的文件，不代表每个文件都存在冲突。</p>{pending.map(item=><article key={item.key}><b>{item.member}</b><ul>{item.files.map((file:string)=><li key={file}><code>{file}</code></li>)}</ul></article>)}</details>}<h4>接下来怎么做</h4><p>{sameRunResume?'负责人会先读取失败记录，在原任务中重试或改派；不会创建新的任务。':info.next}</p><div className="inline-actions">{writer&&sameRunResume&&onResume&&<button className="button primary" onClick={onResume}>继续此任务（负责人处理）</button>}{writer&&<button className="button secondary" onClick={()=>onNewTask(runRecoverySeed(run))}>使用最新配置重新发起</button>}{onInspect&&<button className="button secondary" onClick={onInspect}>查看执行记录与已有产物</button>}{onConfigure&&info.stage!=='成果合并'&&<button className="button secondary" onClick={onConfigure}>检查成员与模型配置</button>}</div><p className="microcopy">继续会复用本次运行的团队、工作区和权限快照；只有负责人协调产生新的成员激活，已完成步骤不会重放。需要改变目标或权限时，再新建任务。</p>{run.error&&<details><summary>技术详情（排查时使用）</summary><pre>{run.error}</pre></details>}</section>;
}
