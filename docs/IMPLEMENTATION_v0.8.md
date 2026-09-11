# Agent Forge v0.8.0 — 正式入口与供应商一步接入

日期：2026-09-09。基于已交付v0.7源码修改。目标是让正式实例首次业务配置集中到供应商，而不是让用户操作离线演示按钮。

## 实际修改

1. `App.tsx`删除演示控制及全局演示注入依赖。默认构建排除`Preview.tsx`、`SuitePreview.tsx`、`previewV07.ts`，不再生成可被正常服务访问的预览页面。开发者专用预览需显式构建，控件收进折叠区并保留模式标识。
2. 新增真实`/api/setup/status`、`/api/setup/models`、`/api/setup/connect`、`/api/setup/environment`接口及供应商接入页面。无需逐个创建模型、绑定默认成员或导入起步Skill。
3. 同页配置供应商协议、地址、Key、模型及高级参数。目录获取是可选的一次有界请求，不固定型号、不自动挑选目录第一项、不把“目录存在”当成模型可执行。
4. 点击连接会发起两次真实模型请求：调用只返回nonce的校验工具，并传回工具结果获取确认。无项目内容、无文件访问、无代码执行。通过后，在一次数据库事务中保存供应商、模型、默认团队绑定、内置Skill及检查记录。
5. 仅给未绑定模型的启用成员配置新模型；已有模型、参数和非空Skill绑定保持。相同成功请求幂等，失败或未确认请求不自动重复付费调用。外部供应商禁止明文HTTP；内网按既有规则检查目标解析结果。
6. 三份内置Skill按固定文件摘要安装，仅接受脚本缺省关闭的本项目文本包，按职责绑定。现有同名未批准、不同内容或撤销/停用包保留原状态，不被自动批准。
7. 新增`start.sh`。缺少虚拟环境时自动离线安装；首次启动自动创建本地配置及密钥，终端显示一次管理员密码。已有配置保留。若Docker/runsc可用，可检查/构建默认执行镜像并自动启动broker；不会安装系统运行时、不会权限提升、不会改成宿主机任意执行。
8. 新增鉴权broker `/readiness`，区分服务可达与运行时/镜像实际就绪。缺少执行环境不冒充可构建测试。
9. 主README仅说明启动与业务使用。备份、在途任务和升级说明收在管理员文档，不渲染到办公室。

## 本轮验证

| 范围 | 本轮实际结果 | 证据 |
|---|---|---|
| 新虚拟环境离线安装 | 27个随包wheel安装成功，pip check通过 | `evidence-v08/offline-install.txt`、`pip-check.txt` |
| 默认前端构建 | 24个生产源文件，0语法错误，无演示入口 | `frontend-build.txt` |
| 全部前端语法 | 27个含开发预览源码，0语法错误 | `frontend-syntax.txt` |
| 后端pytest | **159通过、0失败**，含22个新增接入用例 | `backend.txt`、`backend-results.xml`、`setup-tests.txt` |
| 启动、真实HTTP、浏览器DOM/WebGL | **17通过、0失败**，页面未捕获异常0 | `browser-results.json`、`browser-output.txt` |
| ZIP全新解压、默认单命令安装与启动 | **6通过、0失败** | `release-smoke-results.json`、`release-smoke-output.txt` |
| 空闲行为回归 | **35通过** | `idle-regression.txt` |
| 工位与接待回归 | **22通过** | `posture-regression.txt` |
| 会议行为回归 | **13通过** | `meeting-regression.txt` |
| 城市行为回归 | **13通过** | `city-regression.txt` |

套件有交叉，不应相加为独立业务场景数。前端仍为转译与语法检查加实际浏览器操作，不等于完整TypeScript语义检查。

### 联合检查过程

真实`bash start.sh`启动API、本地数据库和编排器；无预置供应商或模型，初始项目与运行列表为空。浏览器登录后自动进入接入页，实际读取本地协议服务的模型目录，检查错误JSON无请求，再完成OpenAI兼容协议的2次HTTP工具往返。4位成员和3个Skill实际写入数据库。

