> v0.1设计/历史说明。v0.2新增本地日志编排器、离线前端与Agent消息；当前实际能力和验证边界以README及VERIFICATION_v0.2.md为准。未勾选项目不代表已实现或已通过。

# 配置契约 / v0.1

## 1. 配置层级

逐字段覆盖：平台默认 < 项目 < 角色 < Agent实例 < 单次运行。删除字段表示恢复继承，不接受 `null` 代替继承。界面提供最终值与来源预览。供应商凭据独立保管，普通配置导出只保存引用。

运行创建时冻结项目规范、基础提交、Agent版本、模型配置版本、连接地址、凭据版本引用、运行参数及其来源。后续编辑影响新运行，不在途替换。停用供应商/撤销外发授权属于安全撤销，会在下一模型调用前校验。不能撤销已经发送的请求。

本版个体角色提示词、模型参数和供应商连接由管理员配置；普通开发者可在自己的项目任务中覆盖运行参数，但受 schema 和安装资源上限约束。尚未实现管理员可配置的多级组织配额策略编辑器。

## 2. 运行字段

| 字段 | 默认 | 范围 | 作用域 |
|---|---:|---|---|
| max_steps | 24 | 1–500 | 每个Agent子任务最大模型轮数 |
| max_model_calls | 100 | 1–10000 | 整次运行累计模型请求上限，含备用尝试 |
| task_timeout_s | 3600 | 30–86400 | 自动执行时限；人类等待不计；Agent可另设个体时限 |
| call_timeout_s | 120 | 5–1800 | 单次模型HTTP调用超时 |
| command_timeout_s | 120 | 1–3600 | 单条沙箱命令时限 |
| max_parallel | 3 | 1–32 | 同一运行的任务轮次并行度 |
| max_tasks | 20 | 1–100 | 计划子任务数 |
| max_rework_rounds | 2 | 0–10 | 测试/审查失败后的自动返工轮数 |
| cpu | 1 | 0.1–32 | 单个代码沙箱CPU限额 |
| memory_mb | 1024 | 128–65536 | 单个代码沙箱内存限制 |
| max_file_bytes | 200000 | 1024–5000000 | 单次模型文件读写大小 |
| max_context_chars | 120000 | 4096–1000000 | 发给模型的上下文字符上限，不等同模型Token窗口 |

负责人角色初始 `max_steps=8`，审查角色为12，其余继承平台。平台界面可修改角色覆盖。整次运行字段 `max_model_calls/max_parallel/max_tasks/max_rework_rounds` 不允许放进 Agent/角色覆盖，避免“配置已保存但不生效”。

时限在执行边界检查，单条已发出的调用可能到自身超时才结束；不是精确到秒的强制总墙钟终止。Worker全局活动并发与broker沙箱数另外受安装环境控制。当前没有顶层运行总数为2的准入器。

## 3. 模型与供应商

供应商连接：`name, protocol, base_url, api_key, external, enabled`。只有 `openai_chat` 和 `anthropic_messages` 两个原生协议适配器。API Key留空可支持本地无鉴权服务；更新不带字段表示保留，显式空字符串表示清除。

模型配置：`name, provider_id, model_id, allowed_parameters, parameters, supports_tools`。模型ID由用户填；没有固定白名单。Agent执行要求模型支持工具调用。

OpenAI兼容适配器参数：`temperature, top_p, max_tokens, max_completion_tokens, reasoning_effort, presence_penalty, frequency_penalty, seed, stop`。`max_tokens` 与 `max_completion_tokens` 不能同时设置。

Anthropic适配器参数：`temperature, top_p, top_k, max_tokens, stop_sequences, thinking, output_config`，必须填写 `max_tokens`。具体型号是否支持某个字段以该供应商说明与实际验证为准。

这些列表是**当前代码已实现的协议字段**，不是所有模型均支持的保证。UI声明的字段、适配器字段和实际模型能力必须一致；无法仅靠管理员声明证明模型能力。不支持的键报错，不静默丢弃；受保护字段如 `messages/tools/model/base_url/headers` 不允许通过参数覆盖。复杂推理参数的完整供应商侧语义仍需实测。

Agent：`name, role, model_profile_id, fallback_model_ids, instructions, runtime, model_parameters`。主模型与每个备用模型分别校验参数和代码外发授权。当前使用已列出的备用模型顺序，仅在连接失败或HTTP429时尝试；读超时结果不明，不自动重试。不同协议间的参数不保证可直接复用。

价格信息/金额硬预算、供应商代理配置、自动模型发现、每任务临时替换模型、按字段在线热修改、可视化任意重试策略、流式token展示均不属于v0.1已交付能力。

## 4. 项目

`name, import_path, instructions, sandbox_image, check_commands, allow_external_models, runtime, members`。

`import_path` 相对于安装的 IMPORT_ROOT；当前仅导入已有本地目录，不主动拉取远程仓库。API无法任意浏览宿主机。导入复制文件快照并创建独立Git基线；不复制原始Git历史、远程凭据和钩子。重要任务应记录原仓库提交并由人工将补丁接回。

没有测试命令时报告为 `unverified`。独立审查通过后允许进入人工验收，但用户必须填写验收说明。测试失败或审查失败达到返工上限后是 `needs_attention`，不是成功。

## 5. 安装配置

`.env`中的`HOST_UID/HOST_GID, FORGE_DATA_DIR, FORGE_IMPORT_DIR`决定宿主机路径和文件所有权；路径必须是绝对路径。不要移动data目录后只改容器路径，broker启动的兄弟容器需要宿主机真实路径。

`SANDBOX_RUNTIME=runsc`、`SANDBOX_IMAGES`镜像白名单、`MAX_SANDBOXES=4`、`MAX_ACTIVE_ACTIVITIES=4`、`MAX_PROJECT_MB=200`属于安装级配置。普通Agent不可修改；改动后需重建对应服务。

`ENCRYPTION_KEY`、数据库密码、`SANDBOX_TOKEN`不得出现在项目指令、普通配置JSON、Git仓库或工单中。`ADMIN_PASSWORD`只用于初次初始化账号，不是每次启动都重设密码；当前无密码自助找回与管理员密码轮换界面。


## v0.6 目标编排配置

```json
{
  "mode": "discussion",
  "participants": ["agent-A", "agent-B"],
  "strategy": "sequential",
  "stop_when": "goal_satisfied",
  "prompt": "形成可执行方案并解决已知分歧",
  "criteria": [
    {"id": "solution", "description": "给出可执行方案并解释取舍", "evidence_type": "decision"},
    {"id": "source", "description": "结合实际源文件定位问题", "evidence_type": "code_reference"}
  ],
  "assessor_id": "agent-B",
  "stagnation_checks": 3
}
```

请用实际Agent ID替换示例。`stagnation_checks`是连续无有效进展的保护阈值，不是讨论总轮数；预算或时限耗尽只会转人工，不能计为成功。旧`rounds/min_rounds/stop_when`设置经过兼容解析后转为目标模式，使用前请在编辑器核对目标并保存版本。

城市开关、密度、暂停和视角是客户端场景配置，不增加模型调用，不修改业务运行状态。城市循环固定步长，未做跨客户端时钟同步。
