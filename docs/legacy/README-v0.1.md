# Agent Forge · 多智能体开发协作工坊

**v0.1.0 / 工程 Alpha 源码版 / 2026-09-09**

面向团队内部、本地部署的软件开发多智能体平台。支持每个 Agent 独立选择供应商与模型，运行参数分层配置；通过真实代码修改、测试、审查和人工验收生成成果。3D 办公室只是同一套真实任务状态的视图，不生成模拟工作或对话。

> 这是实际实现的第一版源码，不是已经完成生产验收的成熟产品。后端已通过 53 项自动测试；前端完成 TS/TSX 语法检查。本交付环境不能下载依赖、没有 Docker、没有 Temporal SDK 和真实模型凭据，因此尚未完成前端正式构建、浏览器视觉验收，以及 Docker/gVisor、Temporal、PostgreSQL和真实模型的端到端联调。详见 [测试报告](docs/TEST_REPORT.md)。

## 当前交付范围

| 模块 | 已实现的源码能力 |
|---|---|
| 供应商与模型 | 多个连接、独立模型配置、手填模型 ID、凭据加密版本、显式连通性测试、外发授权校验 |
| Agent | 负责人、开发、测试、审查四类角色；每个实例独立模型、备用模型、指令、运行参数 |
| 配置 | 平台 → 项目 → 角色 → Agent → 单次运行；逐字段解析、来源显示、运行快照 |
| 项目 | 导入本地文件快照；不设置语言/框架白名单；自定义预构建镜像、项目规范和测试命令 |
| 执行 | Temporal 持久化编排；真实读写文件、沙箱命令、依赖任务、串行补丁集成、有限返工 |
| 人工控制 | 计划审批、暂停/继续/取消意图、按成果摘要验收；新建独立重试运行 |
| 成果 | 代码补丁、源代码 ZIP、测试与审查 JSON 报告；原始项目目录不被修改 |
| 可视化 | 中文控制台、轻量 3D 办公室、任务 DAG、事件时间线、状态轮询与事件补发 |
| 团队治理 | 单组织内部账号、管理员/开发者/只读角色、项目访问校验、配置与运行审计 |
| 部署 | Docker Compose、PostgreSQL、独立沙箱 broker、默认 runsc、启动检查和停机备份脚本 |

首版供应商适配器为 **OpenAI Chat Completions 兼容协议** 和 **Anthropic Messages 协议**。可同时接入多个实现这些协议的服务，不代表已经适配所有厂商的独立鉴权和 API。未实现 Responses、Azure 特殊鉴权、Vertex、Bedrock 等原生适配器；可在 `providers.py` 扩展。不会硬编码实际模型型号。

## 启动前提

使用 **Linux Docker 主机**，安装 Docker Engine、Compose 插件、Python 3，以及已注册到 Docker 的 **gVisor/runsc**。建议使用非 root 的专用部署用户，并把主机视为受信任管理边界。macOS/Windows 可使用专门的 Linux 虚拟机；本版本未提供这些系统的原生代码执行器。

首次镜像构建需要访问软件源，或由你准备内部镜像/依赖源。模型服务和 API 凭据需自行配置。本平台不会替你安装或托管大模型，也不要求必须使用公网模型。

**运行中的代码沙箱默认完全断网**，依赖必须预装进项目执行镜像。平台模型调用网络与代码沙箱网络分开。缺少工具链时显示失败/未验证，不伪造通过；导入不限制项目类型，但运行和测试依赖实际执行环境。

官方安装参考：
- Docker Engine: https://docs.docker.com/engine/install/
- gVisor Docker integration: https://gvisor.dev/docs/user_guide/quick_start/docker/

### 1. 初始化并启动

在源码根目录运行：

```bash
bash scripts/init.sh
bash scripts/start.sh
```

`init.sh` 生成权限为 `0600` 的 `.env`、随机管理员密码、数据库密码、凭据加密密钥及沙箱鉴权令牌，并把示例项目复制到 `imports/tiny-project`。不会覆盖已有 `.env`。

`start.sh` 会检查 Docker/Compose/runsc，构建默认执行镜像与平台服务，再启动 Compose。缺少 runsc 时停止，不静默退回宿主机执行。预检查不等于完整集成验收。

```text
工作台：http://localhost:8080
API文档：http://localhost:8080/api/docs
用户名：admin
初始密码：在本地 .env 中读取 ADMIN_PASSWORD；不要提交或转发 .env
```

```bash
docker compose ps
docker compose logs --tail=100 api worker temporal sandbox-broker
```

任务持续排队时先看工作台的编排连接状态和 Worker 日志；不要把“API健康”当成“所有执行服务已经就绪”。第一次启动 Temporal 可能晚于 API，服务会重连或重启。

### 2. 首次配置顺序

1. 在“模型与供应商”添加连接，填写**版本根路径**，例如 `https://provider.example/v1`，不包含 `/chat/completions` 或 `/messages`。同一个供应商可以创建多个独立连接。
2. 添加模型配置，手填供应商实际提供的模型 ID，并声明真实支持的调用参数。测试调用只发送固定文本，不发项目代码，但可能计费。
3. 在“Agent团队”给默认四位成员绑定模型。每位成员可绑定不同连接，至少选择一位负责人、一位开发/测试和一位审查 Agent。
4. 在“项目工作区”导入 `tiny-project`，验收命令填写 `python -m unittest discover -s tests`。使用外部模型时，须在项目中明确授权必要代码外发。
5. 创建任务，例如：`修复 calc.py 中 add 函数：它应返回两数之和，保持函数签名不变，现有测试必须通过。`
6. 查看并批准计划；观察真实任务状态、工具记录、测试和审查。最后下载补丁、报告、源代码 ZIP，再决定是否通过人工验收。

