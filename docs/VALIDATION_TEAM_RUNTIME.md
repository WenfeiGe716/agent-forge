# Rust 团队运行时验收

日期：2026-09-12。

## 已实现的执行链

Rust 冻结负责人和成员的职责、模型路由、工具权限、通信范围及已审核 Skill 版本。负责人通过 Codex 动态工具读取能力目录、分发工作、让出回合并审阅成果；成员直接读取团队能力、发送消息或请求一次有明确范围的回复，提交自己的工作记录。只有原始协调任务可提名整次运行的交付候选。

每个成员使用独立 Codex 进程、运行目录和 thread。成员的新分配恢复其原 thread，任务通过新的 claim token 和 generation 隔离；任务、消息、事件、唤醒和幂等回执由 Rust 产品事务维护。任务的 connection/thread/turn 绑定独立保存，HTTP 会话不会混入其他成员或其他任务的输出。

不同成员按冻结的 `max_parallel` 上限并行执行，同一成员的新任务依次执行并恢复原 thread。每个成员使用独立 Git 工作副本；成员停止后，宿主根据基线、成员结果和当前集成目录做文件级三方比较。不同文件的改动可合并，同一文件的不同改动保守报冲突并保留现场。缓存与敏感配置遵循项目导入的排除规则，集成不移动最终工作区的 Git 基线。

成员显式 `waiting` 会冻结它正在等待的回复；宿主确认回复回合结束并完成工作区处理后，只唤醒一次原任务。运行中的消息通过原版 Codex `turn/steer` 送入准确的 thread/turn，成功后才确认消息与 outbox；没有活动回合的消息保留到下次启动。`request_reply=true` 创建有预算限制的独立回复任务，同一成员不会同时处理两个回合。

原版 Codex 在 `thread/resume` 时会补发上一回合的 token 用量通知。真实二进制回复测试发现，原先严格匹配当前 turn 的处理会因此提前停止回复任务。现在仅把宿主已知上一回合、同一 thread、无 callback 的 `thread/tokenUsage/updated` 单独保存为历史用量；历史通知不能成为当前完成证据，其他错线程、错回合事件仍拒绝。

## Codex 工具和 Skill

- 使用未修改 Codex 的 `thread/start.dynamicTools`、`item/tool/call` 和原生工具输出协议；模型不能提供自己的成员身份或 claim token。
- 文件和命令操作由 Codex 原生工具执行，受冻结 sandbox 和原生审批约束；团队动作在 Rust 中逐次授权。
- Skill revision 必须已审核且 digest 匹配。正文和资源复制到成员独立的 `CODEX_HOME/skills`，原位置被链接重定向或已物化内容发生变化时拒绝启动。
- `always` 绑定通过原生 `turn/start` Skill 输入加载；`on_demand` 通过 Codex Skill 目录和开发者上下文提供。模式、参数、脚本授权和实际路径保持冻结。
- 原生 `request_user_input` 映射为现有 `decision_requests`，通过现有 HTTP 问题回答接口回传；回答发送前重新检查用户和项目权限，发送成功后才确认对应 outbox。结束或断开的回调不会转投到另一个会话。
- `agents.enabled=false`，Forge 的团队目录决定可执行成员；不修改 Codex 自身的成员实现。

旧 Forge 文件工具名称与 Codex 原生工具不是严格一一对应：Codex 可以通过 shell 读取文件。当前严格边界是 sandbox 能力类别、原生审批和动态团队工具授权，不能把旧工具清单理解为逐个原生命令的禁止列表。

## 验证

并行阶段原版 Codex 验收：3 个 Codex thread，26 次本地 Responses 请求，3 次原生 Skill 读取，2 条成员消息全部送达，两个成员在并发屏障上同时存活，各自原生命令写入的文件成功集成；原生问答 HTTP 回传、负责人审阅和最终测试验收通过。脱敏结果见 [rust-team-parallel-run.json](evidence/rust-team-parallel-run.json)。这仍是本地确定性供应商验收，不能替代真实云端模型测试。

追加 `--reply` 原版 Codex 验收通过：3 个 thread、5 次独立进程连接、34 次本地 Responses 请求、3 次原生 Skill 读取、3 条消息全部送达。成员请求一次回复并提交 `waiting`；回复成员结束原分配后恢复同一 thread 处理回复，等待成员也恢复自己的 thread，保留并集成实际文件，负责人最后审阅与验收。结果见 [rust-team-parallel-reply.json](evidence/rust-team-parallel-reply.json)。

