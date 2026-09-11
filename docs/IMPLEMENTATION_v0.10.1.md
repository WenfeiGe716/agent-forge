# Agent Forge v0.10.1 — Skill商店与Agency Agents适配

日期：2026-09-09。基于已经交付的v0.10.0源码增量实现。本版增加本地Skill商店，不替换已有执行、模型、人物或城市系统。

## 1. 交付范围

商店入口位于办公室顶部「Skill商店」以及管理工作台侧栏。商店与原有「Skill能力库」是两个相连页面：前者管理可发现的内容和来源，后者保留编辑、审核、导出、撤销和已有运行能力。

| 操作 | 本版实际行为 |
|---|---|
| 来源管理 | 添加多个公开GitHub仓库或本地ZIP来源，编辑名称和引用、启停、显式同步；默认提供Agency Agents的来源配置。 |
| 本地目录 | 首次启动列出平台原有的三份自有Skill，不自动安装、审核、绑定、创建供应商或调用模型。 |
| 浏览搜索 | 按名称/描述/路径、类别、内容类型、来源及安装历史筛选，分页24项；没有虚构评分、下载量和能力认证。 |
| 条目详情 | 查看描述、类型、原始路径、来源依据、许可证、内容摘要、文件、历史版本和与本地版本的差异。 |
| 安装 | 明确确认来源/使用权后，生成现有能力库中的待审核版本；只保存内容，不执行任何仓库安装脚本。 |
| 审核与绑定 | 原有摘要绑定审核通过后，选择目标Agent、明确版本、按需/阶段加载方式、JSON参数；默认不授权脚本。 |
| 更新 | 同步只产生新的目录版本；明确安装、审核、换绑后，后续新运行才使用新版本。 |
| 回到旧版本 | 在详情选取已安装且仍获批准的旧版本，明确修改成员绑定；不是热回滚在途任务。 |
| 团队发布 | 把已审核本地Skill发布到当前组织的商店目录；不上传公网，其他安装为独立Skill。 |
| 卸载 | 停用本地Skill并保留历史。有成员绑定或在途/待处置运行引用时拒绝，提示先处理引用。 |

这里的商店是团队本地内容分发与安装管理，不是付费电商系统、公共托管平台或独立外部Agent运行器。

## 2. 对agency-agents的参考与适配

阅读了上游README、MIT许可证、Frontend Developer示例和`convert.sh`。该仓库主要组织角色职责、方法、工作流程和交付规范，其转换脚本也将角色正文输出为标准`SKILL.md`。

本平台明确区分：

| 类型 | 导入方式 | 不应误解为 |
|---|---|---|
| 原生Skill | 识别`SKILL.md`，保留当前运行器支持的文本资源 | 自动支持任意二进制资源、脚本依赖或运行器扩展 |
| 角色方法包 | 识别带name/description的Markdown角色文件，生成受控的参考型Skill；保存原始文字内容与元数据 | 已创建同名Agent、已变更Agent身份、已经获得某类工具能力或通过能力测评 |

Agency来源下角色规范使用`agency-...`名称。包内包含说明正文、`references/original-agent.md`、上游许可证及来源记录。导入统一UTF-8/LF文本，不把GitHub页面抓取文本伪装成逐字节下载归档。`allowed-tools`等声明可查看，但实际权限仍由平台和当前执行阶段控制。

上游MIT文本要求保留版权和许可声明；适配器保留包内许可证及仓库根许可证，不把该项目的署名改成平台自己的署名。缺少许可证文本的条目会提示管理员自行确认使用权，审核不是法律或安全认证。

### 在线和离线来源

在线同步只接受公开`https://github.com/组织/仓库`，支持`.git`后缀。先通过GitHub API把分支/标签解析为具体提交，再读取该提交的ZIP；校验响应上限、不跟随重定向、不携带模型凭据、不关闭TLS验证。版本记录包含提交和内容摘要。重复相同内容不会制造新的本地安装。

ZIP导入允许离线使用，记录上传文件摘要；**ZIP导入不能证明它来自某个GitHub提交**，因此显示「本地ZIP · 上游未核验」。GitHub源也可导入ZIP，来源名称本身不提升来源可信度。私有GitHub认证、企业GitLab、SSH或任意自定义下载地址尚未实现，可通过经过批准的ZIP流程接入。

