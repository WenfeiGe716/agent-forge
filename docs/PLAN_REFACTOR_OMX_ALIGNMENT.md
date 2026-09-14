# Agent Forge 后续重构计划（Codex + OMX 对齐版）

> **当前方案已更新（2026-09-12）**：本文记录的 Python 控制面是历史阶段方案。当前执行依据改为 [`PLAN_RUST_BACKEND.md`](./PLAN_RUST_BACKEND.md)：Forge 后端长期统一 Rust，SQLite 为唯一业务事实源，前端暂不改，Codex 源码不改，OpenCodex 暂作为独立网关保留。

> 2026-09-12 实施边界更新：按用户确认，OpenCodex、OMX 均可修改或直接复用源码，Forge 当前实现也可重构；Codex 源码保持不变。下文“优先适配”“不改上游”“仅维护小补丁”等表述是原方案的维护建议，不再限制 OpenCodex/OMX 的实现选择。仍保留独立运行目录、权限隔离、版本及许可证记录和真实验证要求。

> 日期：2026-09-12  
> 目标：macOS 本机应用优先；旧运行记录不迁移，重新建立真实执行闭环。

## 结论

oh-my-codex（OMX）应当被当作 **Codex 的工作流与运行支持层**，而不是新的模型执行器。它明确保留 Codex 作为实际执行引擎，并在外围增加角色关键词、Skill、项目指导、持久化目标、团队运行、HUD、诊断和恢复能力（上游 README 的 “What OMX is for” 与 “simple mental model”）。

**本轮决策更新**：OpenCodex 与 OMX 的源码均允许在项目内复用或做可回滚改造；Codex 源码保持原样。当前实现已经把 OpenCodex 作为按运行实例启动的网关，并保留可审计补丁与固定版本；OMX 则先复用其任务领取、租约、邮箱去重和恢复规则，只有能够替换 Forge 的重复职责时才引入对应源码模块。这样不会把“允许改造”误解成同时维护三套执行器。

因此 Agent Forge 采用三层边界：

```text
Agent Forge：团队、成员、权限、任务、审批、3D办公和审计
        ↓
OMX 对齐层：目标/恢复/团队状态/诊断的项目级适配，不改用户配置
        ↓
Codex App Server：模型回合、文件与命令工具、沙箱、审批、流式事件、恢复
        ↓
OpenCodex：供应商协议适配、模型目录、健康检查和路由
```

不直接把 OMX 的固定工作流复制进产品，也不让 OMX 或 Agent Forge 读写用户个人 `~/.codex`。所有运行状态写入 Agent Forge 自己的应用数据目录；每个任务使用独立 `CODEX_HOME`、Thread 和工作副本。

## 从 OMX 借鉴什么

| OMX 能力 | Agent Forge 的采用方式 | 不直接照搬的部分 |
| --- | --- | --- |
| `omx exec` 真实 smoke test | 把“安装/配置检查”和“真实模型调用检查”拆成两个状态；绿色检查不能冒充可执行 | 不依赖用户 shell 中的全局 `codex` |
| `.omx/` 目标、日志、记忆和恢复状态 | 映射到 Forge 的 Task、Attempt、MemoryContext、RuntimeEvent | 不在用户仓库写入不可控的运行状态 |
| Team 使用独立 worktree、显式 session root | 任务×成员工作副本租约；并行任务绝不共享可写目录 | 不以 tmux 作为 Mac 客户端的必要条件 |
| HUD 的 `working/idle/blocked/done/failed/draining/unknown` | 作为成员与任务的真实状态枚举，并由事件推导 | 不用状态文件代替进程与事件事实 |
| doctor、resume、rerun、finish 检查 | 形成“诊断—负责人恢复—人工接管”的统一入口 | 不把 `$deep-interview → $ralplan → $ultragoal` 变成强制流水线 |
| hooks / plugin 生态 | 仅作为可选扩展适配器，按任务权限启用 | 不执行会修改 `~/.codex` 的安装脚本 |
| `--madmax` 的显式高权限语义 | 对应用户的“完全访问”档，并明确展示风险 | 绝不作为默认权限 |

