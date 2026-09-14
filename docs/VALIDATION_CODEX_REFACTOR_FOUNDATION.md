# Codex-first 基础改造记录

日期：2026-09-12。阶段 0 已完成清理，阶段 1 已完成 Codex-first 运行时基础与正式入口骨架，阶段 2 已加入同一 Run 内负责人恢复切片；生产默认切换仍需真实供应商回合与桌面联调验收。

## 已完成

- 通过 git submodule 引入 Codex 与 OpenCodex 源码，commit 见 `third_party/UPSTREAMS.lock`。
- 添加 stdio JSON-RPC 运行时桥接基础、通知标识提取和只读状态接口。
- 补齐初始化通知、服务端审批请求 ID 和响应、防重复响应、连接退出通知、取消清理，以及同线程恢复/追加输入/历史分页接口。审批决策仍由控制层作出，桥接层不会自动批准。
- 修正 `thread/start` 的推理强度字段：通过 `config.model_reasoning_effort` 传入，而非上游未声明的 `reasoningEffort`。
- 默认只查找 Agent Forge 专用构建产物，不回退到全局 `codex`/`opencodex`。
- 独立 `CODEX_HOME` 必填，拒绝个人 `.codex` 及其父目录；不继承个人模型凭据与 Codex 环境配置。
- 子进程独立 HOME/XDG，强制文件凭据存储；初始化返回的目录必须与请求目录一致，不继承个人账户或 Skill。
- 构建脚本检查源码 commit、使用上游声明的 Rust 版本和 `--locked`；状态接口按 `FORGE_CODEX_RUNTIME` 明确报告阶段与是否启用，二进制存在不等于任务已接入。

## 已执行验证与限制

