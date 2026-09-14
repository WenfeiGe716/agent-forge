# Forge、OpenCodex 与 OMX 源码复用方案评估

日期：2026-09-12。状态：决策已进入实现；Rust 产品后端与按运行实例的 OpenCodex 网关已落地，OMX 仍按模块验证后逐步复用。

约束：Codex 源码不改；Forge、OpenCodex、OMX 均可修改或直接复用。已有文档中限制 OpenCodex/OMX 只能薄适配的条款不再作为约束。

## 建议

采用“OpenCodex 完整协议内核 + Forge 专用启动模式；OMX 按模块抽取和改造；Forge 保留唯一业务状态库并收敛调度；Codex 原样运行”。不要把三个项目整体叠成独立调度层，也不要一次重写 Forge 全部后端。

重点不是 fork 多深，而是复用后能否删除重复代码，并让配置、任务状态、执行事实分别有明确的责任方。

## 实际代码证据与当前缺口

1. `runtime/opencodex-gateway.ts:runGateway` 已直接加载上游 `startServer`；它使用 `resolveServiceHomes`、`inspectNativeCodexOwnership`。上游 `third_party/opencodex/src/server/index.ts:StartServerDeps` 明确将这两项标为 test-only。当前做法能验证链路，但长期生产接口应该由允许修改的 OpenCodex 提供，不能继续依赖测试钩子的稳定性。
2. `backend/forge/codex_scheduler.py:_frozen_provider/_frozen_agent_provider` 仍要求保存的连接为 `openai_responses`。因此网关已有协议转换能力，不等于 Forge 已能把任意供应商模型交给 Codex。
3. `backend/forge/codex_gateway.py` 从进程设置匹配一个 URL 并读取令牌。它修复了已配置网关的鉴权传递，但不负责供应商同步、路由版本、网关启动或恢复绑定。
4. `backend/forge/codex_task.py:stream` 在网关模式仍向 Codex 子进程注入 `FORGE_MODEL_KEY`。建议后续让网关持有上游凭据，Codex 仅获得受约束的网关令牌；这属于 Forge 的启动配置改造，不需要改 Codex。
5. `backend/forge/codex_scheduler.py:CodexRunExecutor` 同时处理成员启动、并发、审批递交、结果集成、事件记录和状态修改；`autonomy.py` 也承担队列、恢复、结果和交付判定。直接再接一个完整 OMX 调度器会产生重叠责任。
6. `runtime_events.py:RuntimeEventRecorder` 已提供有价值的持久化入口；`team_mcp.py` 已提供任务范围的协作工具入口。应该复用这些可见接口，将它们背后的调度和事务逻辑集中起来，而不是删除重写所有代码。

## OpenCodex：建议维护 Forge 专用运行模式

保留其协议转换、流式处理、工具格式转换、模型路由及相关测试；在它自己的源码中增加正式 gateway-only 入口。入口接受显式路径、绑定地址、配置来源、凭据来源、生命周期和日志回调；无需 Forge 伪装成上游原生服务，且不启动面向用户 CLI 的配置修复、安装和桌面集成功能。

通过项目内固定版本的独立进程运行。初期每个 Forge 应用数据目录一个网关即可，避免每个成员重复启动；若某供应商适配器存在不可分离的全局状态，再按配置版本分进程隔离。不要未经验证就假设一个进程能安全承载全部配置版本。

配置归属建议：

- Forge 管理供应商资产、凭据修订、模型选用和任务快照。
- OpenCodex 接收可校验的运行配置投影，负责执行请求；管理页面不再成为第二个独立配置入口。
- 运行前由 Forge 编译一个 RouteBinding：供应商修订、模型、上游协议、网关路由标识、配置摘要、凭据引用、能力证据和网关版本。
- Codex 只得到 Responses 地址和路由标识。用户填写原始供应商地址，不要求手工拼 `provider/model` 或理解内部网关令牌。
- 网关按路由版本保留配置，配置变更只影响新绑定。旧任务继续原绑定；凭据被撤销时明确要求处理，不能暗中换到另一供应商。
- 恢复前核验路由绑定。稳定本地代理地址或受控的配置重绑定需要选定一种契约，避免动态端口变化使冻结的 Codex 配置失效。
- 每任务令牌若要具有隔离作用，必须在网关验证允许的路由集合；仅生成不同令牌而共享全部访问权限并不构成隔离。

