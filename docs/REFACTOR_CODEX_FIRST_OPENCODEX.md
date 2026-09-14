# Agent Forge 重构方案：Codex-first Runtime + OpenCodex 多供应商网关

> **当前实施方向（2026-09-12）**：本文的 Codex-first、OpenCodex 网关和运行隔离边界继续有效；Forge 控制面语言已改为 Rust-first，具体迁移阶段、SQLite 事务内核和前端兼容要求以 [`PLAN_RUST_BACKEND.md`](./PLAN_RUST_BACKEND.md) 为准。Codex 源码保持不变。

> 2026-09-12 实施边界更新：按用户确认，OpenCodex、OMX 均可修改或直接复用源码，Forge 当前实现也可重构；Codex 源码保持不变。下文“优先适配”“不改上游”“仅维护小补丁”等表述是原方案的维护建议，不再限制 OpenCodex/OMX 的实现选择。仍保留独立运行目录、权限隔离、版本及许可证记录和真实验证要求。

> 状态：阶段 0 基础边界已完成，阶段 1 单任务 POC 已开始；按 OMX 对齐后的完整路线见 `docs/PLAN_REFACTOR_OMX_ALIGNMENT.md`；尚未切换现有任务执行路径  
> 日期：2026-09-12  
> 目标平台：macOS 本机应用优先，后续可扩展 Windows

> 重要边界：本方案不读写用户现有的 `~/.codex`，不复用其中的会话、插件、密钥或个人配置。Codex 与 OpenCodex 将以项目内固定版本构建，并使用 Agent Forge 自己的数据目录作为运行时配置根目录。

## 1. 结论先行

Agent Forge 不再维护第二套“模型循环、工具执行、审批、流式事件和重试”实现，而是把职责拆成三个边界：

```text
Agent Forge 控制平面
  团队、成员、任务、项目、权限策略、审批视图、3D办公室
              |
              v
运行时桥接层（Runtime Bridge）
  任务/成员映射、团队工具、事件投影、工作副本租约
              |
              v
Codex App Server
  Thread / Turn / Item、模型回合、工具、沙箱、审批、流式事件、恢复
              |
              v
OpenCodex 本地多供应商网关
  Provider、协议适配、模型目录、路由、健康检查、故障转移
              |
              v
OpenAI / Anthropic / Gemini / DeepSeek / 智蔻 / Ollama / 其他兼容供应商
```

这不是把 Agent Forge 变成 Codex 的皮肤。Codex 负责“怎样真实执行一次工作”，OpenCodex 负责“请求发送给哪个供应商”，Agent Forge 负责“谁可以做什么、团队如何协作、用户看到什么并最终治理”。

## 2. 为什么必须重构

当前实现把固定流程、模型调用、本地工具、权限判断、活动日志和前端状态分散在多处。结果是：

- 任务失败后容易被当成整次任务终止，负责人无法在原任务中继续、重试或转派。
- 页面看到的是活动摘要，不是有稳定顺序的模型增量、工具调用、审批和完成事件，因此实时输出与最终状态可能不一致。
- `workflow_core` 的固定阶段与“负责人自主决策”冲突；它会把业务流程误当成执行器职责。
- `engine` 和 `local_runtime` 各自掌握一部分执行语义，异常恢复时无法判断副作用是否已经发生。
- 供应商、模型能力评分和真实协议能力混在一起，导致“模型已启用但工具不可用”时提示不准确。
- 并行任务没有统一的工作副本租约与会话边界，容易出现上下文串线或文件覆盖。

目标不是继续增加配置项，而是减少重复配置：把复杂执行能力交给成熟运行时，把业务控制收敛到少数明确的领域对象。

## 3. 目标边界与不做事项

### 3.1 本次重构要达到

1. 真实调用模型、工具和本机项目目录，不使用模拟“已完成”。
2. 负责人在同一任务内决定分工、协作、重试、转派、审批协助和最终提交。
3. 不同任务完全隔离 Thread、记忆、事件和工作副本；同一成员可以并行参与多个任务。
4. 运行过程中可实时看到有序的模型输出、工具调用、审批和状态变化。
5. 保留三种用户权限语义：请求审批、帮我批准、完全访问；负责人只能在用户授予的范围内行动。
6. 所有供应商通过统一的本地网关接入，并能按供应商同步可用模型、协议和健康状态。
7. 3D办公室展示真实运行投影；是否走到会议室属于交互表现，不成为执行正确性的前置条件。