当前版本不会向远程仓库推送或自动合并，也不会发布到生产环境。成果由你检查后接回原项目。新建重试运行从导入的项目基线开始，**不自动继承上一次未验收的修改**；持续开发时需导入新的项目快照。后续需要增加明确的项目基线更新/接回流程。

### 本地模型与局域网访问

API 和 Worker 容器预设 `host.docker.internal:host-gateway`。模型在宿主机时，可使用类似 `http://host.docker.internal:8000/v1` 的地址；模型服务必须监听容器可达的受控接口，不能只监听宿主机 `127.0.0.1`。防火墙应限制访问。容器内的 `localhost` 指该容器自身。

默认只监听宿主机 `127.0.0.1:8080`。团队经局域网访问时，在前方配置 **HTTPS 反向代理**，设置 `.env` 的 `BIND_ADDRESS`、`ALLOWED_ORIGINS` 和 `SECURE_COOKIE=true`。不要直接把未加 TLS 的登录服务暴露到公网。浏览器安全上下文也影响部分前端能力。

## 配置示例

项目或单次运行可覆盖：

```json
{
  "max_parallel": 3,
  "max_model_calls": 100,
  "max_steps": 24,
  "task_timeout_s": 3600,
  "call_timeout_s": 120,
  "command_timeout_s": 120,
  "max_rework_rounds": 2,
  "cpu": 1,
  "memory_mb": 1024
}
```

Agent 级只覆盖个体执行参数，例如 `max_steps`、调用超时和单沙箱资源；整次运行的调用总量、计划大小、任务并行数、返工轮数在项目/平台/单次运行层配置，不能假装在单个 Agent 上生效。

所有字段说明与限制见 [配置说明](docs/CONFIGURATION.md)。本版金额预算、Token硬上限、供应商速率分桶和任意重试策略编辑尚未实现；已实现调用次数硬上限、用量记录、执行步数/时限和明确备用模型策略。

## 自定义项目执行环境

为项目编写自己的 Dockerfile，预装编译器、包依赖和测试工具，构建到本地镜像：

```bash
docker build -f /path/to/project/Dockerfile.agent -t team/project-tools:local /path/to/project
```

将镜像追加进 `.env` 的 `SANDBOX_IMAGES`（逗号分隔），重建 broker：

```bash
docker compose up -d --force-recreate sandbox-broker
```

随后把项目的“执行镜像”设为相同值。镜像需包含 `/bin/sh`，支持配置的非 root UID、只读系统盘和 `/workspace` 可写目录，依赖可放在 `/opt` 等预置路径。需要联网安装依赖的命令不会在断网沙箱中成功。GUI、原生 macOS/Windows、GPU 或多机依赖环境需要新增执行后端，不能宣称已完整验证。

`.forgeignore` 支持相对路径的 `fnmatch` 模式，不是完整 `.gitignore` 语法。默认排除 `.git`、符号链接、常见依赖缓存和密钥文件名。文件名过滤不能发现写在普通源码里的所有秘密，发送外部模型前仍须检查材料。

## 目录

```text
backend/forge/        API、配置快照、协议适配、执行活动、Temporal工作流、沙箱broker
backend/tests/        API/配置/权限/协议/执行集成测试
frontend/src/         中文工作台、表单、Three.js办公室、React Flow任务图
infra/               数据库初始化、broker与默认执行镜像
scripts/             初始化、预检查、启动、测试、静态检查、停机备份
examples/            可用于首轮真实模型联调的小项目
docs/                架构、配置、安全边界、测试报告、集成验收清单、OpenAPI快照
```

## 本地开发与测试

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements.txt -r backend/requirements-test.txt
bash scripts/test.sh

cd frontend
npm install
npm run build
```

前端开发服务器使用 `npm run dev`，通过 Vite 代理连接本机 API。首次实际联网安装后应生成并提交经验证的 `package-lock.json`，部署改为 `npm ci`；当前未伪造依赖锁文件。顶层 npm/Python 依赖已写定版本，尚未锁定完整传递依赖或镜像 digest。

生产用途不得设置 `DISABLE_DISPATCH=true`；此开关仅为 API 测试隔离编排连接。SQLite仅用于这些自动测试，Compose使用 PostgreSQL。

## 备份与安全边界

保护 `.env`，尤其是 `ENCRYPTION_KEY`；丢失后无法解密已存供应商凭据。密钥备份与业务数据备份分开加密保存。

提供保守的停机备份脚本。先停止并确认无在途任务，再运行：

```bash
docker compose stop api worker sandbox-broker temporal
bash scripts/backup.sh
docker compose up -d
```

脚本不会替你自动停止服务，检测到运行中的沙箱也会退出。它不是在线一致性备份，也尚未完成目标主机的恢复演练。源代码版没有配置自动备份调度和日志自动清理。

完整安全边界、未知结果处理及生产化缺口见 [安全与限制](docs/SECURITY_AND_LIMITS.md)。接入重要代码前先运行 [真实环境验收清单](docs/ACCEPTANCE.md)。