初期所有生产任务统一走网关最容易形成一致契约；直连可以保留给开发诊断。性能证明网关开销有实际影响后，再决定是否增加统一 RouteBinding 下的直连优化。

## OMX：按模块复用，不先移植完整 Team Runtime

已取得用户提供的完整本地源码 `third_party/oh-my-codex`，HEAD 为 `cb955b0d5becbef76d2c1f0096b6e1f238e1e7f7`，包版本 0.21.5。以下判断针对该 checkout，不代表上游最新版本。此前仅取得部分远程文件的证据限制已被本次本地核验替代。完整源码揭示其运行体系是 TypeScript + Rust，并且存在无需 tmux 的 prompt worker 模式；详细影响见下文。

`state/tasks.ts` 的 `computeTaskReadiness` 接受 readTask 依赖，适合提取；claim 接口已经暴露读取、锁、标准化等依赖，但同时需要 taskFilePath/writeAtomic，不能只换一个数据库连接就直接使用。`state.ts` 还连接文件存储、session hooks、runtime bridge；Team runtime 的导入包含 tmux、pane、HUD、child_process 和团队状态操作。

因此分三类处理：

| 能力 | 建议 | 前置条件 |
| --- | --- | --- |
| 任务依赖、claim/lease、状态转换、重复消息处理规则 | 优先抽取或移植算法与对应测试 | 存储操作转换为 Forge 的事务、版本检查和领取代次 |
| 角色提示、Skill 元数据、协作策略 | 选择性复用为 Forge 版本化资产 | 去除全局路径、固定模型和固定团队流程假设 |
| worktree、恢复和诊断辅助逻辑 | 比较后复用 | 生命周期、权限和目录所有权必须由 Forge 控制 |
| tmux pane、HUD、全局 setup、CLI 自升级 | 不进入默认 Mac 执行路径 | 将来确有 CLI 兼容需求再做独立模式 |
| 完整 Team runtime / 独立状态后端 | 暂不整体引入 | 只有证明可替换现有协调器、可删除重复状态管理时才重新评估 |

语言选择也应实测：现有业务事务主要是 Python/SQLAlchemy。仅为复用少量 TypeScript 算法增加 RPC 和跨进程写事务并不划算。短算法可以保留来源和测试后移植到 Python；较完整、低副作用的模块才考虑保留 TypeScript。不要同时维护同一规则的两份实现。

建议先做有明确淘汰条件的小样：选择 claim/lease 与 mailbox 幂等各一条真实路径，比较依赖数量、替换代码量、故障恢复行为。若复用必须带入大量 CLI 状态或双写，则拒绝该模块的直接集成，而不是因已投入工作继续扩大 fork。

## Forge：保留产品能力，重构运行控制

保留用户、项目、供应商资产、组织、Skill、审批记录、成果和前端。优先收敛以下内部职责，初期仍可留在现有 Python 进程，无需微服务化：

1. RouteCompiler：供应商快照转换为冻结的运行路由。
2. RunCoordinator：唯一的任务启动、成员领取、暂停、继续、转派和完成判定入口；负责人模型通过现有 MCP 请求动作，不直接写状态。
3. CodexSession：原样 Codex 的进程、Thread/Turn、interrupt/resume、事件和审批响应。
4. WorkspaceManager：工作副本、租约和结果集成。

这四项是职责划分，不要求马上创建四套框架。优先消除现有重复逻辑，再形成小接口。

