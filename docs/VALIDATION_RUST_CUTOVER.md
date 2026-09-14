# Rust HTTP 与桌面切换验收

日期：2026-09-12。Codex 源码保持不变，前端沿用现有契约。

## 已落地范围

- Axum HTTP/SSE 接管账号、供应商和不可变凭据版本、模型、Agent、项目、Skill、商店、角色模板、设计、组织和权限配置的写路径。
- 模型发现、连接和工具测试、五协议 Benchmark、AI 设计建议使用真实 HTTP 请求；有费用的操作保留持久化幂等结果，结果未知时拒绝自动重试。
- SQLite 产品库独立迁移，兼容 Python scrypt/Fernet；会话只保存 token 摘要，重启后有效，退出、账号停用和 ACL 变更即时生效。
- Rust 内核拥有新 Run/Task、冻结供应商和权限、审批、事件与 outbox；暂停/继续复用原 Codex thread，验收拒绝后可继续同一任务。
- 项目复制、旧产物与新产物下载采用目录句柄和链接校验。工作副本输入预算为 100 MiB，单产物下载和验收上限统一为 128 MiB。
- Electron、默认 Compose、本地启动器和桌面打包使用 Rust。已删除旧 Python 桌面入口及 PyInstaller 构建依赖。

## 验证证据

Rust workspace 测试、严格 Clippy、Electron 测试、打包后端测试通过。新增回归覆盖旧版本缺表迁移、WAL 已提交数据、认证重启、运行控制与审批、验收篡改拒绝、普通回答事务回滚、21 MiB ZIP 下载和链接绕过拒绝。

真实本地 `.local/forge.db` 已导入 `.local/product.sqlite3`：1 个用户、1 个供应商、8 个 Agent、0 个 Skill、1 个项目。原账号登录和退出成功，资产数量通过实际 Rust HTTP 读取核对。原库 SHA-256 在迁移前后均为 `6a331de5a5ceca3ffabba8a15fa5a66205920623b2fff71455ae46a1714b1f2c`，未重置凭据或删除历史数据。

实际未修改 Codex + OpenCodex gateway + Rust HTTP 的最新证据位于 `.local/validation/rust-http-gateway-cutover/verification.json`，脱敏摘要见 [rust-http-cutover.json](evidence/rust-http-cutover.json)。运行完成两次本地供应商协议调用、一次真实文件工具操作、工具结果回传、ZIP/patch 下载和 HTTP 幂等及验收。供应商是本机协议 fixture，未调用付费模型，不能据此声称真实云端模型质量已验收。

`tests/frontend/rust-http-browser.cjs` 使用现有前端登录并遍历 15 个页面，结果为零 JavaScript 错误、零失败 API 请求；桌面和移动截图位于 `.local/validation/rust-browser-1789201761322/`。

```sh
cargo test --locked --manifest-path rust/Cargo.toml
cargo clippy --locked --manifest-path rust/Cargo.toml --all-targets -- -D warnings
node scripts/build-desktop.mjs
.venv/bin/python -m pytest tests/desktop/test_frozen_backend.py -q
PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/test_postgres_rust_export.py -q
.venv/bin/python scripts/verify-rust-http-run.py --output .local/validation/new-run --gateway
```

验证脚本的 `--output` 必须指向尚不存在的目录。产物内的产品数据库和执行目录属于私有测试数据，不提交到仓库。

## 明确限制

以下是切换阶段的历史边界；后续团队、Skill、原生问答与成员会话实现已由 [团队验收记录](VALIDATION_TEAM_RUNTIME.md) 更新。

当前原生 Run 只支持单成员、本地 Codex、固定模型和三档执行权限。多成员调度、MCP 委派、Skill 执行、自定义工具清单、容器、自动项目检查、工作流/模板/团队/路由引用和备用模型尚未支持。模型参数与成员/项目运行覆盖、偏离默认值的平台限制在创建和预检时拒绝，避免静默忽略。历史默认参数保留在产品配置中，不代表 Codex 实际执行限制。

普通问题回答及旧安装/权限审核已持久化决策、审计和 outbox，但这些旧账本还没有 Rust 执行消费者。原生 Codex 的审批通过 runtime approval 接口处理。结果未知的运行不会自动重放，HTTP 尚未提供完整核对恢复流程。

Docker Compose 配置校验通过；PostgreSQL 导出测试使用模拟源连接和真实 Rust 导入器，覆盖扩展表类型及拒绝活动 Run。本机没有可用 Docker daemon，未验证容器实际启动或连接真实 PostgreSQL。桌面包为 Apple Silicon 本机 ad-hoc 签名，未公证。Python 服务保留在显式 legacy/migration 路径供导出和回滚使用，尚不满足整目录删除条件。
