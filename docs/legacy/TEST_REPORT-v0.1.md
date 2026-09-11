# 测试报告 / Agent Forge v0.1.0

报告日期：2026-09-09。交付环境：Linux、Python 3.13.5、TypeScript 5.8.3。具体已安装测试依赖见 `environment-check.json`。容器构建目标Python 3.12尚未执行，因此不能把本结果当成目标镜像验证。

## 已执行结果

| 检查 | 结果 | 边界 |
|---|---|---|
| 后端pytest | **53通过，0失败** | 实际运行；最终一次耗时4.36秒，仅本机测试耗时 |
| Python语法 | 19个.py文件通过AST解析 | 不替代Temporal导入与运行 |
| TS/TSX语法 | 9个文件，0语法错误 | 使用transpileModule，**不是完整类型检查或构建** |
| Compose配置 | YAML解析成功，7个服务 | 未执行docker compose config或实际部署 |
| Shell脚本 | bash -n通过 | 未执行Docker相关操作 |
| OpenAPI | 成功生成25条路径、32个操作 | 描述当前API，不能证明服务间联调 |
| 初始化脚本 | 临时副本实测通过 | 生成0600凭据、复制样例、拒绝覆盖已有.env；不涉及Docker |

实际命令：

```bash
bash scripts/test.sh --disable-warnings --junitxml=../docs/test-results.xml
TYPESCRIPT_MODULE=/path/to/typescript node scripts/check-source.mjs
```

原始测试摘要：`backend-test-output.txt`；JUnit结果：`test-results.xml`；前端语法结果：`frontend-syntax-result.json`；接口快照：`openapi.json`。

## 覆盖内容

配置层级、Agent独立供应商绑定、不可变运行快照、凭据不回显、调用参数校验、循环依赖拒绝、API身份与项目访问控制、CSRF/同源限制、摘要绑定审批、重复创建幂等、文件路径隔离、导入秘密排除、协议请求/响应转换、备用切换条件、内网地址检查、沙箱参数白名单及安全标志。

执行活动级集成用例实际完成：导入有缺陷的项目 → 负责人读文件/生成计划 → 审批API → 开发Agent读写源码 → Git补丁集成 → 运行固定可信的Python单元测试 → 审查 → 生成与读取patch/report/source.zip → 验收API。还验证原始目录未被修改、同运行使用两个不同连接、敏感文件未导出。

额外覆盖：并行补丁真实冲突且现场保留、集成幂等、并发调用配额预留不能越界、未知执行步骤不重放、无测试命令标为未验证、供应商撤销覆盖历史快照授权。

## 关键测试替身说明

**模型回复使用脚本化测试桩。** 两种供应商的HTTP协议测试使用httpx MockTransport，未访问真实供应商，不代表模型质量或某个实际型号兼容性。

**执行链路测试调用真实Git和真实Python unittest，但没有实际Docker。** 测试将沙箱RPC替换成只允许运行一条固定、可信的fixture测试命令；没有在宿主机运行任意模型生成的命令。Docker配置测试只验证参数和路径，不证明gVisor逃逸防护已通过。

**工作流服务没有启动。** 集成用例逐个调用生产Activity函数与API审批接口；没有执行Temporal Worker和真实工作流历史回放，也没有运行PostgreSQL。测试使用SQLite。

## 已发现并修复

复制Git工作副本后，复制的index携带原工作区文件元数据，使`git apply --index`将干净基线误判为不匹配。复制后增加`git update-index --refresh`，正常集成及真实冲突用例均通过。没有通过忽略真正冲突来绕过错误。

前端静态检查发现两处JSX语法错误，修复后全部9个文件语法通过。尚未执行npm正式构建或浏览器视觉验收，可能仍存在依赖集成/类型/交互问题。

## 尚未执行

npm依赖安装、完整TypeScript检查、Vite生产构建、浏览器渲染和交互、Docker镜像构建、gVisor执行、Compose服务联调、TemporalSDK/服务、PostgreSQL、真实模型、多用户压测、长时间故障恢复、备份还原、安全审计，以及真实开发任务成功率基准。

原因：当前工具容器无法解析外部依赖源，没有Docker和Temporal SDK，也没有用户模型凭据。这些不确定性已保留为待验收项，没有用模拟视频、假状态或编造测试结果替代。