### 3.2 明确不做

- 不再重写一套与 Codex 平行的 agent loop、工具协议、审批协议或流式协议。
- 不把 3D 场景当作调度器、权限系统或事实数据库。
- 不让模型目录发现自动改变 Agent 绑定或正在运行的任务。
- 不在主分支自动升级 Codex/OpenCodex，也不替换正在运行的进程。
- 不承诺每个上游供应商都支持相同的工具、推理、图像和流式能力；能力必须通过真实探测确认。
- 不以“完全访问”声称实现操作系统级隔离；它只是用户确认的权限策略。

## 4. 领域模型与唯一事实来源

```text
Task（业务任务）
 ├─ TeamSnapshot（创建时冻结的团队范围与成员配置）
 ├─ RuntimeSession（task × member，一个独立 Codex Thread）
 │   └─ Turn（一次工作回合）
 │       └─ Item（模型消息 / 工具调用 / 审批 / 文件变化 / 结果）
 ├─ Attempt（围绕一个回合的业务重试记录）
 ├─ WorkspaceLease（工作副本租约）
 ├─ MemoryContext（任务记忆与可授权团队记忆）
 └─ RuntimeEvent（追加式事件流）
```

对象职责：

| 对象 | 所有权与语义 |
| --- | --- |
| Task | 用户看到的任务边界。恢复、重试、转派仍沿用同一个 task_id，不偷偷新建任务。 |
| TeamSnapshot | 任务启动时确认的负责人、成员、模型路由、Skill 和权限快照。设计期修改不追溯改写它。 |
| RuntimeSession | 一个任务中一个成员的独立执行上下文；不能跨任务复用记忆。 |
| Turn | Codex 的一次请求到完成/中断/失败的回合，可包含多次工具调用。 |
| Attempt | Agent Forge 的业务重试层；只记录为什么再次尝试，不盲目重放未知副作用。 |
| ToolInvocation | 模型意图、本地执行、结果和证据的组合；模型能力与本地权限分开。 |
| ApprovalRequest | 精确到操作、路径、网络目标或安装包范围的审批申请。 |
| RuntimeEvent | 带 task/thread/turn/item/sequence 的追加事实，用于所有 UI 投影。 |
| WorkspaceLease | 任务成员工作副本的占用与冲突控制。 |
| MemoryContext | 任务隔离、团队授权共享、可压缩但不丢目标/决定/证据的上下文。 |
| RuntimeVersion | Codex、OpenCodex、协议 schema 与关键配置的可追溯版本。 |

### 4.1 状态机

```text
queued → running → waiting_approval → running
running → interrupted → recoverable
running → failed → leader_recovering → running
running → awaiting_acceptance → completed
running → cancelled
```

- `waiting_approval` 是等待用户，不是失败。
- `interrupted` 是可恢复的中断；用户停止后应能继续原任务。
- `failed` 先进入 `leader_recovering`，负责人分析原因后选择重试、转派、补充信息或请求人工处理。
- `needs_attention` 只表示当前有一个明确、可定位的未决事项，不应掩盖整个任务仍可恢复。
- `completed` 和 `cancelled` 是不可变终态；后续修改通过新的 Attempt 或新的任务完成。
- 如果上一次副作用未知，必须先检查执行收据、文件变化或命令结果，再决定是否重试。

## 5. 三层架构职责

### 5.1 Agent Forge 控制平面

负责：

- 部门、楼层、团队、成员、角色模板和 Skill 绑定。
- 任务创建、团队快照、项目工作区和工作副本租约。
- 三种权限策略、审批策略、预算和企业能力画像。
- 负责人身份与团队范围校验。
- 运行事件投影到任务看板、成员侧栏、审批面板、活动记录和 3D 状态。
- 任务级恢复策略、审计、版本冻结和兼容性提示。

不负责：模型 token 循环、工具协议解析、命令执行细节、流式 delta 拼接。

### 5.2 Codex App Server 执行运行时