业务状态以 Forge 数据库事务为准；Codex 通知是执行事实；OpenCodex 日志是请求诊断。事件保存、业务状态更新和待投递消息应设计为一致事务；外部进程副作用不能假装拥有数据库的 exactly-once 保证。引入领取代次/fencing，拒绝过期 worker 提交。断线、进程退出、回合完成、成员完成、整项任务完成分别建模。

特别注意：Codex turn 完成不等于用户任务完成；成员提交成果不等于负责人验收；进程重启后不得自动重放结果未知的写文件或命令。恢复必须先核对会话、文件和已有结果，再决定继续或新 Attempt。

## 实施顺序与淘汰门槛

1. 冻结可重复的现状测试；补两个关键反例：运行中修改供应商配置、写文件后进程崩溃。
2. 改 OpenCodex 的正式 gateway-only 入口和桌面生命周期，同时完成 RouteBinding；此时接通现有真实供应商资产，淘汰要求用户手工配置网关的临时路径。
3. OMX claim/mailbox 小样通过后，替换 Forge 对应重复逻辑，统一 RunCoordinator 的状态写入。
4. 验证“成员失败—负责人读取证据—原任务重试/转派—成果集成—验收”；再覆盖重启、过期提交、审批断线、并行工作目录隔离。
5. 通过真实供应商协议矩阵后移除旧模型/工具循环；抽出仍有用的成果集成等函数。清理数据与架构迁移分别处理，不因为文档允许重构就自动删除运行记录。

完成标准不是新增模块数量：用户只配置一次供应商；所有任务入口使用同一绑定；只有一套团队调度和业务状态写入；Codex 源码零改动；新旧执行器不再同时承载生产运行。

## 完整源码复核：Forge 与 OMX 谁更好

结论：**OMX 的任务领取、投递去重协议比 Forge 当前 Codex 调度更完整；Forge 的现有 App Server 接入和产品状态模型更适合作为本产品重构起点。建议保留 Python 后端，按 OMX 规则重构 Forge 的协调事务，不整体嵌入 OMX Team runtime。** 这是当前方案选择，不是对两者所有运行场景的质量总排名。

| 比较项 | 源码判断 | Forge 的取舍 |
| --- | --- | --- |
| 并发领取、过期提交 | OMX 较强：锁内重读、版本、claim token、租约与转换校验；Forge 主要是内存 member_jobs 和本机 runner 锁 | 替换 Forge 领取路径，借鉴 OMX 协议及反例测试 |
| 投递身份、重放 | OMX 有永久 request ID ledger、事件重建和异常历史拒绝 | 使用数据库唯一键、事务 outbox 和去重收据承接规则，避免再引入一套 JSON 权威库 |
| 桌面退出后的恢复 | OMX resume 有存活会话/进程内 handle 限制；Forge 有持久化 Thread 与同 run 恢复路径，但两者均未证明任意副作用后的安全恢复 | 保留 Forge 恢复契约，增加执行代次和未知结果核对 |
| Codex 会话接入 | Forge 已实现 App Server Thread/Turn、事件与审批；OMX Team 支持 interactive/prompt，不能说只支持 tmux | 保留 Forge CodexSession 接入，不因复用调度改为终端驱动 |
| 权限、供应商冻结、成果交付 | Forge 已实现本产品对应检查；OMX 的完成规则不等同 Forge 用户验收 | 保留产品契约，集中到同一个事务入口 |
| 状态与分发成本 | OMX 已是 TS + Rust，且存在 TS JSON fallback | 不以“统一 TypeScript”为由整体引入 |

### 1. 任务协议的优势真实存在，但任务状态没有全部迁入 Rust

`src/team/state/tasks.ts:58` 的 claim 在锁内重新读取，校验 expectedVersion、依赖、worker 成员身份、活动 claim，生成 token、设置租约并增加版本。`:193` 附近的转换路径校验领取身份与租约。相比之下，Forge `backend/forge/codex_scheduler.py:411` 的 `_sync_members` 使用 pending/queued 记录、并发预算和进程内 member_jobs；`backend/forge/db.py` 的 Task 尚无对应的一等 claim token/lease/version 字段。

