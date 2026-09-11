# v0.13 · 人工治理的模型与团队设计中心

核心流程：需求 → 建议草稿 → 人工编辑 → 核对版本与摘要 → 批准发布 → 显式选择或实例化 → 冻结运行。

生成建议、获取目录、运行评测都不会直接为现有 Agent 换模型、安装 Skill、创建成员或启动业务任务。保存草稿也不会让它成为运行配置。管理员可以回看、编辑、发布、撤销和重新采用具体版本。

## 模型资产与官方接入

“供应商与模型”管理凭据、地址、协议和具体型号；“设计中心 → Model Hub”管理评测证据、系统评分和人工画像。一个供应商可以有多个模型，同一组织可以使用多家供应商。

| 接入类型 | 具体协议 | 鉴权与模型目录 |
| --- | --- | --- |
| OpenAI 官方 | Responses、Chat Completions | API Key；`GET /v1/models` |
| Claude / Anthropic 官方 | Messages | API Key；`GET /v1/models` |
| DeepSeek 官方 | OpenAI 兼容 Chat Completions | API Key；`GET /models`；保留工具轮次的 reasoning_content |
| Gemini 官方 | 原生 generateContent | API Key 请求头；分页 `GET /v1beta/models`，筛选支持 generateContent 的条目 |
| 兼容网关 / 私有模型 | 上述兼容协议，或自定义 JSON/HTTP | 加密凭据；可配置目录响应映射 |

预设只填充连接方式；实际型号来自该账号能读取的供应商目录，也允许管理员手填。目录可含已停用、暂不可用、图像或嵌入模型，因此目录发现不等于工具能力验证。网络失败有明确错误，不会伪造目录或偷偷改用别家。

这里接入的是官方模型 API。ChatGPT 产品订阅、Claude Code 的订阅/OAuth 登录与模型 API 计费不是同一种授权；本版没有读取桌面产品凭据或接管 Claude Code CLI 会话。

自定义协议支持声明式 JSON/HTTP 请求模板、消息和工具往返映射、响应 JSON Pointer、参数声明及认证请求头。它不执行管理员上传的 Python/JavaScript，不支持任意网络传输协议、任意 SDK、SSE/双向实时流或任意签名代码。需要这些能力时应增加经过审查的专用适配器。

## 评测、系统评分与人工修正

内置 `forge-core-smoke v1` 最多发出六次模型请求：两条代码理解问答、两条推理问答和两次工具往返。只发送评测文本；模型返回的代码不会在宿主机执行。它是基础可用性评测，不能作为生产代码质量排行榜。管理员可以在 Benchmark 页面编辑企业自己的评测用例，发布一个版本后用该套件评估模型。

| 维度 | 计算规则 |
| --- | --- |
| coding / reasoning / tool | 该维度通过用例数 ÷ 用例数 × 100 |
| reliability | 成功请求数 ÷ 尝试请求数 × 100 |
| latency | `max(0, 100 × (1 − 平均响应毫秒数 / 30000))` |
| cost | 仅在填写输入、输出每百万 token 单价后计算：`max(0,100 − 单价之和 × 2.5)`；应使用相同币种比较 |

没有评测或没有报价显示“未知”，不是零分。评测失败记录已发生的请求，不产生可发布画像。系统分数必须匹配该模型的已完成评测证据；管理员通过 `overrides` 和修正原因表达自己的判断，不能把人工分数伪装成系统评测。

最终分数按维度采用人工覆盖值，其余沿用系统分数。模型或供应商连接版本改变后，原评测标记过期，不继续参与推荐排序。原记录仍可追溯。

## 路由策略与企业规则

推荐器先使用已发布画像及管理员约束排序。没有选择设计助手模型时，这是明确标记的本地规则建议；选择模型并确认调用后，才会发送需求与候选目录给真实 AI。生成结果永远是草稿。

AI 使用仅提交配置的 `propose_design` 结构化工具（兼容完整 JSON 输出）；该工具不能执行任务。输出仍须通过团队/路由结构、成员关系、角色权限和候选模型校验，失败不创建配置、不自动回退为伪装的 AI 结果。

路由编辑器支持规划、开发、测试、审查的独立模型；预算档位、并发、模型调用上限、允许的供应商、外部模型权限和工具要求均可编辑。预算档位是策略参数与推荐偏好，不是货币账单的硬限额。

