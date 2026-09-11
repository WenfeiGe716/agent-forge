# Agent Forge v0.2 · 修复与验证报告

## 结论

**离线安装、前端构建、本地运行器、可视化交互和脚本化多Agent协作链路已经实测。真实模型质量、Docker/gVisor、Temporal/PostgreSQL及目标部署环境尚未完整验收。**

不要把以下“通过”理解为所有成熟平台需求已经实现，或平台已达到生产可用性/安全认证。每项结果限定在下面写明的环境和方法。

## 1. 依赖安装与构建

| 项目 | 结果 | 原始记录 |
|---|---|---|
| 新虚拟环境安装 | 27个重新打包的wheel安装成功，无公网下载 | `evidence/offline-python-install.txt` |
| Python依赖一致性 | `pip check`：No broken requirements found | `evidence/offline-python-check.txt` |
| 交付脚本安装复验 | `scripts/install-offline.sh`实际执行成功，使用源码根目录的新.venv | `evidence/release-installer.txt` |
| npm锁文件 | 根据新无registry依赖清单实际生成，不是手工伪造 | `evidence/npm-lock-generation.txt`、`frontend/package-lock.json` |
| npm安装 | `npm ci --offline --ignore-scripts`成功 | `evidence/npm-ci-offline.txt` |
| 前端构建 | 本地React运行时＋本地TypeScript编译器构建单文件页面成功 | `evidence/npm-build.txt` |
| 前端语法 | 10个TS/TSX文件，0语法错误；不是完整语义类型检查 | `evidence/offline-build.json`、`evidence/npm-source-check.txt` |

没有修复或绕过容器的公网DNS策略。网络诊断仍显示依赖源解析失败；本轮解决方法是交付可复现的离线核心依赖路径。Docker镜像、Temporal SDK、数据库服务和模型权重不包含在该wheel包中。

**供应链来源边界：** wheel根据已安装发行包重建，保留许可证、元数据和包文件，生成新的RECORD和SHA256。它们不是上游原始wheel，也未完成外部供应链/漏洞审计。`offline/MANIFEST.json`、`offline/requirements-linux-cp313.lock`及wheel内说明提供来源。前端vendor同样保留具体来源哈希和第三方许可证，没有复制字体文件。

## 2. 后端自动测试

**70项通过、0项失败。** 在离线安装出的虚拟环境内运行：

```bash
PYTHONPATH=backend .venv/bin/python -m pytest backend/tests -q \
  --junitxml=docs/evidence/backend-results.xml
```

原始结果：`evidence/backend-test-output.txt`、`evidence/backend-results.xml`。

覆盖配置继承与快照、独立Agent供应商、权限/审批、文件路径、Git集成/冲突、有限调用预算、协议转换、消息隔离与幂等、消息限额、上下文送达、活动日志复用、未知活动禁止重放及全局活动并发限制。

部分协议/业务单元测试使用测试替身；原有活动级集成测试真实调用Git和固定可信Python单测，没有真实Docker。不能用这些用例宣称沙箱逃逸防护已验收。

## 3. 浏览器、3D及交互联合验收

**40项通过、0项失败；未捕获前端异常为0。** 详细逐项结果：`evidence/browser-results.json`。实际Chromium渲染器为ANGLE/SwiftShader软件GPU，不是伪造截图或静态效果图。

| 范围 | 实测内容 |
|---|---|
| 基础页面 | 登录、初始数据、验证模式明确标识、1024px下无水平溢出 |
| 3D拾取 | 点击实际画布上的人物进入正确成员详情，Esc关闭返回 |
| 相机 | 拖动旋转、滚轮缩放、键盘方向键/Home、复位与聚焦；拖动不误开成员弹窗 |
| 场景 | 日夜切换改变实际像素；布局切换移动人物，并注明布局不代表实际会议 |
| GLB | 非法文件拒绝；内嵌静态模型解析、替换渲染、恢复人物；错误头/截断/超限/错误版本拒绝 |
| 渲染生命周期 | 多次3D/任务图切换，不重复标签、不丢失上下文；10成员按8＋2分页 |
| 任务图 | 实际依赖节点、缩放/复位、点击读取契约与上游交接 |
| 配置 | 错误JSON提示、Agent参数保存、平台参数保存、新增供应商、实际HTTP连通测试 |
| 开发任务 | 配置快照预览、创建真实任务、计划审批、暂停/继续、最终人工验收 |
| 协作 | 真实消息/交接事件更新界面，两个协议供应商用于同一运行 |
| 成果与审计 | 产物URL与HTTP返回内容核对、原始项目未修改、密钥文件排除、快照不回显凭据、审计展开 |

### 浏览器测试方法的明确边界

本执行容器的Chromium受管理策略限制，禁止所有URL导航。**没有移除、修改或禁用该策略。** 测试使用`page.set_content`加载实际生成的单文件页面，以受限测试桥接将`/api`请求送到真正运行的本地API，并将实际HTTP SSE内容交给页面。桥接层不生成任务状态、模型回复或虚构服务端结果。

因此，本报告证明实际DOM/WebGL操作与真实API的联合工作，但**不覆盖普通地址栏导航、目标机器Cookie/HTTPS/CORS配置、浏览器原生EventSource断线重连或原生下载弹窗**。产物下载验证的是实际HTTP响应与文件内容，而不是浏览器下载管理器。后端SSE游标续传另做了真实HTTP测试。

