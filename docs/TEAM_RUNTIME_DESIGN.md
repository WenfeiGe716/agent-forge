# Team Runtime Design

日期：2026-09-12

实施状态：负责人分工、成员直接通信、回复唤醒、独立 Codex 会话、原生工具/Skill 和现有 HTTP 展示已接线，验收与当前限制见 [Rust 团队运行时验收](VALIDATION_TEAM_RUNTIME.md)。

## 决策

Forge 不需要独立的 workflow、DAG 或 planner 编排器。一次运行由一个负责人和零个或多个成员组成。负责人通过团队工具观察能力、分配工作、收集结果和提交交付候选；成员通过同一组受控工具读取团队上下文、领取工作、发送消息和提交结果。模型负责决定“现在找谁做什么”，Rust 负责身份、权限、状态、幂等和投递事实。

这与 OMX `$team` 的核心思想相近，但不复制它的 tmux、worktree 和 JSON 状态文件运行时。OMX 的任务认领、乐观版本、claim token、mailbox 和事件协议是可借鉴的协作协议；Codex App Server 继续是每个运行会话的执行器，OpenCodex 继续是供应商协议网关。Codex 源码不修改。

## 运行模型

```text
冻结 TeamSnapshot
        |
负责人 Codex Session -- inspect_team / assign_work / read_result
        |                         |
        +-- TaskAssignment -------+-- 成员 Codex Session
        |                         |
        +-- Message / Receipt ----+-- Message / Result
        |
控制平面：SQLite 事务、ACL、审批、事件、outbox、租约
```

### 冻结快照

创建 Run 时复制并冻结团队成员的最小可见配置：

- `worker_id`、显示名、业务角色和职责说明；
- 已确认的 Skill 版本、工具权限档和模型路由版本；
- `is_coordinator`、可通信范围和工作区策略；
- 运行级预算、审批策略和 `TeamSnapshot` 版本摘要。

设计期修改不会改变已开始的 Run。成员可以读取其他成员的公开能力、当前状态和本 Run 的任务摘要；凭据、完整提示词、审批内容和不属于其授权范围的文件不进入能力目录。

## 四个核心接口

团队运行时只需要四类业务接口。当前写入由活动 Codex 会话的动态工具进入同一 Rust 事务实现；HTTP 用户读取公开目录、任务和消息，不能传成员身份执行团队写入。后续 MCP 适配也必须使用相同的受信任宿主接口。

### `inspect_team`

输入：`run_id`、调用者 `task_key`，可选 `member_id`。

输出：冻结成员目录、负责人标识、成员运行状态、当前分配摘要和可调用动作。调用者只能看到允许的字段；负责人可以看到完整目录，普通成员看到公开能力和协作状态。

### `assign_work`

只有负责人能创建正式分工。输入包含 `operation_id`、`recipient_id`、标题、说明、验收标准和可选依赖摘要。Rust 在一个事务中检查：接收者属于快照团队、通信权限有效、预算未超限、操作幂等键未被不同内容使用，然后创建 `TaskAssignment`、事件和 outbox 唤醒记录。

分工不是模型调用。创建分工后，成员是否立即唤醒由运行策略和资源上限决定；唤醒会创建新的成员 `RunSession`，不会重放成员已经完成的回合。

### `claim_task` / `submit_result`

成员领取任务必须携带任务版本和 lease。返回 `claim_token` 与 `attempt_generation`。续租、释放和提交都必须带回这两个值；过期或旧代次一律拒绝。结果是待验收证据，不等于整个 Run 成功。

建议状态：`pending -> running -> submitted`，异常路径使用 `blocked`/`outcome_unknown`，恢复必须由负责人引用证据显式执行。任务状态变化与事件、outbox、回执在同一 SQLite 事务中提交。

### `send_team_message` / `inbox`