## 旧任务清理边界

第一阶段开始前执行一次“旧运行清理”，不做数据迁移：

- 删除旧 `Run / Task / Event / Meeting / Activity` 运行记录及其运行工作副本。
- 删除旧运行的临时 Codex/OpenCodex runtime home、缓存和孤立租约。
- 保留 Provider、模型目录与配置、Agent、角色模板、Skill、团队模板、楼层布局、项目配置和审计中的配置变更记录。
- 清理必须显示将删除的记录数与目录范围；完成后不可恢复，不触碰用户 `~/.codex`、个人 Codex 会话或其他项目 Docker。

## 阶段目标

当前进度：阶段 0 已完成；阶段 1 的桥接、事件记录、正式入口和本地默认调度切片已完成（由 `FORGE_CODEX_DEFAULT_SCHEDULER` 在新 Run 快照时启用，旧 Run 保持原执行器）；阶段 2 已完成同一 Run 内恢复切片，并已补上 Codex Thread 的冻结配置复用、同线程 resume、活动回合 interrupt 和 steer 接口；阶段 3 已补上运行时可选的、任务范围内的负责人 MCP 工具桥接（`FORGE_CODEX_TEAM_MCP`，默认关闭，避免未打包的子进程被误启用），并在负责人提示中明确“按需协作、先读证据再恢复”的真实控制契约；本轮已加入成员委派的独立 Codex Thread/工作副本/运行目录、成员实时事件按任务隔离、结构化 `submit_result`（含真实成果校验、幂等防覆盖、done 后集成）以及负责人验收门；阶段 6 已完成任务页对 `runtime.*` 事件的实时投影。完整 Mac 端到端联调、模型供应商可用回合和负责人自动恢复回合仍在进行中。

### 阶段 0：清理与契约冻结

**目标**：建立干净基线，停止继续在旧执行器上叠加补丁。

**交付**：运行数据清理命令、迁移/删除审计、Task/RuntimeSession/Turn/Item/Attempt/ApprovalRequest/RuntimeEvent schema、Codex/OpenCodex/OMX 版本锁定表。

**验收**：新数据库无旧运行；配置资产仍可打开；个人 `~/.codex` 的读写探针为零；重复执行清理不会误删配置资产。

### 阶段 1：Codex-first 单任务真实闭环

**目标**：一条任务真实完成“模型 → 工具 → 本机项目目录 → 流式输出 → 结果”。

**交付**：Runtime Bridge 正式入口、事件持久化与回放、供应商协议/模型真实探测、`turn/start`、工具调用、完成/失败/中断事件；保留当前 POC 作为回归测试，不再作为临时 NDJSON 旁路。

**验收**：使用一个已验证供应商完成真实文本任务和无副作用工具任务；前端刷新后可从事件游标恢复；错误能指出 Provider、Model、Thread、Turn、阶段、HTTP 状态和是否发生副作用。

### 阶段 2：同一 Task 的负责人恢复

**目标**：失败不创建新任务，先由负责人判断并继续原任务。

**交付**：负责人 Thread 与控制工具：读取成员能力、读取结果、检查副作用、重试原 Attempt、转派团队内成员、请求补充信息、提交交付候选；状态统一为 `running / waiting_approval / recovering / paused / completed / failed`。

**验收**：成员失败后，负责人可在原任务内完成“分析 → 重试或转派 → 继续 → 验收”；只有负责人明确无法恢复且用户未处理时才进入 `failed/needs_attention`。

### 阶段 3：团队自治协作

**目标**：去掉固定业务 DAG，由负责人依据团队能力目录决定是否协作、找谁协作、交互几轮。

**交付**：团队能力目录、任务内成员会话、团队消息、唤醒/回复、并行分工、会议行为事件；成员只能访问本任务授权信息，不能跨任务读记忆或扩大权限。当前切片已为每个成员任务建立独立 Codex Thread、`CODEX_HOME` 和工作副本，并把事件写入对应 `{run_id}:{task_key}`；成员必须通过 `submit_result` 报告真实结果，done 成果自动进入既有集成检查，blocked/waiting 保留为可恢复边界。