以 Thread/Turn/Item 为核心提供真实执行能力：

- 启动、恢复、引导和中断线程。
- 模型回合、命令执行、文件变更、MCP 调用、审批和流式事件。
- 本地项目目录内执行，按运行时权限处理越界、联网和安装操作。
- 压缩、历史与恢复所需的执行上下文。
- 向 Runtime Bridge 发送结构化通知；Forge 负责接收顺序、持久化和 UI 游标回放，不假定上游会重放所有流式增量。
- 负责人需要团队协作时，可在冻结的运行配置中启用 Forge 自带的 `forge_team` MCP stdio 服务；该服务只暴露当前 `run_id × task_key` 的能力目录、分工、消息、恢复、会议和用户决策工具，不授予新的文件、网络或系统权限。

生产接入优先使用本机 stdio 或 Unix socket；WebSocket 仅在经过版本与安全验证后启用。App Server 的实验性接口必须被版本锁定并写入兼容性矩阵。

Codex 源码以项目内 `third_party/codex` 的固定 commit 作为上游镜像（git submodule），构建产物进入 Agent Forge 的应用资源目录。启动时显式设置独立 `CODEX_HOME`，并移除继承的 `CODEX_HOME`、个人配置路径和插件路径，保证不会影响用户的 Codex 客户端。

### 5.3 OpenCodex 本地多供应商网关

负责供应商差异，不负责团队协作：

- Provider 连接、认证模式、上游 base URL 和协议适配。
- 按供应商同步模型目录，不把目录可见误认为已启用或可调用。
- Responses、Anthropic、Gemini、DeepSeek、OpenAI-compatible 等协议转换。
- 模型路由、组合与请求级故障转移。
- 供应商健康检查、就绪检查、请求日志和敏感信息脱敏。

Agent Forge 只保存稳定引用（provider_id、route_id、model_id）和企业能力画像；具体协议细节由 OpenCodex 适配器负责。OpenCodex 的管理 API 路径要以实际版本契约为准，不在 Forge 中猜测或复制其内部实现。

OpenCodex 源码以项目内 `third_party/opencodex` 的固定 commit 管理，按其许可证和上游说明构建独立网关。网关的配置、账号池和请求日志只写入 Agent Forge 的应用数据目录；默认不连接或修改用户已有的 OpenCodex/Codex 配置。

## 6. 负责人自治与团队协作

负责人不是固定 DAG 的 planner，而是一个拥有团队工具的 Codex Thread。它可读取团队能力目录，并在任务范围内调用：

- `inspect_team`：查看成员职责、Skill、模型路由、权限、当前工作和可用性。
- `assign_work`：把工作交给团队内成员，创建该成员在本任务的 RuntimeSession。
- `send_team_message`：发送需要回应的团队消息。
- `read_work_result`：读取成员结果、证据、失败原因和未决事项。
- `retry_activation`：在检查副作用后重试原分配。
- `reassign_activation`：把未完成工作转给团队内其他合适成员。
- `request_approval`：向用户提交精确的审批申请。
- `resolve_issue`：标记已解决、需要补充信息或必须人工处理。

这些工具只表达业务意图；控制平面必须再次校验成员仍属于冻结团队、模型路由仍可用、权限和工作副本租约仍满足。负责人不能创建团队外成员、扩大权限、跨任务读取记忆或绕过用户审批。

不保留固定的“需求→规划→开发→测试”业务流水线。仍然需要一个很薄的运行时调度器，用于并发上限、租约、取消、事件顺序和进程健康；它不决定专业分工，不替负责人发言。

## 7. 供应商与模型资产

模型关系保持：

```text
ProviderConnection → UpstreamCatalog → ModelConfig → ModelRoute → AgentBinding
```

- 每个 ProviderConnection 独立拉取上游模型目录；全量替换目录前保留同步批次与差异审计。
- ModelConfig 记录真实探测到的流式、工具、推理、图像和上下文能力；管理员的人工修正是企业能力画像，不覆盖原始探测结果。
- ModelRoute 记录协议、认证、主备和故障转移；路由成功不代表模型具备某个 Skill 的业务能力。
- 任务启动时冻结 provider、route、model、运行版本；后续目录更新只影响新任务。
- 若模型不支持工具、协议探测失败或供应商未就绪，创建/恢复时指出具体的供应商、路由、模型、阶段和上游响应，不显示泛化的“需人工处理”。