消息必须绑定 `run_id`、发送者、接收者和来源 `task_key`，并使用幂等 `operation_id`。发送事务检查成员关系和通信权限，写入 mailbox、事件和 outbox。接收者读取自己的 inbox 后写入 delivery receipt；“已投递”只表示进入接收者上下文，不表示对方同意或任务完成。

需要回复时可以产生一次新的成员任务或唤醒现有会话，但必须保留原消息和新任务的关联。禁止隐式无限自动回复、跨 Run 读取消息或用消息文本授予权限。

## 负责人和成员的职责

负责人不是固定 planner，也不是每条消息的中转人。它拥有本次冻结团队范围内的协调动作：读取能力、分配、转派、请求决策、汇总结果和提交交付候选。成员可以读取公开团队目录，直接向其他成员求助、发送证据或请求回复；正式分工、转派和最终交付仍由负责人确认。

Rust 控制平面必须重新校验每个动作的成员身份、Run 归属、ACL、审批和预算。模型看到某个工具不代表它获得额外文件、网络、安装或凭据权限。

## 会话和工作区

负责人和每个实际成员使用独立的 Codex execution context。当前实现按 `max_parallel` 并行运行不同成员，每名成员同时只运行一个任务。成员使用独立 Git 工作副本，暂停等待回复时保留自己的未集成修改，下次激活同步其他成员已经集成的兼容修改。成员结束后由宿主进行文件级三方集成并记录结果；同文件不同改动不会静默覆盖。负责人协调回合在成员任务处理结束后恢复并审阅集成证据。

运行中的成员消息使用 `turn/steer` 投递；身份、目标回合及送达回执仍由 Rust 决定。回复结果必须有当前回合的完成和工作区处理证据，才可释放等待者，模型提前提交文字结果不足以触发文件依赖方。

会话死亡、断线或租约过期只产生 `outcome_unknown`，不得自动重试可能已经造成外部副作用的操作。负责人核对线程、工作区和工具收据后，才能显式 `reconcile`、`retry` 或 `reassign`。

## 实施顺序

1. 将 `RunConfig.members` 从“可冻结但只执行 `members[0]`”提升为带角色、公开能力和权限摘要的 `TeamSnapshot`。
2. 在 Rust 控制平面增加能力目录读取和成员字段过滤。
3. 将现有 `forge-core` task 命令接到 Rust HTTP/MCP：创建分工、claim、renew、release、submit、结果读取。
4. 在 Rust product store 增加统一 mailbox/message receipt 命令；保留现有 `agent_messages` 表的迁移兼容。
5. 为负责人和成员分别创建 Codex session，并把消息唤醒作为一次新的受控分配。
6. 添加端到端验收：负责人识别两个不同能力成员，分发两个任务；成员互读公开能力、互发消息；双方提交结果；负责人读取并提交交付候选。
7. 已接入独立工作副本与并行文件集成；真实供应商验收状态与尚未开放的恢复能力见验收文档。

## 明确不做

- 不把 `orchestration_templates`、workflow、DAG 或自动规划器作为运行时必要条件；旧字段只保留迁移兼容。
- 不把 OMX 的 tmux pane、worktree 状态文件或 CLI 作为 Forge HTTP 后端的权威状态。
- 不让 Python 和 Rust 同时写同一个 Run 的任务、消息或执行状态。
- 不修改 `third_party/codex`；OpenCodex 只承担协议转换和供应商路由。

## 验收标准

“支持团队协作”只有在以下事实都能从 Rust 事务和事件中复现时才算完成：

- Run 快照包含至少两名成员及其公开能力，且版本冻结；
- 非负责人不能调用正式分工，越权接收者、跨 Run 和旧版本请求被拒绝；
- 相同操作重放返回原回执，不产生第二个任务或第二条消息；
- claim、续租、提交受 token、代次和版本保护；
- 消息有发送、读取和送达回执，回复唤醒不会重放旧会话；
- 成员结果保持“待验收”，负责人可以核对证据后再提交 Run 交付候选；
- Codex 进程、供应商网关和前端兼容测试全部通过，且 `third_party/codex` 无差异。