Rust workspace 全量回归 192 项通过，0 失败，2 项可选真实二进制 smoke 默认忽略；之后对上面的恢复修复补跑团队 HTTP 回归。全 workspace/all-targets Clippy 通过，前端 61 个 TS/TSX 文件语法检查通过，Codex 源码无改动。

`rust/crates/forge-server/tests/team_run.rs` 覆盖：三成员任务分发、操作重放幂等、实际 Skill 文件、成员直接通信及送达、成员结果与负责人提名、HTTP 任务/会话隔离、原生问题的 HTTP 回答回传、等待原生审批时取消，以及当前回合的完成证据才能放行下一回合。

`scripts/verify-rust-team-run.py` 使用项目内真实 Codex 二进制和本地 Responses 服务。它检查原生 Skill 已被 Codex 选中，并让每个成员通过原生工具读取 Skill 正文和参考文件，然后完成团队通信、结果审阅和 HTTP 验收。原生人工问答也由本地供应商发起，经 HTTP 回答后返回同一个 Codex 工具调用。没有使用付费供应商；这证明执行和协议链路，不能据此判断云端模型的协作质量。

## 智蔻真实调用状态

读取了启用的智蔻连接，并核对桌面端与 Rust 产品库使用同一份保存的连接。实际调用 `bailian/glm-5.2` 和 `gpt-5.6-terra:qlyj` 的 `/v1/responses`，均返回 HTTP 401；`/v1/models` 同样返回 401，错误分类为 `invalid_request_error`。服务端未提供足以区分凭据失效、账号策略或网关拒绝的原因，不能仅凭 401 断定具体原因，也没有确认任何可用模型。

`scripts/probe-configured-provider.py` 验证真实流式函数调用与工具结果续接；`scripts/verify-rust-team-live.py` 必须先通过该检查，才启动隔离 Rust 产品库中的真实团队验收。后续验收要求两个成员实现独立模块、一次有回复的成员求助、回合时间重叠、独立目录、原生 Skill/命令证据、原始 8 项契约测试及产物一致性。目前脚本在真实供应商检查阶段失败，**没有真实云端团队运行成功的结果**，后续断言仍待连接可用后实测。

```sh
.venv/bin/python scripts/verify-rust-team-live.py \
  --model 'gpt-5.6-terra:qlyj' \
  --output .local/validation/new-zhikou-team-live
```

连接检查脱敏证据见 [rust-team-zhikou-live.json](evidence/rust-team-zhikou-live.json)。测试不修改既有模型启用状态或 Agent 绑定，不使用其他供应商替代智蔻。

```sh
cargo test --manifest-path rust/Cargo.toml --locked
cargo clippy --manifest-path rust/Cargo.toml --locked --workspace --all-targets -- -D warnings
cargo build --manifest-path rust/Cargo.toml --locked -p forge-server --bin forge-server
.venv/bin/python scripts/verify-rust-team-run.py --output .local/validation/new-team-run
.venv/bin/python scripts/verify-rust-team-run.py --reply --output .local/validation/new-team-reply-run
git -C third_party/codex diff --exit-code
```

`--output` 必须是新目录；包含运行数据库的完整证据留在 `.local/validation`，仓库只保存不含凭据的验收摘要。

## 当前边界

- 前端组件本轮未重构，继续使用原有 HTTP/SSE 和任务、消息、审批展示契约。
- 团队暂停/人工核对恢复、交付退回后原团队重开尚未开放。团队运行支持取消；未知结果保留现场，不自动重试。工作副本是协作隔离；操作系统权限仍由 Codex sandbox 决定，full 模式不能视为恶意成员之间的安全边界。
- `max_model_calls` 当前对团队回合启动做事务预留，并非对一次回合内的每次上游 HTTP 请求计费；真实验收同时使用运行超时，不能把回合上限宣称为精确供应商请求预算。
- 容器成员执行、自动故障转移和通用工作流不是本阶段实现。历史 workflow/template 字段仅为迁移兼容。
- Python 仍供历史数据导出和回滚使用；新团队运行由 Rust 唯一执行。旧 Python 桌面入口已在此前切换阶段删除，本轮没有删除历史产品数据或更改个人 Codex 配置。