建议的真实检查顺序：

1. 连接可达与认证检查。
2. `/models` 或供应商等价目录接口拉取并校验响应。
3. 最小文本请求。
4. 最小流式请求。
5. 最小工具调用请求（只调用无副作用探针）。
6. 记录能力与错误证据，只有通过的能力才进入“可执行”。

## 8. 实时事件与前端展示

前端 reducer 的主键必须是 `(task_id, thread_id, turn_id, item_id, sequence)`，而不是成员名或一条可覆盖的活动文本。至少支持：

- `turn.started / turn.completed / turn.interrupted / turn.failed`
- `agent_message.delta / agent_message.completed`
- `command.started / command.output.delta / command.completed`
- `file_change.started / file_change.completed`
- `approval.requested / approval.resolved`
- `team_message.sent / team_message.received`
- `leader.recovery.started / leader.recovery.action / leader.recovery.completed`

展示规则：

- 实时输出区域按事件顺序增量渲染，最终结果以 `completed` 事件为权威。
- 每个错误显示任务、成员、供应商、路由、模型、Thread/Turn、阶段、是否有副作用和建议动作。
- 任务页显示“正在执行 / 等待审批 / 负责人处理中 / 可继续 / 已完成 / 已取消”，不把所有非完成状态压成“需人工处理”。
- 活动记录保留完整事件；3D 只订阅投影，允许弱网或窗口关闭后重新从事件游标恢复。

## 9. 记忆、并行与工作区

- 每个 `Task × Member` 使用独立 Thread 和任务记忆；默认不跨任务读取。
- 负责人可读取团队能力目录和本任务成员交付，但必须通过控制平面授权，不是共享全部聊天记录。
- 长任务触发压缩时保留目标、验收标准、关键决定、未完成事项、文件变化和工具收据。
- 同一成员并行任务使用不同工作副本租约；同一副本冲突时先暂停相关 Attempt，由负责人选择串行化、合并或转派。
- 运行重启时恢复同一个 Thread 和事件游标；未知执行状态不自动重放。

## 10. 迁移阶段

### 阶段 0：契约冻结与盘点

- 为 Task、RuntimeSession、Turn、Item、Attempt、ApprovalRequest、WorkspaceLease 和 RuntimeEvent 定义版本化 schema。
- 盘点 `workflow_core`、`engine`、`local_runtime` 中重复的执行职责，建立只读兼容适配器。
- 将 Codex 与 OpenCodex 固定到项目内的上游 commit，记录源码目录、许可证、构建命令和校验摘要；禁止把用户 `~/.codex` 当作依赖。
- 记录当前数据迁移/删除策略：旧运行可归档或删除，配置资产不删除。

### 阶段 1：单任务真实 POC

- 启动一个本机 Codex App Server 和一个 OpenCodex 网关。
- 仅迁移一个成员、一个项目、一个真实模型调用、一个无副作用工具和一条流式输出。
- 验证启动、实时 delta、完成、失败、停止、恢复和错误定位。

当前实现：`forge.codex_task.CodexSingleTask` 为显式 POC 入口，按任务生成独立
Codex 配置并收集有序事件。已真实请求智蔻模型并正确识别上游 403 失败；待供应商恢复后
继续验收模型文本、工具调用和流式增量。该入口尚未被旧任务调度器默认调用。

已增加默认关闭的 `POST /api/runtime/poc-task` 原型入口：仅引用已保存的供应商与模型，
服务端读取加密密钥并以 NDJSON 转发有序事件；它不创建旧版 Run，不接收前端原始密钥，
也不改变现有任务默认路径。开启前仍需完成权限、取消/恢复和事件持久化契约。

### 阶段 2：任务×成员会话与负责人工具

- 一个负责人 Thread + 多个成员 Thread。
- 通过 MCP/稳定团队工具实现查看能力、分工、消息、结果、重试和转派。
- 保证失败后仍在原 Task 中恢复，不创建替代任务。

### 阶段 3：权限、工具和工作区

