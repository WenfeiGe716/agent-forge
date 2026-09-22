# Agent Forge macOS 桌面版

## 使用

双击 `desktop/release/Agent Forge-darwin-arm64/Agent Forge.app`。这是内置 Electron、Rust 后端、固定版本 Codex 和前端资源的本机应用，运行不依赖仓库中的 `.venv` 或终端进程。当前版本支持 Apple Silicon macOS；执行工具（例如 Git、项目语言运行时）仍需在本机具备，就绪检查会报告缺项。

首次启动可导入包含 `.local/config.json` 的原项目目录。应用保留数据库、任务成果、项目工作副本、供应商密钥、角色模板、Skill 和组织布局，清除副本中的旧网页登录会话。原工作空间不被改写；它与新桌面工作空间是两个独立实例，不会双向同步。请后续主要使用桌面入口，避免在两个实例重复下发相同任务。

原环境有未结束任务时，先完成或取消，再导入。导入同时核对数据库、文件及加密密钥；缺失密钥、越界数据路径、链接或并发写入会中止，不通过生成新密钥或部分复制伪装成功。失败准备目录不会成为有效工作空间。

桌面端默认由当前 macOS 账号进入本地管理员会话；没有开放远程桌面登录服务。退出登录后，可用 **Agent Forge → 重新进入本地工作空间** 再次确认进入，也可使用原平台账号密码登录。

## 项目和权限

项目工作区 → 导入项目 → 选择项目文件夹，原生选择器只产生短期一次性目录授权。保存时校验目录身份并复制安全文件快照；目录授权过期、被替换或已经使用时，请重新选择。

选择目录并不批准联网安装、项目外访问或完全访问。执行仍遵守「请求审批」「帮我批准」「完全访问」，Docker/gVisor 仍是可选项。源目录不会被自动回写。应用是执行管理入口，不会自动安装用户任务所需的系统软件。

关闭窗口会隐藏到后台，Dock 或 `⌘1` 可重新显示。任务状态变化可产生系统通知；系统是否展示由 macOS 通知设置控制。`⌘Q` 会检查所有未结束任务（含排队、暂停和等待审批），有任务或无法确认状态时先提示。退出可能中断执行；已完成活动可复用，中断中的工具活动保留未知状态待人工核对，不盲目重放副作用。

任务成果和 Skill 导出通过原生保存对话框保存；不会自动打开下载文件或运行可执行产物。外部网页不会在具有桌面权限的工作台窗口内打开。

## 供应商协议与模型参数

修改现有供应商的协议或自定义协议配置时，保存前会检查当前模型配置及绑定成员的覆盖参数。不兼容时拒绝整个连接变更，保留原连接与凭据；提示会列出模型名称、型号、配置 ID、协议及具体原因。当前列表中停用的配置也会检查，已移出的历史配置和已有运行快照不被改写。

例如 Responses 使用 `max_output_tokens`，而旧 Chat Completions 配置可能使用 `max_tokens`。请同时核对「声明支持的调用参数」和「默认模型参数 JSON」，系统不会静默改名、丢弃参数或重新绑定 Agent。如果新旧协议无法兼容同一配置，请新建供应商连接，在新连接下配置模型后再人工切换绑定。

启用模型不等于验证上游服务或工具能力；批量操作校验失败时不会部分启用。

## 数据和备份

默认目录为 `~/Library/Application Support/Agent Forge/workspace`。菜单「打开应用数据目录」可打开。`config.json` 不含供应商加密密钥、管理员初始密码或执行服务口令；这些值在 `secrets.enc` 中由 Electron safeStorage 使用 macOS 钥匙串保护。已有供应商凭据仍按原密钥加密存储于 SQLite。

备份前退出应用，然后完整备份工作空间。`secrets.enc` 依赖原登录钥匙串；单独复制它到其他 Mac 或删除钥匙串不能保证可恢复。本版未实现跨机器密钥恢复向导，勿删除原工作空间和原钥匙串。解密失败时应用不会覆盖或重建现有数据。

桌面服务只监听随机回环端口。独立的每次启动凭据保护全部请求；本地原生控制凭据与界面请求凭据分离。客户端验证后端身份后才建立会话，不接管已有的 8787 服务。渲染进程禁用 Node、启用进程沙箱和上下文隔离，IPC 仅接受自己主窗口的工作台页面。网页入口没有原生目录授权接口。

## 本地构建

在 Apple Silicon Mac 上执行：

```sh
bash scripts/build-upstreams.sh
npm install --prefix .local/build/gateway-toolchain --save-exact bun@1.4.2
npm ci --prefix desktop
node scripts/build-desktop.mjs
```

构建工具依赖网络；应用启动本身不下载运行时。Electron 固定为 `44.3.0`，gateway 的 Bun 固定为 `1.4.2`；npm 依赖由 `desktop/package-lock.json` 锁定，Rust 依赖由 `rust/Cargo.lock` 锁定。构建脚本验证 OpenCodex 的固定提交和认证补丁，再将 gateway 源码及其依赖一并打包。桌面打包不再使用 Python 或 PyInstaller。

如果官方运行时下载不可达，可将同版本的 `electron-v44.3.0-darwin-arm64.zip` 放到 `desktop/build/`；脚本必须通过 Electron npm 包提供的 SHA-256 校验才会使用它。不要禁用校验。打包只复制显式列出的客户端代码和冻结资源，不打包 `.local`、用户数据库、凭据或项目。

开发启动先运行 `node scripts/build-offline.mjs`，再使用 `npm start --prefix desktop`（首次需执行 `desktop/node_modules/.bin/install-electron` 安装 Electron 开发运行时）。开发和打包版本默认使用同一桌面数据目录并受单实例锁保护，避免同时启动。

## 验证与发布范围

```sh
node scripts/check-source.mjs
node scripts/typecheck.mjs
npm test --prefix desktop
cargo test --manifest-path rust/Cargo.toml --locked --workspace
.venv/bin/python -m pytest tests/desktop -q
```

`build-desktop.mjs` 会在打包前强制执行语义 TypeScript 检查，并在复制到 `.app` 后逐文件比较前端资源 SHA-256；资源不一致或签名结构错误会直接失败。`test_frozen_backend.py` 使用真正打包的 Rust 后端和隔离临时数据，验证启动、会话、目录导入、文件排除和退出，不调用外部模型，也不读取生产凭据。可用 `FORGE_DESKTOP_TEST_BINARY` 指定 `.app/Contents/Resources/backend/forge-server`。Rust 的 `desktop` 集成测试还覆盖一次性目录票据、旧 SQLite 只读迁移和进程身份。

真实供应商、多成员并行、故障恢复和 Skill 原生工具读取属于带凭据的验收层，不会在默认离线构建中伪造通过；在准备好供应商和固定 Codex 后运行 `python3 scripts/verify-rust-team-live.py --help` 查看参数，再把输出证据保存到独立临时目录。这样能把“构建通过”和“真实模型链路通过”分开，避免桌面发布误把 fixture 结果当作供应商验收。

本机构建使用 ad-hoc 签名并执行严格签名结构检查。面向其他用户公开发布需要合法的 Apple Developer ID 签名、公证和额外分发验收；本版没有购买证书、上传应用、公证或自动更新配置。Windows 仍需文件安全访问、进程管理和受控执行适配，不能只把 `.app` 换成 `.exe`。

实现依据：[Electron 安全建议](https://www.electronjs.org/docs/latest/tutorial/security)、[safeStorage](https://www.electronjs.org/docs/latest/api/safe-storage)。容器与旧数据库迁移见 [Rust 部署说明](RUST_DESKTOP_DEPLOYMENT.md)。