管理员可将一个已发布策略设为企业规则。它约束所有新任务；任务级覆盖、默认成员绑定和备用模型都不能绕过供应商、外发和调用/并发上限。项目自己的外发授权仍然必须满足。

新任务只读取明确选中的策略版本。模型推荐不会在任务执行中重新选择模型；运行中的模型和连接、参数、团队、Skill、权限及版本依据都保存在快照中。

## 团队、Skill 与办公室

团队设计器支持层级关系、增删成员、姓名、执行角色、职责、部门、主/备用模型、Skill 版本、工具权限、通信范围和办公位置。执行角色对应现有 planner/developer/tester/reviewer；具体专业分工写在名称、部门和职责里。权限只能缩小角色允许的工具集合。

Skill 推荐仅显示候选信息；内容、脚本权限和指定版本仍需通过原 Skill 审核链。团队发布与实例化时都会重新检查引用的 Skill、模型及供应商状态。发现上游 Skill 不会自动下载、批准或绑定。

团队发布保存模板，不创建 Agent。管理员点击“实例化团队”才创建具有独立实例标识的成员。任务选择团队模板时必须选取同一个完整实例；后台从已发布模板重建配置，防止后来手工编辑的成员混入旧模板版本。

办公室设计器提供楼层与平面拖动，也可以输入坐标和区域。布局模板需要发布后显式应用；团队模板的位置随显式实例化写入新成员。3D 办公室按已应用坐标放置工位，可切换查看已配置楼层；楼层采用独立视图，不模拟楼梯/电梯通行。场景位置不授予业务权限，也不产生模型请求。

## 版本、审核与运行隔离

- 每次编辑都追加一个草稿版本，旧版本内容不变；并发编辑通过预期版本检查防止覆盖。
- 发布和撤销要求提交确切版本及 SHA-256 摘要，并记录操作者、时间和审核说明。
- 自动生成来源和生成证据只由服务器写入；人工编辑保留来源依据并标记编辑。
- 可以选择旧的、仍已发布的版本运行；撤销版本不能再用于新任务。
- 普通配置修改不会改变已创建任务；现有的供应商停用、项目外发授权撤销、Skill 撤销仍按安全机制阻止后续调用或加载。
- 评测与 AI 建议支持 `Idempotency-Key`；界面为同一操作保留请求标识，避免重试重复计费。失败或重启中断的调用不会自动重放，不确定的已发生费用需要人工核对。

关键代码集中在 `design_center.py`（设计版本与审批）、`model_hub.py`（证据与建议）、`design_runtime.py`（发布配置到快照）、`providers.py` / `protocols.py`（模型协议）中。设计和运行之间只有明确的发布版本接口，界面动画不参与业务决策。

## 启动与验证

已有本地环境继续运行 `bash start.sh`，访问 `http://127.0.0.1:8787`。账号和加密配置保持在 `.local`，不要提交凭据与数据库。Docker/gVisor 不可用时，模型资产管理、设计和评测仍可使用；实际构建/测试/Skill 脚本执行仍需要沙箱。

```bash
node scripts/build-offline.mjs
node scripts/check-source.mjs
PYTHONPATH=backend .venv/bin/python -m pytest backend/tests -q
node --test tests/visual/*.test.mjs
```

真实供应商联调使用 `scripts/verify-model-hub-live.py`，必须传入 `--paid` 才运行付费评测/建议。它读取现有加密供应商配置，不接受命令行密钥，不发布建议、不绑定成员、不创建业务任务。真实结果存入本地 `.local/live-verification-v013.json`。

`--skip-benchmark` 可复用相同模型版本的已完成评测，仅验证两种建议；`--kind routing` 或 `--kind team` 可限制为一次建议调用。浏览目录不需要 `--paid`。具体已验证结果见 [v0.13 验证报告](VALIDATION_v0.13.md)。

官方协议依据：[OpenAI Function calling](https://developers.openai.com/api/docs/guides/function-calling)、[OpenAI 模型目录](https://developers.openai.com/api/reference/resources/models/methods/list)、[Anthropic API](https://platform.claude.com/docs/en/api/overview)、[Claude Code 鉴权](https://code.claude.com/docs/en/authentication)、[DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)、[Gemini Function calling](https://ai.google.dev/gemini-api/docs/function-calling)、[Gemini 模型目录](https://ai.google.dev/api/models)。本版以受控协议传输测试验证官方适配器；没有各厂商账号的地方不声称完成真实付费账户验收。