进入正式3D工作台后检查没有演示控制；Skill页能管理真实内置记录。刷新后无需重新配置。停止并重新启动实际API子进程后，供应商、成员、Skill、登录会话、加密密钥均保持。随后从正式模型页面添加第二个Anthropic协议连接，完成另2次HTTP往返，原成员配置没有被覆盖。检查760px布局、无沙箱时状态非ready、旧预览路径HTTP404。

发布复核另外从ZIP解压到全新目录，未预建虚拟环境，直接执行`bash start.sh --port 8796`。自动离线安装、实际API启动、首页与构建字节一致、随机密钥及0600权限、配置接口登录保护均通过。缺少Docker时仍明确记录缺项，没有执行容器。

### 明确测试替身

这4次模型HTTP请求访问的是独立、明确标识的脚本化协议测试服务，不是真实LLM。它只实现目录、nonce工具调用和OK确认，不执行项目任务。生产代码没有内置它，也不会在正常启动时运行它。

浏览器使用Chromium/ANGLE SwiftShader，当前管理策略禁止URL导航；未改动策略。以既有受限HTTP桥接将正式HTML与真实本地API连接，验证DOM/WebGL与持久化。普通地址栏导航、Cookie/TLS的目标环境行为、原生SSE和下载仍需目标主机验收。截图不是设计图：`01-provider-setup.png`是空白实例，`02-provider-connected.png`和`03-production-office.png`是实际配置了测试服务后的运行界面。

### 本轮发现与修复

新增用例发现正式bundle里尚残留Skills和StudioWorkspace对全局演示对象的判断，已删除；默认目录也不再生成预览脚本。浏览器发现模型目录及select选项影响隐式label的可访问名称，补上稳定的显式aria-label。调整接入页标签对比度。启动端口检测增加SO_REUSEADDR以避免正常重启的TIME_WAIT误报端口占用；仍不会停止已有监听服务。测试脚本的Skill卡片定位改为页面实际结构，并在重启前关闭旧测试页避免残留桥接连接。

## 能承诺与不能承诺

**安装环境满足后，首次业务配置在同一供应商接入页完成。** 导入实际项目、给出需求和必要的项目访问/代码外发授权属于业务操作；平台不会替用户猜测目标仓库、验收规则或授权范围。

真实模型的工具遵循能力、复杂开发任务质量、Docker/runsc实际镜像构建与容器执行、硬件GPU流畅度、Temporal/PostgreSQL整栈、数据库迁移/回滚、HA、长期压测及供应链审计未在本轮完整验证。本地环境没有Docker，因此不能用新增启动器代码或健康检查测试声称容器已验收。

默认便利镜像不作为语言限制；特定项目仍需要实际工具链。此版本不是“一个Key替代所有系统运行时”的承诺，也不是生产成熟度认证。

## 接口参考

此次核对官方资料，用于目录接口与工具回传适配设计，不代表获得供应商认证：

- OpenAI Models API： https://developers.openai.com/api/reference/resources/models/methods/list/
- OpenAI Chat Completions： https://developers.openai.com/api/reference/chat-completions/overview/
- Anthropic Models API： https://platform.claude.com/docs/en/api/models/list
- Anthropic Tool Use： https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview

## 复验

```bash
bash scripts/install-offline.sh
PYTHONPATH=backend .venv/bin/python -m pytest backend/tests -q
node tests/visual/behavior.test.mjs
node tests/visual/etiquette.test.mjs
node tests/visual/meetings.test.mjs
node tests/visual/city.test.mjs
```

联合检查脚本为`tests/e2e/v08_onboarding_acceptance.py`，需要Playwright、Chromium和XServer/Xvfb；在没有既有`.local`的新源码副本中运行。它拒绝覆盖已有部署。测试工具不包含在离线运行依赖包中。

```bash
xvfb-run -a -s '-screen 0 1600x1200x24' python tests/e2e/v08_onboarding_acceptance.py
```