`src/runtime/bridge.ts:228` 的 RuntimeBridge 通过同步子进程运行 Rust `omx-runtime`，默认启用，设置 `OMX_RUNTIME_BRIDGE=0` 才关闭。`crates/omx-runtime-core/src/lib.rs` 中命令涵盖 authority、dispatch、mailbox 和 replay，未包含任务 claim/transition。准确划分是：**任务领取规则主要在 TS；投递、邮箱和 authority 等状态引入了 Rust 权威。** 不能只读 bridge 文件头注释就认为所有任务语义写入都已统一。

### 2. Rust 处理了并发与历史去重，但不应当作现成 SQL 事务层

`crates/omx-runtime/src/main.rs:73` 使用进程间排他锁包住 load → process → persist → compatibility view。`crates/omx-runtime-core/src/engine.rs:291` 在事件压缩前写入永久 dispatch identity ledger，防止 request ID 因历史清理而重新可用；加载路径拒绝部分不完整或不合法历史。这些规则值得采用。

但 persist 使用多次 `std::fs::write` 写 snapshot、ledger、events、mailbox 和 dispatch，并非跨文件原子提交。并发锁不能证明中途进程死亡时所有文件共同提交。`src/team/state/dispatch.ts:176` 的桥接调用会捕获错误返回 false；`:287` 附近排队路径在桥接失败或读回失败后仍可写 legacy JSON。存在“命令是否已经提交”的歧义需要处理，不能简单移入 Forge 后再与 SQL 双写。

本次只确认上述设计风险，没有执行崩溃注入来证明具体数据损坏。也不能把 OMX 的消息去重称为外部文件、shell 或模型调用 exactly-once。

### 3. OMX 不是只能使用 tmux，但 prompt 模式也不是现成的桌面恢复方案

`src/team/runtime.ts:3186` 的 spawnPromptWorker 启动子进程；startTeam 中有 interactive 与 prompt 分支。此前从导入列表判断终端耦合，只能说明完整文件混合了终端职责，不能说明必须 tmux。

`runtime.ts:5679` 的 resumeTeam 会检查冻结执行绑定。prompt 模式中，即使 worker PID 仍活着，重启后缺少当前进程里的 handle，也会记录 `prompt_resume_unavailable:missing_handle` 并返回 null；interactive 模式依赖原 tmux session 存活。`src/team/__tests__/runtime.test.ts:12208` 附近有对应回归测试。monitor 对过期 claim 的回收和全员死亡诊断，也不等于自动恢复已产生文件副作用的工作。

Forge 当前 run 路径可读取 prior_thread_id，并有同 run 重试/转派、既有执行证据与成果集成逻辑。它更符合本产品需要继续保留的契约，但当前测试尚不足以证明并发提交与所有重启场景正确。权限与成果检查也不能替代最终功能验证。

## Forge 是否需要换开发语言

**当前决策：不做全后端语言迁移，也不为这次协调器改造新增必需的 TypeScript 服务。保留 Python + SQLAlchemy，前端与桌面继续使用现有 TypeScript；OpenCodex 保留自身运行环境。**

理由不是保守维护旧代码，而是这次要解决的缺口是持久化领取、单一事务提交、消息去重和恢复状态，换语言并不会自动获得这些能力。OMX 最值得采用的 claim/transition 规则可以迁入数据库事务；直接复用整个运行时则会带入 TS、Rust binary、JSON 状态布局和进程桥接，同时还需要保留 Forge SQL 产品状态。

若单独采用 Rust dispatch 核心，其 JSON CLI 也可以由 Python 调用，并不要求先迁到 TS。但当前建议仍是在 Forge SQL 中实现所需协议，避免形成两个事务域。只有将来决定整套采用 OMX 的 worker 生命周期、策略和存储，并能实际删除 Forge 对应职责时，才重新评估保留 TS + Rust 核心。那将是运行架构变更，不是单纯语言升级。