**本轮执行容器访问GitHub依赖端点仍发生DNS解析失败。已阅读公开页面，尚未下载完整上游归档，也没有完成agency-agents全仓库在线同步验收。默认Agency Agents目录是未同步状态，不包含伪造的上游条目。** 目标部署环境可显式点击同步，或者上传自行取得的仓库ZIP。实际同步结果会报告兼容条目和拒绝原因，不能预先承诺上游全部内容均能原样运行。

### 参考来源（2026-09-09读取）

- 项目说明：https://github.com/msitarzewski/agency-agents
- 许可证：https://github.com/msitarzewski/agency-agents/blob/main/LICENSE
- 角色示例：https://github.com/msitarzewski/agency-agents/blob/main/engineering/engineering-frontend-developer.md
- 转换流程：https://github.com/msitarzewski/agency-agents/blob/main/scripts/convert.sh
- Skill格式：https://agentskills.io/specification

## 3. 状态、持久化及运行边界

新增四张表：`skill_store_sources`、`skill_store_items`、`skill_store_revisions`、`skill_store_installs`。来源/目录版本与本地已审核Skill版本分开，不用远程main分支覆盖已有工作内容。

所有安装与绑定通过真实API持久化。安装调用既有`skills.save`，审核调用原有审核接口，运行沿用既有`freeze_bindings`、`activate_skill`、`read_skill_resource`和撤销检查，不是只往Agent配置加一个显示标签。

不同供应商、主/备用模型、角色指令、运行参数和其他Skill均保持原样。来源同步不调用模型，按需使用Skill时仍由Agent实际模型调用加载工具；不会把整个商店塞入每个Agent上下文。

源配置、同步和Agent绑定使用版本比较，防止过期编辑覆盖。安装同一内容幂等，同名现有Skill不会被接管；源更新与本地手工修订冲突时拒绝覆盖。发生数据库并发安装冲突返回409并回滚。内容摘要用于内容一致性，不等于发布者数字签名。

来源下架条目保留历史；停用来源不自动撤销已审核的本地Skill。需要停止实际使用时，应撤销相关本地版本或停用Skill。撤销会影响后续加载，不能撤回已经发送的模型请求。

## 4. 安全和格式约束

在线只访问固定公开GitHub端点，不提供任意URL代理。来源修改、同步、导入、安装、审核、绑定、卸载和团队发布需要管理员；已登录账号可以浏览组织共享内容。因此不要在共享Skill中放密钥或私人资料。

仓库ZIP上限20MB、解压声明总量60MB、8000条目、目录上限1500项。拒绝路径穿越、绝对路径、符号链接、重复路径、加密ZIP和不安全路径。仅在内存读取，不向任意路径解压。原生Skill包含本版不支持的二进制或超限资源时拒绝该包，不把资源偷偷丢掉再宣称兼容。

本地运行器继续采用受限YAML、UTF-8文本资源、每包80文件/1.5MB/单文件200KB等边界；商店加入的来源和许可证文件也计入限制。嵌套Skill根目录需拆包。被拒绝条目在来源详情中有原因；没有自动执行`install.sh`、`convert.sh`、Git hook、包安装或MCP授权。

Skill脚本默认不授权。主动启用后仍要求已有命令权限、开发阶段和真实隔离执行环境；本轮没有使用不受限宿主机命令代替沙箱。

## 5. 实际验证结果

| 检查 | 结果 | 证据 |
|---|---|---|
| 全新虚拟环境离线依赖安装 | 通过，沿用27个已声明来源的重打包wheel | `evidence-store/offline-install.txt` |
| 前端正式构建 | 30源文件、0语法错误 | `evidence-store/frontend-build.txt` |
| 全部前端语法 | 33源文件、0语法错误；不是完整语义类型检查 | `evidence-store/frontend-check.txt` |
| 后端回归 | **204通过、0失败，其中本轮商店新增35项** | `evidence-store/backend.txt`、`backend.xml` |
| 正式页面+本地API+重启 | **19项通过、未捕获前端错误0** | `evidence-store/browser-results.json`、`browser-output.txt` |
| 既有视觉状态机 | 空闲35、接待22、会议13、城市13、模型15、常驻环境29通过 | `evidence-store/regression-*.txt` |
| 原始核心文件对照 | 21个室内/城市/状态机/任务执行关键源文件与v0.10.0逐字节相同 | `evidence-store/preserved-source.json` |
| 新解压源码包启动 | 8项通过：新目录自动安装、启动、登录、目录和正式页面 | `evidence-store/release-smoke.json` |
| 上游实际网络 | 失败：DNS临时解析错误；未计为通过 | `evidence-store/upstream-network.json` |

