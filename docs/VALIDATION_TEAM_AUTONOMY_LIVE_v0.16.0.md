# v0.16.0 Mac 客户端真实模型与团队执行验收

日期：2026-09-11（Asia/Shanghai）。本次使用正在运行的 Mac Agent Forge 0.16.0、已有供应商和开发团队，经原生界面创建项目并发起任务；不是模型替身、预录回放或仅运行平台单元测试。

## 结论

真实执行及产物核验通过，当前任务状态为 `awaiting_acceptance`（待人工验收），不是失败或需修复。没有替用户批准最终成果，也没有将数据库改成已完成。

- 供应商／模型：智蔻／`gpt-5.6-terra:qlyj`，28 次模型调用，28 次响应完成。
- 模型记录用量：519,091 input tokens、7,565 output tokens；这是本次实际记录，不等于已核对供应商账单。
- Nova 与 Echo 分别通过真实本机命令运行同一份 9 项契约测试，均退出 0。
- 验收者审读生成代码后，在最终集成目录独立复跑原测试，仍为 9/9 通过。不是 27 个不同测试。
- 负责人自主选择 Nova 开发、Echo 验证，读取实际成果后提交交付候选；没有预设角色流程或强制全员讨论。
- 产出需求文档、实现代码和验证文档，工作交接、模型响应、命令回执、文件摘要及导出包相互对应。

## 项目与边界

| 项目 | 值 |
| --- | --- |
| 项目名称 | 团队自治真实验收 JQ9sUL |
| 项目 ID | `582d619a87e94463beb6f6056d95b0b7` |
| 运行 ID | `bd0d9e9453758ab08bdc4dc5a275db34` |
| 时间 | 03:33:16 创建，03:37:22 生成交付候选，约 4 分 6 秒 |
| 团队 | 研发部／开发团队；Atlas 为负责人，授权池含 Nova、Echo、Sage |
| 执行位置 | 本机独立项目任务工作副本 |
| 权限 | 请求审批；公开测试项目允许向现有模型发送内容，本机命令回执为 Seatbelt、network denied |
| 预算上限 | 60 次模型调用、8 个工作分配、2 个并行任务；本次实际 5 个工作分配 |
| 固定流程／固定项目检查 | 未启用；测试命令写在任务验收要求中，由 Agent 自己实际调用 |

源码目录：`/Users/gewenfei/Documents/AgentForge-live-JQ9sUL`。

这是新建的公开、标准库标签规范化测试项目，不包含业务代码或凭据。原始函数仅抛出 `NotImplementedError`；发起任务前实际运行 unittest，9 项测试失败（包含子测试，报告 12 个 error），退出 1。Agent 需要完成字符串类型检查、英文／中文逗号及 Unicode 空白分隔、casefold、稳定去重等契约。

验收者只准备 README、失败桩与固定测试，没有替 Agent 完成实现或交付文档。原始导入目录的实现至核验时仍为失败桩，没有回写用户源码。既有项目、团队、供应商、权限选择未改动；当前新运行记录未删除。

## 实际协作与执行证据

1. Atlas 通过 `inspect_team` 查看冻结团队能力，读取项目后委派 Nova 实现代码及需求文档，然后主动让出执行等待消息。
2. Nova 使用实际文件工具写入 `tag_normalizer.py`、`docs/requirements.md`，运行 `python3 -m unittest discover -s tests -v`，9/9 通过。
3. Nova 主动发送具体交接通知；平台另发结果通知，两者实际进入 Atlas 后续模型请求。Atlas 再委派 Echo，不重放初始工作。
4. Echo 用 `read_work_result` 读取 Nova 的两个交付文件，核对其集成提交与 SHA-256；自己重新运行相同原测试，9/9 通过，并写入 `docs/verification.md`。
5. Echo 的主动通知及平台结果通知进入 Atlas 新一轮模型请求。Atlas 用 `read_work_result` 读取全部三份最终交付物并核对，主动提交 `ready`。
6. 平台生成成果清单、差异与源码包，进入待人工验收。没有活动执行或未决工具／安装审批；尚有最终成果验收需要用户决定。

消息共 2 条成员主动通知和 2 条平台结果通知，不把自动通知计作 4 次成员主动求助。本次没有同事 `request_reply=true` 求助、会议或 Sage 工作，不宣称这些路径也完成了真实验收。

原生命令证据均包含开始事件、完成事件、持久化 native 执行行、工具返回；退出 0、未超时、输出未截断且报告 9 项测试：