不建议把 OMX 全部翻译成 Python。复用范围限定为状态协议、可独立的策略资产与测试场景；保留来源、版本和适用许可，Forge 中只维护一份生产规则实现。OMX 后续升级按相关协议变更评估，而非同步整个 CLI 项目。

## 建议落地的单一状态契约

1. 领取：同一个数据库事务检查依赖、当前状态与版本，并写入 owner、claim token、lease、attempt generation。使用条件更新/唯一约束实现冲突判定，不能只把文件锁逻辑改成普通查询。
2. 提交：结果必须绑定 run、task、attempt 和 claim；同一幂等键重复提交返回原结果，冲突结果明确拒绝。租约过期或旧代次结果不得覆盖新执行。数据库 fencing 只能阻止状态提交，仍需工作目录隔离与进程管理约束外部副作用。
3. 投递：任务变化与 outbox 同事务提交，发送可重试，消费者持久化去重；收到消息与工作完成分别表达。
4. 恢复：区分线程可继续、进程失联、结果未知、已提交待集成。先核对会话、目录和收据，再继续或创建新 attempt；不要因为租约过期就盲目重放外部命令。
5. 收敛：`team_mcp`、调度器与恢复入口调用同一协调模块，替换重复写状态路径。不要额外叠一层 OMX 调度器再让 Forge 调度它。

这组契约是后续实现目标，不是已完成能力。OpenCodex 的正式 gateway-only 入口和 RouteBinding 按前文推进，Codex 源码保持原样。

## 验证记录与限制

- 固定 OMX checkout：`cb955b0d5becbef76d2c1f0096b6e1f238e1e7f7`，package 0.21.5。本次已阅读完整源码中的任务、bridge、dispatch、Rust engine、运行恢复及相关测试路径，不表示逐行审计全仓库。
- Forge 本轮执行：`PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/test_codex_scheduler.py backend/tests/test_same_run_recovery_controls.py -q`，**16 passed**。首次未设置 PYTHONPATH 导致导入失败，修正后通过。测试证明现有覆盖场景，不证明真实模型或进程崩溃行为。
- OMX 尝试离线 Rust 定向测试：`cargo test --offline -p omx-runtime-core -p omx-runtime --test execution`，因本地缺少 `fs2` 依赖而未能构建，不能记为测试通过。未安装依赖，未执行 setup，未修改个人 Codex 配置。
- OMX 测试仅阅读的证据包括 execution.rs 的并发同 ID 投递、缺失 ledger 拒绝和进程死亡释放锁，以及 runtime.test.ts 的 prompt handle 丢失拒绝恢复；不使用测试数量作为质量排名。
- 仍需在后续实现中执行六类故障测试：重复领取、过期 worker 提交、写文件后崩溃、消息重复投递、负责人恢复、审批连接过期。当前比较没有端到端性能/成功率数据，不声称总体可靠性已分出胜负。
- 本轮仅更新分析文档；`third_party/codex` 工作树检查无改动。OpenCodex 版本以 `third_party/UPSTREAMS.lock` 为准。

## 用户选择 Rust 后的修订方案（2026-09-12）

用户倾向统一使用 Rust。这个方向可以成立，但应区分“最终目标语言”和“一次性全量重写”。更好的目标是：**Rust 作为 Forge 后端的唯一长期宿主，SQLite 作为唯一业务事实源，Codex 保持未修改并作为独立进程，OpenCodex 暂作为网关 sidecar，前端暂不改。Python 只作为有退出期限的迁移兼容层，不再扩展新的协调职责。**

### 为什么不是直接把 OMX Rust runtime 拿来当 Forge 后端