- 在独立临时 `CODEX_HOME` 下，使用本机已有二进制验证过真实 `initialize` 与 `thread/start` 协议；没有发送模型请求。这是协议探测，不是项目内源码构建验收。
- 在用户明确隔离边界之前，曾用全局 Codex 做过一次线程探测，返回会话路径位于个人 `.codex/sessions`。之后不再使用个人配置，也没有擅自清理该会话。不能把早期探测描述为完全未触碰个人目录。
- 新增安全约束前的后端全量回归：1027 passed、1 skipped、2 subtests passed；桌面回归：21 passed。安全约束变更后，桥接与桌面后端定向回归：33 passed、1 skipped；跳过项是尚无项目内源码构建产物的真实 App Server 测试。
- 首次源码构建发现默认 Rust 1.91.1 不满足上游要求。已安装上游声明的 Rust 1.95.0，未切换全局默认工具链；项目内 release 构建已完成，产物安装在 `.local/runtimes/codex/bin/codex`。构建脚本随后复用缓存成功，并显式锁定 `RUSTUP_TOOLCHAIN`。
- 最新桥接/桌面后端定向回归：36 passed；补充了独立 HOME、配置/凭据软链接越界、启动前重新校验，以及三档 Forge 权限到 Codex `thread/start` 的映射。
- 当前后端全量回归：1113 passed、1 skipped、2 subtests passed（约 130 秒）；跳过项是环境条件不足的真实 App Server 探针。
- 当前桌面单元回归：21 passed；`git diff --check`、两个上游子模块工作树检查和构建脚本语法检查均通过。
- 构建完成后的真实 Codex 探针：独立运行目录初始化、`account/read` 为空、`thread/start` 成功；桥接与桌面定向回归为 44 passed。探针未发起模型请求。
- 阶段 1 单任务 POC 已真实启动 `turn/start` 并请求智蔻 `gpt-5.6-sol:qly`；上游 `/v1/responses` 返回 403，Codex 重试 5 次后以 `turn.status=failed` 结束。已修正执行器，不再把外层 `turn/completed` 误归类为成功，并保留结构化错误列表。此次未执行本地命令，因为模型没有返回工具调用；上游 `/v1/models` 同轮也曾返回 500。
- 阶段 1 契约回归当前为 19 passed（Codex task、bridge、POC API、RuntimeEventRecorder、runtime cleanup）；全量 `backend/tests` 通过 1060 passed、1 skipped、2 subtests。新增事件记录器会把 Codex 事件持久化到 Forge Event/SSE，失败与中断均不会被误报为成功。
- 阶段 0 清理已对桌面实际数据执行：删除 1 个旧 Run、1 个 Task、18 个运行事件、4 个活动记录、1 个 Outbox、3 个执行 Step 及 `data/runs` 下 107 项文件/目录；复核 dry-run 为 0，Provider 与其他配置资产仍保留，未触碰个人 `~/.codex`。
- 新增默认关闭的 `POST /api/runs/{run_id}/codex-start`：只读取冻结的负责人主模型，将流式 RuntimeEvent 写回同一 Run，并将终态映射为 completed/paused/needs_attention。
- 新增默认关闭的 `FORGE_CODEX_DEFAULT_SCHEDULER`：仅对开关开启后创建的新 Run 在不可变快照中写入 `execution_engine=codex`，本地调度器随后启动真实 CodexSingleTask；旧 Run 缺省为 `legacy`，不会因重启或开关变化被切换。Codex 运行创建单一可见 Task，并将审批、完成、中断和失败状态及 runtime 事件写入同一 Run。
- 阶段 2 同一 Run 恢复切片：`retry/reassign/continue` 保留失败源 Task 与证据，在原 Run 内创建恢复 activation；转派只允许冻结团队成员，重复请求幂等，恢复任务可被调度器继续执行。
- 阶段 6 实时视图补强：前端将持久化的 `runtime.*` Codex 通知投影为模型增量、工具调用和失败状态，保留原始协议事件；任务页仍通过同一 Run 的 SSE 游标实时回放。补充了“回合 completed 但内部 status=failed”不误报成功的回归；前端测试 428 项通过，离线构建与源码检查均通过。
- 阶段 3 负责人工具桥接：新增任务范围内的 `forge_team` MCP stdio 服务，支持能力目录、分工、成员消息、成果读取、失败恢复、用户决策和按需会议；Mac 打包后端可用专用参数承载该子进程。默认开关 `FORGE_CODEX_TEAM_MCP=false`，开启前仍需真实供应商回合验收。
- 成员执行第一切片：负责人通过 `assign_work` 产生的同一 Run 成员任务，会按冻结模型与权限启动独立 Codex Thread、独立 `CODEX_HOME` 和独立工作副本；成员事件带有 `{run_id}:{task_key}` 任务标识，避免并行任务串流。成员必须通过任务范围内 MCP 的 `submit_result` 提交真实成果；`done` 复用既有集成检查，冲突转工作问题，`blocked/waiting` 保留为可恢复边界，不会把模型文本自动冒充完成。
- Codex 运行时审批第一切片：命令、文件变更和权限请求均绑定 `run × member × thread × turn × item`，HTTP 决策只进入持久化队列，再由原连接单次送回；重复、过期、断线和不确定送达均有明确状态，不会把计划审批或永久授权混入其中。连接结束后未解决的请求会将原任务置为可继续的 `paused`，不会永久停在“等待审批”。
- 本轮回归：`backend/tests` 为 1113 passed、1 skipped、2 subtests；前端为 428 passed；Mac Apple Silicon 桌面包重新构建成功。打包后的 `forge-desktop-backend --forge-team-mcp` 已实际完成 `initialize` 与 `tools/list` 协议验证。
- OpenCodex 2.51.0 真实进程冒烟已通过：`/healthz` 返回该进程身份，未鉴权 `/v1/models` 返回 401。使用临时配置、独立端口，操作系统禁止测试进程读写个人 `.codex`/`.opencodex`/`.agents`。没有模型请求，测试进程已停止。
- OpenCodex 路由适配器定向测试已通过：推理固定走 `/v1/responses`，模型强制使用 `provider/model`，鉴权使用 `x-opencodex-api-key`，错误保留状态码、类型、上游 request-id 并脱敏令牌；管理目录 `/api/providers` 与 `/api/models` 不与推理目录混用。
- Codex→OpenCodex→供应商真实协议链路已通过：`scripts/verify-opencodex-codex-route.py` 启动项目内 Codex 二进制、固定版本 OpenCodex 网关和一次性本机模拟供应商，验证独立网关令牌、上游供应商令牌、`provider/model` 路由、`/v1/responses` 路径及上游原生模型名；该测试不访问外网或用户已有配置。
- 最新独立运行时验证脚本已通过：Codex `initialize`/`account/read`/`thread/start` 返回项目专用 runtime home，未继承个人账户；OpenCodex 健康检查与未鉴权目录拒绝均通过，未发送模型请求。
- 真实网关测试发现并修复两项接入差异：无效配置会被上游回退为默认配置（现启动前使用完整上游 schema 拒绝）；上游 loopback 会跳过令牌鉴权（现使用独立构建补丁修正）。原始 submodule 未修改，补丁与版本摘要匹配后才允许启动。

## 尚未完成

- 从固定源码构建并打包两个专用运行时（Codex 二进制已构建；OpenCodex 当前是独立源码网关构建副本，尚未封装成桌面资源）。
- OpenCodex 网关已完成独立入口、健康/拒绝鉴权冒烟和本机模拟供应商转发；仍待接入实际 Forge Provider/Model 配置同步、真实供应商矩阵和桌面资源打包。其归属探测适配使用上游标注为 test-only 的回调，升级时必须重新审核。
- 负责人 Codex Thread 的自动分析/恢复、桌面端针对控制的完整联调仍待后续阶段；当前已支持冻结配置复用、同线程 resume、活动 turn interrupt 和追加指令 steer，默认调度开关仍关闭，POC/调度器均已具备真实入口，但仍需一个可用供应商回合才能验收本地工具调用。

这些项目完成前，不将新运行时设置为生产默认，不声称真实任务端到端已经通过。