| 执行者 | 工作 ID | 本机执行 ID | 事件 |
| --- | --- | --- | --- |
| Nova | `auto_15606fa21e52be4bcd5e1088` | `d444e8c355f5ff8a55029d4c8f1ec1596e1fe69aaa6e442c6957e325972c7c5f` | 455／456／457 |
| Echo | `auto_793c14fd50d3c50ab8b116c3` | `b4356e348137a530bfd29830090e770205be47eea608a74aa12a388ff2d02ad2` | 521／522／523 |

验收者在审读实现和测试后独立执行：

```sh
cd '/Users/gewenfei/Library/Application Support/Agent Forge/workspace/data/runs/bd0d9e9453758ab08bdc4dc5a275db34/integration'
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

真实输出：`Ran 9 tests in 0.001s`、`OK`，退出 0。此复跑不属于上列 Agent 的本机执行回执，也没有改变其历史记录。

## 未隐藏的实际错误

- 测试源码最初放在仓库 `.local` 下，原生导入触发隐藏目录保护并返回 422。按故障诊断核查后，只将新建的公开测试源移到普通 Documents 目录，导入成功；没有放宽保护或修改业务目录。
- Nova 最初在 shell 使用环境中不存在的 `apply_patch`，本机退出 127。模型实际收到错误后，自行改用已授权 `write_file` 完成实现。没有人工安装命令、改写模型回应或替它补代码。
- Echo 尝试通过 `delete_file` 清理两个 `__pycache__` 文件，事件 559／561 被平台缓存路径规则拒绝，没有发生删除或新增 native 执行。模型接受该限制并完成文档；缓存不在最终导出包中。这不是执行权限损坏，也没有扩大访问范围。

## 文件与导出一致性

固定测试 SHA-256：`1c98c856a6d6d24709a0e4e54a144437464bcbefba191b2c2665e391c46dcccd`。原始源、5 个实际工作副本、最终集成和导出包一致；没有删除、跳过或弱化固定测试。

| 实际交付 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `docs/requirements.md` | 740 | `12a6f4735bdb7eba595414d1a9c13120cf1962c1a544cb12c17a39d59a30f9b2` |
| `tag_normalizer.py` | 711 | `255d60ded9a717ec7157d762b4038b34f753e326d92504bbe5ff4934b53a4979` |
| `docs/verification.md` | 3208 | `c7a97c6a659ea8178b2e4eca93c2a6c1a147a088c7827aea447a1899d68cdf3e` |

负责人读取回执、最终 integration、report.json 与 source.zip 的三份交付文件内容和摘要一致。

- 最终候选摘要：`820956c5a8d8f8d5c62d384498d47edbef1e32a6de401dfdc6b3c27d616ff9d7`，与 `SHA256(changes.patch + report.json)` 一致。
- `source.zip` SHA-256：`0658c808782e6a78fd100ef274150e0e081bf11c85c67f046991fc1641fa62d6`；另行核对包内内容，不把它误称为上述候选摘要的组成部分。
- 成果目录：`/Users/gewenfei/Library/Application Support/Agent Forge/workspace/data/runs/bd0d9e9453758ab08bdc4dc5a275db34/artifacts`。
- `report.json` 的固定项目检查为 `not_requested`，独立 reviewer 流程也为 `not_requested`；本次真实测试证据来自 Nova／Echo 的命令及成员交付，不伪造固定流程通过。

## 可复核方式与限制

新增只读脚本 `scripts/verify-autonomy-live.py`，仅读取指定运行的 SQLite、文件、执行回执与导出包，不调用模型、不执行生成代码、不批准、不写应用数据、不打印凭据。它不是实时执行的替代品，而是核对上面已经发生的真实执行：

```sh
python3 scripts/verify-autonomy-live.py \
  '/Users/gewenfei/Library/Application Support/Agent Forge/workspace/forge.db' \
  bd0d9e9453758ab08bdc4dc5a275db34 \
  /Users/gewenfei/Documents/AgentForge-live-JQ9sUL \
  --expected-contract-sha256 1c98c856a6d6d24709a0e4e54a144437464bcbefba191b2c2665e391c46dcccd
```

本次返回 `technical_evidence_ready=true`，退出 2（waiting）；唯一未完成项是实际用户验收与最终 `completed`，不能报告成全流程已批准。原生客户端也已确认显示“成果已就绪，等待你的验收”。

本次实际调用使用生产 provider HTTP 适配器和用户已配置模型，未注入模型替身；本地响应、用量和执行回执不等于独立供应商签名证明，当前未持久化上游 response ID。当前文件一致性也不是每一历史瞬间的不可变证明。

这是一个受控小项目的真实端到端执行，不外推为复杂大项目、并行多运行、成员主动求助、会议室冲突、依赖联网安装或全部权限模式均已完成真实测试；这些场景未在本次触发。没有部署或回写原始导入目录。