套件存在重叠，不应把数字相加成为独立场景数量。发布启动检查使用新解压、没有虚拟环境的目录实际执行`bash start.sh`；报告补充后最终压缩包重打包，已核对最终包中六个启动/API/前端关键文件与该次实测逐字节相同。

后端实测覆盖：来源白名单、ZIP拒绝、角色适配、许可证保留、原生包兼容拒绝、幂等安装、待审核不可绑定、名称冲突、按成员保留模型配置、更新差异、显式旧版本绑定、本地修订保护、缓存保留、下架、撤销、卸载在途保护、只读权限以及安装后的实际Skill加载/资源读取。真实运行器测试中创建真实运行快照，并确认后续换绑不改变已有快照；脚本执行未授权时拒绝。

网络适配器的「先解析提交再下载」和不跟随重定向用httpx测试传输验证，不冒充公网下载。角色导入使用明确命名的原创合成测试文档，不是未经核对的Agency Agents副本。

浏览器检查实际经过登录、三份原有Skill目录、搜索筛选、文件查看、安装、审核、给Nova绑定、错误JSON、卸载保护、团队发布、真实文件选择和ZIP HTTP上传、查看版本差异、显式安装更新、900px布局、返回原能力库、城市时钟保持、登出，以及真实进程停止/重启后的目录和绑定保持。浏览器商店流程没有创建模型、供应商、项目、任务或模型调用。

初次浏览器验收中两处测试选择器错误已修正：文件页不应查找说明页组件；原能力库按钮实际名称为「创建Skill」。修正后整条流程从干净实例重跑；没有删掉相关业务检查。

### 截图与运行环境

`01-store.png`：真实页面的三份平台原有Skill，未加载上游或虚构评分。

`02-detail-bind.png`：真实能力库中的审核版本与Nova绑定。

`03-sources.png`：实际来源列表，Agency Agents仍显示未同步。

`04-update-fixture.png`：明确标识为「测试专用角色方法包」的合成ZIP更新差异，仅作为协议测试证据。

浏览器为Chromium/SwiftShader。使用既有受限桥接加载实际生产构建，所有`/api`访问连接实际本地后端；本轮扩展测试桥接以传输真实ZIP字节。没有修改浏览器策略。检查DOM、WebGL和实际HTTP/持久化，不代表原生URL导航、HTTPS/Cookie、EventSource自动恢复或下载管理器已经在目标主机通过。截图不是生成图。

## 6. 保留和未完成范围

室内精修、城市环境、工位坐姿、领导接待、多Agent协作与目标判断沿用现有实现。商店仅增加导航、管理前端及本地数据接口，没有添加演示推进按钮或供应商登录门禁。

本版未完成：上游全仓库实机同步、任意第三方Skill全量兼容、私有Git直接认证、角色模板一键创建独立员工、公共商店/付费体系、自动安全签名认证、真实LLM理解与执行上游方法的质量评测。Model Hub完整能力测评/评分、自定义任意协议供应商以及智能模型路由仍是独立待实现项，不计入本版交付。

真实模型、Docker/gVisor、Temporal/PostgreSQL整栈、硬件GPU、长期压力/备份恢复/迁移回滚和生产安全审计未在本轮验收。已有软件GPU帧率问题没有在本轮修复，也没有复用历史数据声称获得提升。

## 7. 复验与部署

```bash
bash start.sh
# 默认 http://127.0.0.1:8787，平台账号登录后打开Skill商店。

PYTHONPATH=backend .venv/bin/python -m pytest backend/tests -q
node tests/visual/ambient.test.mjs
node tests/visual/behavior.test.mjs
node tests/visual/etiquette.test.mjs
node tests/visual/meetings.test.mjs
node tests/visual/city.test.mjs
node tests/visual/models.test.mjs
```

`tests/e2e/store_acceptance.py`需Playwright、Chromium、DISPLAY和独立测试目录，拒绝覆盖已有`.local`部署；占用8797端口。三份自有Skill以包资源形式随API分发，Docker源码路径也保留目录，但本轮未验证容器构建。

新表由现有启动建表流程创建；没有宣称支持跨版本在途工作流热替换。管理员升级操作留在部署文档，不显示为日常办公前置步骤。