测试源代码：`tests/e2e/browser_acceptance.py`、`tests/e2e/browser_transport.py`。同一份单文件页面也由正常启动的API实际提供；HTTP内容哈希校验见正常启动结果。

## 4. 多Agent自动交互链路

参与角色：Atlas负责人、Nova开发、Echo测试、Sage审查。负责人和开发者使用OpenAI兼容协议连接，测试和审查使用Anthropic协议连接。

单个完整测试运行实际发生：

- 16次HTTP模型请求。连接服务返回脚本化、可核验的工具调用，不调用真实LLM。
- 3个开发子任务：修复加法与文档任务并行，然后依赖交接后补充边界测试。
- 4条显式Agent消息，均在后续模型请求前进入接收者上下文；2条依赖成果交接。
- 真实修改`calc.py`、生成说明和测试，使用独立Git工作副本并进行串行集成。
- 真实运行加法、负数、零值3项固定单测，全部通过。
- 独立审查阶段完成后仍等待人工验收，最后由界面提交验收通过。

原始证据：`evidence/browser-run-events.json`、`evidence/browser-changes.patch`、`evidence/browser-report.json`、`evidence/browser-source.zip`。

**不能据此推导：** 真实大模型会自行制定同样的计划、会正确理解消息、能够处理任意工程，或多Agent优于单Agent。证明这些需要真实供应商接入和代表性任务基准。

验证命令服务只接受固定命令和已知精确源码字节；把这些内容复制到干净临时目录后执行可信unittest，不允许运行任意模型生成的宿主机命令。其通过结果不等于Docker/gVisor通过。

## 5. 故障恢复与HTTP专项

**6项通过，0项失败**，见`evidence/recovery-results.json`：

| 检查 | 实际结果 |
|---|---|
| 未登录请求 | 访问运行列表被拒绝 |
| 真进程停止/重启 | 在等待计划审批时停止API进程，重新启动相同数据库/密钥；计划摘要不变，模型请求仍是3次，没有重复调用 |
| 过期审批 | 重启后错误摘要返回409 |
| 拒绝计划 | 已保存的拒绝使运行取消，不启动开发Agent |
| 调用预算 | 上限设为1时恰好发生1次调用，随后转人工，不越界继续 |
| SSE游标 | 实际HTTP请求携带Last-Event-ID，只返回该游标之后的事件 |

本地运行器的未知活动保护另有单元测试：活动执行后结果未确认时，重建运行器也拒绝自动重放。**没有宣称所有中断位置、机器掉电、磁盘损坏或数据库故障均已验证。**

## 6. 正常启动复验

**5项通过，0项失败**，见`evidence/normal-start-results.json`：使用真正的`start-local.py`启动，成功登录、选择local编排器、明确关闭验证夹具模式、HTTP根路径提供与构建产物完全一致的页面、安装级运行上限可读取，且没有预设伪造模型连接。

该复验不含真实模型和沙箱调用。

## 7. 本轮修复

1. 离线构建/安装：从“依赖源无法访问导致无法构建”变为“提供独立核心离线运行路径”。原Temporal容器栈没有被冒充为已验证。
2. `crypto.randomUUID`兼容性：浏览器实测发现新建任务在缺少该方法时崩溃，改为安全随机字节生成请求ID的兼容路径，不使用Math.random替代。
3. 渲染器资源：避免在同一canvas重建渲染器时主动丢失上下文，补充指针事件和缓冲区清理，实测GLB替换和视图多次切换。
4. 协作：补充真正的消息存储、授权范围、限额、幂等、上下文传递和可追溯交接，而不只是人物动作。
5. 交付一致性：前端Dockerfile改为使用已构建产物，修正相关COPY与忽略规则；这些Docker配置仅做静态检查，没有声称镜像构建通过。

## 8. 尚未完成／不应承诺的项目

| 项目 | 状态与原因 |
|---|---|
| 真实供应商与模型能力 | 没有用户供应商凭据/模型服务，未做真实模型推理与开发质量评测 |
| Docker/gVisor执行与安全 | 本环境没有Docker/runsc，未跑真实容器、镜像拉取、资源/网络隔离及逃逸检查 |
| Temporal/PostgreSQL整栈 | 缺少对应SDK/服务与镜像；保留适配器，不把本地模式结果套用到该栈 |
| 目标浏览器正常访问 | 受当前管理策略限制，需在实际部署主机复核普通导航、HTTPS、Cookie、原生SSE和下载 |
| 完整TypeScript语义检查 | 当前构建为转译＋语法检查＋浏览器实测，不等于tsc --noEmit语义检查 |
| 生产成熟度 | 未完成HA、压测、长期运行、备份恢复演练、数据库迁移体系和供应链安全审计 |
| 完整平台规划中的全部功能 | 金额预算/Token硬上限、完整供应商速率控制、生产发布、跨平台原生Agent协议等仍非本轮交付 |
| 高级3D模型 | 仅会话内静态GLB预览，不含骨骼动画、贴图、复杂场景编辑、持久化模型资产管理 |

以上边界也是本版本的发布边界。源码、测试和证据可用于继续开发及在实际环境复验，不应直接视为生产认证。
