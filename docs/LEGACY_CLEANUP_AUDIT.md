# Legacy Cleanup Audit

日期：2026-09-12。

本轮已经完成桌面和默认容器入口的 Rust 切换：

- `desktop/main.cjs` 通过 `desktop/supervisor.cjs` 启动 `forge-server --desktop`；旧的
  `desktop/backend_entry.py` 和 `desktop/requirements-build.txt` 已删除。
- `scripts/build-desktop.mjs` 构建并打包 Rust `forge-server`、固定 Codex runtime 和
  OpenCodex gateway 资源，不再调用 PyInstaller。
- `docker-compose.yml` 的默认 `api` 服务使用 `infra/Dockerfile.rust`。Python 服务、旧
  worker 和 Temporal 仅在显式 legacy/migration/ops profile 下启用。
- `start.sh` 默认调用 `scripts/start-rust.mjs`；只有 `--legacy` 才进入旧 Python
  本地启动器。原配置和数据库保留，Rust 产品库和 Run 内核分别使用新文件。
- Rust `ProductStore` 负责产品数据、会话、凭据和迁移；`RustOwnedBackend` 独占新 Run、
  Task、provider snapshot、权限、审批、事件和 outbox。`PythonSqliteBackend` 是 Rust
  内部兼容旧 schema 的读取适配器，不需要 Python 进程。PostgreSQL 通过显式导出脚本
  进入 Rust SQLite，不允许 Rust 直接双写旧库。

仍保留的 Python 代码属于迁移或兼容边界，而不是桌面运行时依赖：

- `backend/forge/*`、`backend/Dockerfile` 和 `scripts/start-local.py` 提供旧 FastAPI
  服务，供回滚、数据导出和历史测试使用；它们不在默认桌面或 Compose 启动链中。
- `scripts/export-postgres-to-sqlite.py`、`scripts/export_rust_run_config.py` 只读旧
  数据并生成 Rust 可导入快照。
- `backend/tests` 保留旧 API 回归覆盖，直到生产迁移完成并确认无回滚需求。

安全清理边界：

1. 不修改或删除 `third_party/codex`；Codex 源码及其固定 runtime 由 Rust transport
   直接托管。
2. 不删除 Python 迁移工具和 legacy profile，除非生产数据库已完成导出、校验、备份，
   且回滚窗口已经关闭。
3. 删除旧 FastAPI/worker 的最后一步应在一次发布中完成：先停写旧服务，再验证 Rust
   HTTP、桌面包、容器和迁移快照，最后移除依赖和 profile。任何 Run 不得在两个宿主间
   双写。

当前验证状态：Rust workspace、forge-server desktop 集成测试和 clippy 已覆盖核心路径；
桌面打包脚本已切到 Rust。Docker daemon 在本机不可用，因此容器实际启动仍需在 CI 或
有 Docker daemon 的环境执行。

真实本地旧库已完成导入，原文件摘要不变。新增表兼容、持久化认证、15 个现有前端页面、
实际 Codex 工具往返与产物验收均有验证记录，见 [切换验收](VALIDATION_RUST_CUTOVER.md)。
仍不删除 Python 的多成员/Skill/容器执行实现：Rust 尚无这些能力和旧执行账本消费者，
不能把入口切换描述为完整功能等价，也不能关闭迁移与回滚路径。