OMX Rust 内核的命令列表主要覆盖 authority、dispatch、mailbox、replay、snapshot，不包含 Forge 所需的 Task claim、Attempt、业务验收、供应商路由冻结和成果集成。其 dispatch 还带有 Tmux transport 语义。`omx-runtime` 当前通过“文件锁—加载—处理—多文件持久化—兼容视图”执行单次命令，适合借鉴 reducer 和去重规则，不适合直接成为 Forge 的第二个权威状态库。

因此采用“Rust-first Forge”，而不是“Forge 嵌入完整 OMX Team runtime”：抽取 OMX 的协议规则和测试思想，重写存储适配为 SQLite 事务；不把 OMX 的 JSON 状态文件与 Forge SQL 长期双写。OpenCodex 继续负责协议转换，Codex 源码零修改。

### 推荐的 Rust 模块

- `forge-core`：Run、Task、Attempt 状态机和命令处理，包括 claim/release/submit/recover、执行代次、冻结执行策略；不依赖 HTTP、进程或 SQLite。
- `forge-store`：SQLite migration、事务、条件更新、唯一约束、outbox、事件 cursor、幂等命令结果。每个业务命令在一个事务中同时写状态、事件和待投递消息。
- `forge-runtime`：Codex App Server 进程与连接管理、Thread/Turn、审批、interrupt/resume、工作目录和工件；所有回调携带 attempt generation，旧回调直接拒绝。
- `forge-server`：暂时实现现有 FastAPI 的 HTTP/SSE 契约，迁移完成后替代 Python API。

不要长期保留“Python 控制面 + Rust 队列 + Rust 运行器”三套写状态的服务。模块可以分 crate，生产桌面进程先保持单一 Rust 宿主；Codex 和 OpenCodex 因为独立上游生命周期保留 sidecar。

### 分阶段迁移

1. **契约冻结**：保存现有 HTTP/SSE 的路径、字段、排序、错误码、鉴权/CSRF、approval 格式、SSE cursor 和下载行为。新旧运行记录不能由两个写者混合处理。
2. **先做 Rust 事务核心**：加入 claims、attempts、commands、events、outbox 表。验证并发领取仅一人成功、旧 lease/旧 attempt 提交被拒绝、重复 command 返回原结果、提交后进程崩溃仍可查询结果。
3. **做一条真实垂直链路**：Rust 创建 Run，启动未修改的 Codex App Server，完成 Thread/Turn/stream/submit/reconcile；Python API 暂作兼容转发，但 Rust-owned run 的写操作只能进入 Rust。
4. **逐块迁移产品语义**：供应商快照与 RouteBinding、审批、工作区、恢复、验收、组织与权限按完整事务边界迁移；每完成一块删除对应 Python handler。
5. **最后处理网关语言**：OpenCodex 先保持 Node/Bun sidecar。只有协议兼容、生命周期和真实供应商矩阵稳定后，才单独评估 Rust 化 OpenCodex；不要把网关重写和 Forge 迁移绑在同一次风险中。

### 前端可以暂时不改

可以。现有 React 前端通过 `/api` JSON、Run SSE（含 `after` cursor）、`/codex-start` 特殊流和静态资源入口工作；只要 Rust server 兼容这些 HTTP、cookie、CSRF、SSE 断线续传、下载和错误语义，前端无需知道后端从 Python 变为 Rust。前端暂缓不会阻塞后端重构，但必须把契约测试冻结下来，不能只对比几个 JSON 字段。

### 采用 Rust 的真实收益与边界

收益是部署依赖收敛、进程生命周期统一、状态转换类型化、单一写入责任和更容易建立 fencing/outbox 语义。Rust 不会自动让模型调用更快，也不会自动提供跨文件 exactly-once；可靠性仍取决于 SQLite 事务、外部进程收据、工作目录隔离和恢复策略。

因此最终决策为：**Forge 迁移到 Rust 是合理的长期方向；第一步不是全量重写，而是 Rust `forge-core` + `forge-store` 的事务核心和一条兼容现有前端的真实执行链。前端暂时冻结，Codex 保持不动，OMX 作为协议与测试来源按模块复用。**
