//! Minimal HTTP/SSE compatibility host. All product data comes from a trusted,
//! authorization-aware backend; private kernel commands are never HTTP routes.

mod backend;
pub mod desktop;
pub mod desktop_launch;
mod gateway;
mod http;
mod memory;
pub mod product_api;
mod product_store;
mod python_sqlite;
mod run_service;
mod rust_owned;

pub use backend::{
    ApprovalDecisionInput, Backend, BackendError, ControlAction, Event, EventPage, ProjectAccess,
    RunControl, RunDecisionInput, RunView, User,
};
pub use http::{router, router_with_product_store, ServerConfig};
pub use memory::MemoryBackend;
pub use product_store::{ProductStore, PRODUCT_SCHEMA};
pub use python_sqlite::PythonSqliteBackend;
pub use rust_owned::{RunController, RustOwnedBackend};