**验收**：一个复杂任务至少出现一次负责人分工、成员消息往返和结果汇总；一个简单任务可由单个 Agent 完成且不强制开会；成员失败可转派给同团队其他成员。

### 阶段 4：权限、工具与本机执行

**目标**：让“本机项目目录内执行默认、越界和联网安装需审批”真正落到 Codex 原生边界。

**交付**：

- `请求审批` → Codex approval request，用户逐项决定。
- `帮我批准` → 负责人可代处理已授权范围内的低风险审批，仍留审计记录。
- `完全访问` → 显式映射到高权限 sandbox/approval 策略，每次保存保留确认记录。
- 文件、命令、网络、工具安装均生成 ToolInvocation 与 ApprovalRequest。

**验收**：项目目录内代码修改可真实执行；越界路径、外网请求、工具安装分别产生明确审批；没有把“模型支持工具”误判成“模型拥有本机权限”。

### 阶段 5：记忆压缩与并行隔离

**目标**：长任务和并行任务不串线、不覆盖。

**交付**：每个 `Task × Member` 独立 Thread/MemoryContext；压缩保留目标、约束、决定、未完成事项、文件变化和工具收据；每个成员/任务分配独立工作副本租约；未知副作用时禁止盲目重放。

**验收**：两个任务并行修改同一项目时不会共享 Thread 或可写目录；重启后可恢复同一 Thread；压缩前后负责人仍能继续任务。

### 阶段 6：Mac 客户端统一运行视图

**目标**：把任务、审批、恢复和实时输出放回 3D 办公室，不让用户在多个后台页面之间猜状态。

**交付**：

- 发送需求后直接启动；只在真正阻塞时显示审批/澄清。
- 员工卡片展示 `工作中 / 等待审批 / 负责人处理中 / 暂停 / 异常 / 已完成`，禁止泛化成“需人工处理”。
- 3D 员工面板直接处理审批、重试、转派、继续和查看原因。
- 会话抽屉实时显示模型 delta、工具调用、命令输出、成员消息和负责人决策；内容多时虚拟列表、折叠非关键事件。
- 会议室仅是协作的空间投影：需要面对面交流时员工移动过去；单 Agent 或会议室占用时可在工位/休息区完成异步协作，不阻塞执行。

**验收**：从发送到完成只需一个任务视图；刷新/关闭窗口后能继续流式回放；长输出不撑破布局；失败卡片有明确下一步按钮。

### 阶段 7：切换、升级与真实回归

**目标**：安全替换旧执行路径，并能跟随 Codex/OMX/OpenCodex 上游迭代。

**交付**：双实现对照期、特性开关、版本兼容矩阵、上游更新脚本、自动回归、可回滚构建和真实供应商测试矩阵。

**验收**：至少覆盖一个 OpenAI-compatible、一个 Anthropic、一个 Gemini/DeepSeek 类协议和当前自定义供应商；通过文本、流式、工具、审批、失败恢复、并行隔离和 Mac UI 测试后，才移除旧执行器。

## 第一批实现顺序

1. 先清理旧运行记录并冻结 schema。
2. 把 Runtime Bridge 从 POC 变成可持久化的单任务执行器。
3. 接入真实事件流和任务状态 reducer。
4. 加入负责人恢复工具，验证原任务内重试/转派。
5. 再做团队自治、权限映射、记忆压缩和 3D UI。

这样可以先解决“任务真实走通、失败可继续、实时输出可信”三个核心问题，再逐步恢复视觉和组织能力；不会继续为旧任务和固定流程增加配置负担。

## 主要风险与控制

- **上游协议变化**：固定 Codex App Server schema 和 commit，启动时做 initialize/turn/tool contract check。
- **供应商能力不一致**：目录、探测、实际调用分层记录；不以静态标签判断工具能力。
- **高权限误用**：完全访问仅由用户确认；负责人不能提升权限。
- **并行文件冲突**：工作副本租约 + 负责人决策，禁止共享可写目录。
- **OMX 与个人配置串扰**：只复用源码思想/可隔离组件，运行时显式设置 Forge-owned `CODEX_HOME/OMX_ROOT`，禁止访问个人目录。