- 将本地工具执行、审批、越界检查和工作副本租约接到 Codex 运行时边界。
- 实现三种权限的一致语义，以及联网/安装的精确审批。
- 删除或封存 Forge 内第二套命令/模型执行循环。

### 阶段 4：全量供应商与目录同步

- 以 OpenCodex 实际版本契约接入官方供应商、智蔻及其他兼容服务。
- 加入目录全量同步、协议探测、模型批量启停、路由主备和健康状态。
- 建立 provider/model compatibility matrix。

### 阶段 5：并行、记忆压缩与 3D 投影

- 引入租约冲突处理、任务记忆压缩、实时游标恢复和多任务看板。
- 将成员状态、敲键盘、前往会议室、休息等行为定义为事件投影；面对面交互只在确有协作消息时触发。

### 阶段 6：旧执行路径退役

- 新任务只走 Codex-first 路径。
- 旧运行保留只读查看、导出和必要的迁移入口；不再让旧流程创建新的执行副作用。
- 完成回滚演练后再删除旧写路径。

## 11. 验收标准

1. 使用真实供应商完成模型调用、流式输出和至少一个真实本地工具调用。
2. 能明确显示出错的 Provider、Route、Model、Thread/Turn、阶段和上游错误。
3. 负责人可在原任务中分析失败、检查副作用、重试或转派，且不会重复已完成的破坏性操作。
4. 等待审批、停止、异常退出、可继续和完成在 UI 中有不同状态和明确下一步。
5. 两个以上并行任务不共享上下文、不覆盖彼此工作副本，成员消息只在所属任务内可见。
6. 重启客户端后能恢复同一运行会话和实时事件游标。
7. 供应商目录可以按连接全量更新；目录变化不会静默改写既有 Agent 绑定或运行实例。
8. 三种权限均有真实测试；负责人协助审批不能扩大用户未授予的范围。
9. 长任务压缩后仍保留目标、决定、未完成工作和副作用证据。
10. Codex/OpenCodex 升级和回滚均有版本记录、兼容检查和活动任务排空策略。

## 12. 风险、许可与未决问题

### 已知风险

- 两个本地运行时进程增加健康检查、日志和版本管理成本；通过单一 Runtime Bridge、readiness 检查和有界队列降低风险。
- OpenCodex 是独立社区项目，供应商账号池和上游服务条款需要管理员确认；默认不启用隐式账号池切换。
- 自定义供应商可能只支持文本或不支持工具；必须快速失败并给出具体能力缺口。
- Codex App Server 部分接口处于实验阶段；必须锁定版本和 schema，不能把实验接口当作无版本承诺。
- Apache-2.0 的 Codex 源码和 MIT 的 OpenCodex 需要保留版权、许可证和修改声明；商标不随许可证授予。
- 将上游源码直接纳入项目会增加构建体积和安全审计范围；通过固定 commit、最小构建产物、依赖锁定和定期上游同步降低风险。

### 仍需 POC 决定

1. 一个 App Server 内如何安全地承载不同成员的模型路由，还是按路由启动隔离的运行时配置。
2. 当前 OpenCodex 版本用于模型目录同步的稳定管理 API 与认证方式。
3. 生产支持的 Codex App Server 版本窗口，以及 schema 生成与兼容策略。
4. “完全访问”下工作副本、网络和安装操作的最终本机安全边界。
5. 是否采用 Codex 原生协作工具；在未验证前，团队 MCP 工具由 Forge Bridge 作为唯一业务事实来源。
6. 源码已采用 git submodule 固定；独立构建和打包仍需实现并验证，禁止回退到 PATH 中的个人全局安装。

## 13. 参考资料

- [Codex App Server 官方文档](https://learn.chatgpt.com/docs/app-server)：Thread/Turn/Item、启动/恢复/中断、流式事件和审批接口。
- [Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)：自定义 provider 的 `responses` 协议边界及记忆/压缩配置。
- [Codex MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)：本地 STDIO/HTTP 工具、启停与审批配置。
- [OpenCodex 仓库](https://github.com/lidge-jun/opencodex)：多供应商适配、模型路由、目录/健康检查和请求日志。
- [OpenAI Codex 许可证](https://github.com/openai/codex/blob/main/LICENSE)：Apache-2.0 许可与再分发要求。
