# oh-my-codex（OMX）源码分析

> 2026-09-12 实施边界更新：按用户确认，OpenCodex、OMX 均可修改或直接复用源码，Forge 当前实现也可重构；Codex 源码保持不变。下文“优先适配”“不改上游”“仅维护小补丁”等表述是原方案的维护建议，不再限制 OpenCodex/OMX 的实现选择。仍保留独立运行目录、权限隔离、版本及许可证记录和真实验证要求。

> 分析对象：[`Yeachan-Heo/oh-my-codex`](https://github.com/Yeachan-Heo/oh-my-codex)，`main` 分支，2026-09-12；仓库公开的 `package.json` 版本为 `0.21.5`。本文只记录上游公开源码与 README 的结论，不读取或修改本机 `~/.codex`。

## 结论摘要

OMX 不是 Codex 的替代实现，而是围绕 Codex CLI 的工作流层。它明确把 Codex 保留为执行引擎，并通过角色提示词、Skill、团队运行时、持久状态、MCP、HUD 和 CLI 对任务进行路由与治理。README 将其定位为 “workflow layer for OpenAI Codex CLI”，并说明实际 agent work 仍由 Codex 完成（[`README.md`](https://github.com/Yeachan-Heo/oh-my-codex/blob/main/README.md#what-omx-is-for)、[`src/index.ts`](https://github.com/Yeachan-Heo/oh-my-codex/blob/main/src/index.ts)）。

对 Agent Forge 的直接启示是：应继续采用“Forge 控制面 + Codex Runtime 执行面”的分层，不要再实现第二套模型循环、工具循环或本地执行器。OMX 的团队与状态代码可以作为持久化、恢复、并行隔离和负责人治理的参考，但不能原样搬入：OMX 面向单机 CLI/tmux、默认依赖项目中的 `.omx/` 和 Codex 用户配置，而 Forge 需要多任务数据库、Mac UI、供应商路由、三种权限、审计和 3D 投影。

## 1. OMX 已经解决的问题

### 1.1 Codex-first 的运行方式

`README.md` 说明 OMX 只增加工作层，不替换 Codex；`omx` 启动后仍调用已安装并认证的 `codex`。`src/cli/omx.ts` 只负责加载编译后的 CLI 入口，真正的启动参数、session 和团队运行时由其他模块处理（[`src/cli/omx.ts`](https://github.com/Yeachan-Heo/oh-my-codex/blob/main/src/cli/omx.ts)）。

### 1.2 可组合而非强制单链路的工作流

README 同时提供 `$deep-interview`、`$ralplan`、`$ultragoal`、`$team`、`$plan` 等入口，并强调这些能力可独立调用；规划阶段不直接改代码，执行需要进入明确的 `$ultragoal` 或 `$team` lane。这个“能力可组合、执行边界显式”的设计适合 Forge 的负责人决策模型，但不应把 `plan → prd → exec → verify → fix` 硬编码成每个任务都必须经过的流程。

### 1.3 角色模板与模型/工具画像

`src/agents/definitions.ts` 为每个角色声明描述、推理强度、模型类别、路由角色和工具访问模式（`read-only`、`analysis`、`execution`、`data`）；例如 `analyst`/`planner` 属于 leader，`executor`/`test-engineer` 属于 execution。`src/agents/native-config.ts` 再把角色提示词、模型和 provider 解析成 Codex 原生 TOML，并支持 exact model 与 per-agent model override（[`src/agents/definitions.ts`](https://github.com/Yeachan-Heo/oh-my-codex/blob/main/src/agents/definitions.ts)、[`src/agents/native-config.ts`](https://github.com/Yeachan-Heo/oh-my-codex/blob/main/src/agents/native-config.ts)）。

这证明 Agent Forge 的“角色模板库”和“Skill 商店”应是独立资产；运行时只把已确认的角色/Skill 快照注入 Codex session，不直接修改模板源。

### 1.4 团队状态、并行任务与恢复

`src/team/state.ts` 定义了 `TeamTask`、owner、claim lease、依赖、阻塞、失败、版本、delegation/coordination 证据、邮箱消息和 dispatch request；同一文件还定义 `TeamLeader`、`TeamPolicy`、`TeamGovernance`。`src/team/runtime.ts` 负责 worker 生命周期、tmux pane、任务 claim/reclaim、心跳、状态快照、清理和恢复（[`src/team/state.ts`](https://github.com/Yeachan-Heo/oh-my-codex/blob/main/src/team/state.ts)、[`src/team/runtime.ts`](https://github.com/Yeachan-Heo/oh-my-codex/blob/main/src/team/runtime.ts)）。

这些结构可直接转化为 Forge 的持久化概念：`TaskAttempt`、`MemberLease`、`MailboxMessage`、`DispatchRequest`、`RuntimeEvent` 和 `RecoveryAction`。关键原则是“同一任务继续运行/重试”，而不是失败后强制新建任务。

### 1.5 团队内通信与可靠投递

`src/team/mcp-comm.ts` 将消息写入 mailbox，再创建 dispatch request，通过 hook、stdin、tmux send-keys 或 mailbox 等传输；每次通知都会记录成功、排队、失败原因并支持幂等去重（[`src/team/mcp-comm.ts`](https://github.com/Yeachan-Heo/oh-my-codex/blob/main/src/team/mcp-comm.ts)）。

Forge 应采用同样的“消息先持久化，再投递；投递有 request/message id；失败可重试”的闭环，但传输层应改为 Codex App Server/Runtime Bridge 事件与请求，不应依赖 tmux 作为事实来源。

### 1.6 运行状态与终止语义

`src/runtime/run-state.ts` 将 active、outcome、lifecycle outcome、phase、iteration、error 等字段归一化并原子写入；`src/runtime/run-loop.ts` 提供 `runUntilTerminal`、最大迭代保护和 continuation snapshot（[`src/runtime/run-state.ts`](https://github.com/Yeachan-Heo/oh-my-codex/blob/main/src/runtime/run-state.ts)、[`src/runtime/run-loop.ts`](https://github.com/Yeachan-Heo/oh-my-codex/blob/main/src/runtime/run-loop.ts)）。

这比 Forge 当前仅用“需人工处理/失败”更明确。Forge 的终态应区分 `completed`、`failed`、`cancelled`、`waiting_approval`、`waiting_user_input`、`paused` 和 `interrupted`，并保留继续执行所需的 thread/turn 标识与最近事件。

### 1.7 协作强度的动态判断

`src/team/coordination-protocol.ts` 根据任务文本、文件/领域重叠和依赖关系合成 `lightweight` 或 `coordinated` 计划；只有出现跨边界、共享文件或依赖时才启用完整协调机制（[`src/team/coordination-protocol.ts`](https://github.com/Yeachan-Heo/oh-my-codex/blob/main/src/team/coordination-protocol.ts)）。

该思路符合 Forge 的要求：负责人根据实际能力与上下文决定是否沟通、沟通谁、沟通几轮；系统只提供安全边界和可观察记录，不预先固定 DAG。

### 1.8 持久状态与运行时隔离

README 约定计划、日志、memory 和 mode state 放在项目/实例作用域的 `.omx/`，并要求并发会话使用不同 root 或 worktree；`src/runtime/bridge.ts` 通过 Rust `omx-runtime` 作为状态权威，命令产生事件，查询读取兼容 JSON（[`src/runtime/bridge.ts`](https://github.com/Yeachan-Heo/oh-my-codex/blob/main/src/runtime/bridge.ts)、README 的 [Concurrent standard conversations](https://github.com/Yeachan-Heo/oh-my-codex/blob/main/README.md#concurrent-standard-conversations)）。

Forge 应吸收“每个任务独立 root、原子状态、事件可重放、并行工作副本隔离”，但所有目录必须由 Forge 自己管理，严禁写入用户个人 `~/.codex`。

## 2. 不应照搬的部分

1. **不要照搬 tmux 作为执行内核。** OMX 的 `src/team/tmux-session.ts`、`src/team/runtime.ts` 适合终端 HUD；Forge 的事实来源应是 Codex App Server/Runtime Bridge，3D 办公室只是投影。
2. **不要默认写用户级 Codex 配置。** `src/agents/native-config.ts` 的注释和实现会生成 `~/.codex/agents/` 或项目 `.codex/agents/`。Forge 必须使用项目拥有的 runtime home，不能触碰个人 `.codex`。
3. **不要把 OMX 的固定 phase pipeline 当作产品流程。** `src/team/orchestrator.ts` 明确列出 `team-plan → team-prd → team-exec → team-verify → team-fix`。Forge 只应保留可选的治理策略，负责人可跳过、并行、重试或改派。
4. **不要复制 OMX 的模型名称和等级。** `frontier/standard/fast`、`gpt-6-astra` 等是 OMX 的角色画像，不是供应商目录。Forge 必须使用自己的 Provider → Model → Capability Profile → Policy 版本链。
5. **不要把 leader 当作绕过权限的超级用户。** OMX 的 leader/worker 角色主要是工作流和调度语义；Forge 仍需将负责人动作映射到用户批准的执行权限，越界访问和联网安装必须经过 Forge 审批策略。
6. **不要直接复用 `.omx` 文件作为 Forge 数据库。** OMX 文件状态适合单机 CLI；Forge 需要任务、成员、供应商、审批、事件的统一数据库和 UI 查询索引。

## 3. 对 Agent Forge 当前设计的判断

当前“Forge 控制面 + Codex Runtime + OpenCodex Provider Gateway”的方向是正确的，但实现仍处于 foundation/POC：旧的 Forge engine/local executor 仍是默认执行路径；Codex POC 已能建立独立 runtime 并发送真实 turn，但尚未把流式事件持久化为 Forge Run/Event，也没有把负责人恢复、审批和同一任务重试接入生产路径。

因此，重构重点不是把 OMX 整个复制进来，而是把其成熟边界映射到 Forge：

| OMX 能力 | Forge 应采用的形态 |
| --- | --- |
| Codex-first CLI | Codex App Server/Runtime Bridge，Forge 不再维护第二套模型循环 |
| Agent definitions | 角色模板快照 + 每个成员独立模型/权限配置 |
| Skill/prompt | 审核后的 Skill 版本，按任务快照绑定 |
| TeamTask/claim/mailbox | Forge TaskAttempt、成员 lease、团队消息和投递状态 |
| run-state/run-loop | 统一生命周期、继续/重试/暂停/人工接管 |
| coordination protocol | 负责人可动态决定协作，系统提供冲突检测和证据 |
| `.omx`/worktree isolation | Forge-owned task runtime root/worktree，不写 `~/.codex` |
| HUD/notifications | Mac UI + 3D 状态投影，数据源为持久化 RuntimeEvent |

## 4. 重新调整后的阶段目标

### 阶段 0：清理与边界冻结

- 清除旧任务记录和历史运行数据（仅 Forge 数据库/项目数据；不触碰 `~/.codex`）。
- 关闭旧 engine/local executor 的生产入口，保留代码仅用于迁移期间对照。
- 固化 runtime home、任务 root、worktree 和密钥存储边界。

### 阶段 1：单任务真实闭环（Codex-first）

- 通过 Runtime Bridge 创建 thread/turn，接入一个已验证供应商。
- 持久化所有流式事件、工具调用、审批请求、错误和终态；支持断线后 replay。
- UI 显示“正在思考/调用工具/等待审批/等待输入/完成/失败”及明确错误模型、请求 ID。
- 验收：真实代码任务至少完成一次文件修改、测试、结果展示。

### 阶段 2：负责人恢复与同任务继续

- 失败时保留原 thread/turn，负责人先读取错误、重试、改派或请求成员协作。
- 仅在不可恢复或用户要求时结束任务；“继续执行”不能新建无上下文任务。
- 审批由负责人提出建议，最终依据团队/成员权限策略执行。

### 阶段 3：动态团队协作

- 负责人获取团队成员能力、模型、Skill、权限和当前负载。
- 根据任务实际依赖决定是否沟通、面对面会面或异步消息；系统记录协作原因和轮次。
- 并行任务使用独立 runtime/worktree，消息按任务隔离。

### 阶段 4：权限与工具治理

- 将“请求审批 / 帮我批准 / 完全访问”映射到 Codex approval/sandbox policy。
- 默认本机项目目录；越界文件、联网、安装工具触发审批；审批状态可继续/拒绝/改范围。
- 工具目录动态来自实际 MCP/本地工具能力，不在 UI 中写死工具清单。

### 阶段 5：记忆、压缩与并行可靠性

- 每个任务独立短期记忆；团队共享仅通过显式消息/产物。
- 对长上下文采用 Codex 原生 compaction 事件并保留压缩前摘要和游标。
- 加入成员 lease、异常退出检测、幂等投递、重试退避和资源限额。

### 阶段 6：Mac/3D 运行视图

- Mac 客户端订阅 RuntimeEvent；3D 员工状态只由事件投影，不直接驱动执行。
- 键盘、走动、会议室占用、休息区等为状态可视化规则；审批、继续、重试、改派在成员面板中完成。
- 任务页支持并行任务切换、实时输出、产物和错误证据，减少说明性文案。

### 阶段 7：替换旧路径与上游升级

- 以 feature flag/canary 将 Codex-first 路径设为默认，旧路径仅在回滚窗口保留。
- 锁定 Codex/OpenCodex/OMX 上游版本与补丁清单；升级时运行协议、权限、事件回放和真实模型测试。
- 发布前验证不写 `~/.codex`、任务隔离、并行运行和恢复语义。

## 5. 最终建议

OMX 值得借鉴的是边界和运行时治理：Codex 负责执行，工作流层负责路由，任务状态可恢复，团队通信可审计，简单任务不强行编排。Forge 不应成为 OMX 的 UI 复刻或 tmux 包装器，而应把这些能力落到自己的数据库、审批模型和 Mac/3D 交互上；这样既能复用 Codex 的持续迭代，也能保持企业级多供应商和人工治理能力。

## 6. OMX 与 OpenCodex 是否需要改造

### 6.1 OMX：优先适配，不改上游

**最小必要改动（放在 Forge 适配层）：**

- 将 OMX 的角色/Skill 元数据转换为 Forge 的角色模板和 Skill 版本快照；不直接写入用户级 Codex 目录。
- 将 `TeamTask`、claim lease、mailbox、dispatch request、run-state 映射到 Forge 数据库，并以任务 ID 隔离。
- 将 OMX 的 leader/retry/coordination 语义转换为 Forge 的负责人策略：负责人可读取成员能力、决定协作、重试或改派；策略结果通过 Codex App Server 执行。
- 将 `.omx` 中可借鉴的日志/记忆格式转换为 Forge 的事件和记忆表，提供游标、重放和压缩摘要。

**不应修改/不应依赖：**

- 不修改 OMX 的工作流提示词、角色定义和 native-agent 生成逻辑来承载 Forge 业务规则。
- 不让 Forge 调用 `omx setup --scope user`，不创建或覆盖 `~/.codex/AGENTS.md`、`~/.codex/agents/*.toml`。
- 不把 tmux pane、HUD 文本或 `.omx` 文件作为 Forge 的唯一事实来源。

这样可以通过锁定 OMX 上游版本、记录适配器契约，在 OMX 更新时只验证适配层，而无需长期维护深 fork。

### 6.2 OpenCodex：保持上游原样，增加薄适配层

OpenCodex 的公开 README 将其定位为本地 provider proxy：把 Codex Responses API 转换为不同供应商协议，支持流式、工具调用、推理和图片，并提供 provider/model 目录、账号池和路由（[`lidge-jun/opencodex README`](https://github.com/lidge-jun/opencodex/blob/main/README.md)）。它是数据面，不负责 Agent 角色、团队生命周期或本地工具权限。

**最小必要改动（Forge 适配层）：**

- Forge 维护 Provider/Model/Capability/Revision 的版本化目录，按 provider ID 生成 OpenCodex 配置；同步上游模型列表后再由管理员确认是否启用。
- Forge Runtime Bridge 只连接 OpenCodex 暴露的 Responses 兼容端点；将 provider、model、request ID 和协议错误写入 RuntimeEvent。
- 将 Forge 的三种权限映射到 Codex Runtime 的 approval/sandbox，再由 OpenCodex 只负责模型协议转换；不要把权限逻辑下沉到 provider proxy。
- 为每个任务使用独立 token、配置和 runtime home；OpenCodex 的 loopback 服务必须启用认证，不能把密钥放进命令行或持久化日志。

**仅在上游缺陷时维护小补丁：**

- 认证、CORS/绑定地址、模型目录同步和协议兼容性等安全/稳定性问题，可以通过可审计的补丁文件维护，并在每次升级时重放和测试。
- 不改 OpenCodex 的核心 provider translation、账号池路由或 UI 业务；若需要 Forge 专属行为，写在 adapter/gateway 层。

### 6.3 组合后的升级策略

1. `third_party/codex`、`third_party/opencodex` 和（如采用）OMX 以固定 commit/版本清单管理。
2. Forge 只维护三类代码：Runtime Bridge、OpenCodex/OMX adapter、控制面策略；上游补丁独立存放并记录原因。
3. 每次上游升级先运行协议 schema、事件回放、工具调用、审批、并行隔离和真实模型 smoke test；通过后再切换默认版本。
4. 升级失败只回滚 Forge 运行时指针，不删除任务事件和已确认配置。