## OMX 与 OpenCodex 是否要改造

### OpenCodex：需要最小适配，暂不重写

OpenCodex 已经覆盖多供应商协议转换、模型路由、健康/就绪检查、Provider 管理和请求日志；这些能力应继续留在它里面。Agent Forge 需要的不是再写一套网关，而是补齐稳定的机器接口和隔离边界：

1. 通过版本锁定的管理 API 读取 Provider、模型目录、同步批次、能力探测和健康状态。
2. 配置根目录、状态卷、认证令牌和日志目录全部支持 Forge-owned 路径，禁止回退到用户 `~/.opencodex`。
3. 对每个请求保留 `provider/model/route/request_id`，并把上游错误原样映射到 Forge 的 RuntimeEvent。
4. 需要时才给上游提交小补丁：缺少稳定 API、路径覆盖、错误字段或事件关联时再补；不把团队、审批和本机工具执行塞进 OpenCodex。

当前执行链由 Rust `forge-runtime` 连接未修改的 Codex App Server，OpenCodex
作为独立网关进程提供 `/v1/responses` 与 `provider/model` 路由。供应商 revision
和 route binding 在 Rust-owned Run 创建时冻结；OpenCodex 的管理目录仍由独立网关
负责，不在 Forge Python 控制面保留一份重复 client。真实供应商转发通过
`scripts/verify-opencodex-codex-route.py` 验证后再纳入桌面切换门槛。

当前策略是“适配器优先、补丁最小化”。每次升级先跑兼容性检查，补丁无法干净应用时停在旧版本，不自动覆盖运行中的网关。

### OMX：不改成执行内核，只做隔离适配

OMX 的固定工作流、tmux 团队运行、HUD 和 hooks 对 CLI 很有价值，但不是 Mac 客户端的核心执行路径。直接使用其安装/设置流程可能写入 `.codex/config.toml`、`.codex/hooks.json` 或个人状态目录，因此不直接调用其全局 setup。

Agent Forge 只需要一个薄的 OMX 对齐适配器，复用以下思想：

- durable goal / mission → Forge 的 Task、Attempt 和 MemoryContext；
- team worktree / session root → Forge 的 WorkspaceLease；
- HUD 状态 → Forge 的 RuntimeEvent reducer；
- doctor / resume / rerun → 负责人恢复和诊断动作。

只有在后续确实需要 OMX CLI 兼容模式时，才对 OMX 做最小补丁：让 `OMX_ROOT`、Codex home、hooks、状态目录和插件目录都可显式注入，并提供“无 tmux、无全局 setup、JSON 事件输出”的 headless 模式。该补丁必须保持可独立回滚，不能修改用户个人 Codex 目录。

换句话说：

```text
OpenCodex：保留并补机器接口/隔离能力
OMX：保留设计思想，必要时补隔离 headless 接口
Agent Forge：实现产品控制平面和 Mac UI
Codex：继续作为唯一真实执行内核
```

## 2026-09-12 网关鉴权接入验证

已将独立 OpenCodex 网关令牌接入 Codex 负责人、成员、POC 和正式单任务启动入口。后端以 `FORGE_OPENCODEX_GATEWAY_URL` 的完整地址匹配决定是否注入 `FORGE_OPENCODEX_API_AUTH_TOKEN`，不依据 loopback 或供应商名称猜测。匹配路由缺少令牌时拒绝启动；API 返回 409，正式入口在更新运行状态前检查。

验证：网关地址/令牌、调度器负责人及成员、单任务、POC 与 OpenCodex 客户端共 57 项测试通过；真实 Codex → OpenCodex → 本地测试 HTTP 供应商验证通过（不代表付费供应商模型能力验证）。`third_party/codex` 工作树无改动。

本切片尚未实现桌面自动管理网关生命周期、Provider 配置同步或 OMX 源码引入；这些仍按前述阶段推进。
