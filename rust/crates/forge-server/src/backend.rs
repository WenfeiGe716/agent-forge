use crate::product_api::{ProductError, ProductReply};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use thiserror::Error;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct User {
    pub id: String,
    pub username: String,
    pub role: String,
    pub enabled: bool,
}

/// Trusted host metadata, never accepted from an HTTP request.
#[derive(Clone, Debug)]
pub struct ProjectAccess {
    pub id: String,
    pub owner_id: String,
    pub members: Vec<String>,
}

/// A frontend projection provided by the run's sole state owner. The backend
/// must exclude internal secrets (claim tokens, credentials) from both views.
#[derive(Clone, Debug)]
pub struct RunView {
    pub id: String,
    pub project_id: String,
    pub summary: Value,
    pub detail: Value,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Event {
    pub id: i64,
    pub run_id: String,
    pub kind: String,
    pub data: Value,
    pub created_at: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct EventPage {
    pub events: Vec<Event>,
    pub has_more: bool,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ApprovalDecisionInput {
    pub digest: String,
    pub approved: bool,
    #[serde(default)]
    pub comment: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunDecisionInput {
    pub digest: String,
    pub approved: bool,
    #[serde(default)]
    pub note: String,
}

/// Product commands only. Transport identities, claims and kernel versions
/// are resolved by the trusted backend, never accepted from HTTP clients.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunControl {
    pub action: ControlAction,
    #[serde(default)]
    pub task_key: Option<String>,
    #[serde(default)]
    pub agent_id: Option<String>,
    #[serde(default)]
    pub reason: String,
    #[serde(default)]
    pub prompt: Option<String>,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ControlAction {
    Pause,
    Resume,
    Cancel,
    Retry,
    Reassign,
    Continue,
    Steer,
}

#[derive(Debug, Error)]
pub enum BackendError {
    #[error("resource does not exist or is not accessible")]
    NotFound,
    #[error("backend temporarily unavailable")]
    Unavailable,
    #[error("invalid trusted host data")]
    InvalidData,
    #[error("current state conflicts with the requested action")]
    Conflict,
    #[error("account is read only")]
    Forbidden,
    #[error("operation is unavailable on this backend")]
    Unsupported,
    #[error("{0}")]
    UnsupportedConfiguration(&'static str),
}

impl From<ProductError> for BackendError {
    fn from(error: ProductError) -> Self {
        match error.status {
            403 => Self::Forbidden,
            404 => Self::NotFound,
            409 => Self::Conflict,
            400 | 422 => Self::InvalidData,
            _ => Self::Unavailable,
        }
    }
}

/// Synchronous operations run on Tokio's blocking pool, never its I/O workers.
/// Implementations must re-read project access on every call and scope event
/// queries by run *before* pagination. Do not wrap the global kernel event feed
/// without adding product ownership and authorization.
pub trait Backend: Send + Sync + 'static {
    fn product_request(
        &self,
        _user: &User,
        _method: &str,
        _path: &str,
        _query: &BTreeMap<String, String>,
        _body: &[u8],
    ) -> Result<ProductReply, ProductError> {
        Err(ProductError {
            status: 404,
            detail: "Not Found".into(),
        })
    }
    fn create_run(
        &self,
        _user: &User,
        _input: Value,
        _idempotency_key: Option<String>,
    ) -> Result<Value, BackendError> {
        Err(BackendError::Unsupported)
    }
    fn preview_run(&self, _user: &User, _input: Value) -> Result<Value, BackendError> {
        Err(BackendError::Unsupported)
    }
    fn capabilities(&self) -> Value {
        serde_json::json!({"api_version": 1, "read": {"runs": true, "events": true,
            "stream": true, "artifacts": false}, "write": {"create_run": false,
            "control_run": false, "decisions": false, "approvals": false},
            "durability": {"sessions": "process", "python_database": "read_only"}})
    }
    fn check_credentials(
        &self,
        username: &str,
        password: &str,
    ) -> Result<Option<User>, BackendError>;
    fn user(&self, user_id: &str) -> Result<Option<User>, BackendError>;
    fn runs(&self, user: &User, limit: usize) -> Result<Vec<Value>, BackendError>;
    fn run_statuses(&self, _user: &User) -> Result<BTreeMap<String, String>, BackendError> {
        Err(BackendError::Unsupported)
    }
    fn run(&self, user: &User, run_id: &str) -> Result<Value, BackendError>;
    fn event_notifications(&self) -> Option<tokio::sync::watch::Receiver<u64>> { None }
    fn events(
        &self,
        user: &User,
        run_id: &str,
        after: i64,
        limit: usize,
    ) -> Result<EventPage, BackendError>;
    fn approvals(&self, _user: &User, _run_id: &str, _limit: usize) -> Result<Value, BackendError> {
        Err(BackendError::Unsupported)
    }
    fn decide_approval(
        &self,
        _user: &User,
        _run_id: &str,
        _approval_id: &str,
        _input: ApprovalDecisionInput,
    ) -> Result<Value, BackendError> {
        Err(BackendError::Unsupported)
    }
    fn control_run(
        &self,
        _user: &User,
        _run_id: &str,
        _input: RunControl,
    ) -> Result<Value, BackendError> {
        Err(BackendError::Unsupported)
    }
    fn decide_run(
        &self,
        _user: &User,
        _run_id: &str,
        _phase: &str,
        _input: RunDecisionInput,
    ) -> Result<Value, BackendError> {
        Err(BackendError::Unsupported)
    }
}
