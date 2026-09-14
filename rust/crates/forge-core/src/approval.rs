//! Durable, one-shot decisions for an exact runtime callback. The trusted host
//! authenticates actors and verifies live callbacks before dispatch. A delivery
//! receipt records a transport write, never successful execution or acceptance.

use crate::{validate_identifier, DomainError, Run, RunExecution, RunStatus};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use thiserror::Error;

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(untagged)]
pub enum ApprovalRequestId {
    Integer(i64),
    String(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ApprovalKind {
    #[serde(rename = "item/commandExecution/requestApproval")]
    CommandExecution,
    #[serde(rename = "item/fileChange/requestApproval")]
    FileChange,
    #[serde(rename = "item/permissions/requestApproval")]
    Permissions,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ApprovalRequest {
    pub run_id: String,
    pub execution: RunExecution,
    pub request_id: ApprovalRequestId,
    pub item_id: String,
    pub kind: ApprovalKind,
    pub payload: Value,
}

impl ApprovalRequest {
    /// Require a frozen, running execution with a concrete thread and turn.
    /// Matching IDs do not authorize the caller or prove a callback is live.
    pub fn validate(&self, run: &Run) -> Result<(), DomainError> {
        self.validate_fields()?;
        if self.run_id != run.id
            || run.status != RunStatus::Running
            || run.execution_for(&self.execution.connection.worker_id) != Some(&self.execution)
            || !run
                .config
                .as_ref()
                .is_some_and(|config| config.permits_worker(&self.execution.connection.worker_id))
        {
            return Err(ApprovalError::StaleExecution.into());
        }
        Ok(())
    }

    pub fn digest(&self) -> Result<String, DomainError> {
        self.validate_fields()?;
        let value = serde_json::to_value(self).map_err(|_| ApprovalError::InvalidPayload)?;
        let bytes =
            serde_json::to_vec(&canonicalize(value)).map_err(|_| ApprovalError::InvalidPayload)?;
        Ok(format!("{:x}", Sha256::digest(bytes)))
    }

    fn validate_fields(&self) -> Result<(), DomainError> {
        validate_identifier(&self.run_id, "run_id")?;
        validate_identifier(&self.item_id, "item_id")?;
        validate_identifier(&self.execution.connection.worker_id, "worker_id")?;
        validate_identifier(&self.execution.connection.connection_id, "connection_id")?;
        if self.execution.connection.generation < 1 {
            return Err(ApprovalError::StaleExecution.into());
        }
        if let ApprovalRequestId::String(value) = &self.request_id {
            validate_identifier(value, "approval_request_id")?;
        }
        let thread = self.execution.thread_id.as_deref();
        let turn = self.execution.turn_id.as_deref();
        let (Some(thread), Some(turn)) = (thread, turn) else {
            return Err(ApprovalError::StaleExecution.into());
        };
        validate_identifier(thread, "thread_id")?;
        validate_identifier(turn, "turn_id")?;
        let payload = self
            .payload
            .as_object()
            .ok_or(ApprovalError::InvalidPayload)?;
        for (field, expected) in [
            ("threadId", thread),
            ("thread_id", thread),
            ("turnId", turn),
            ("turn_id", turn),
            ("itemId", self.item_id.as_str()),
            ("item_id", self.item_id.as_str()),
        ] {
            if payload
                .get(field)
                .is_some_and(|value| value.as_str() != Some(expected))
            {
                return Err(ApprovalError::PayloadScopeMismatch.into());
            }
        }
        if let Some(id) = self.payload.pointer("/item/id") {
            if id.as_str() != Some(self.item_id.as_str()) {
                return Err(ApprovalError::PayloadScopeMismatch.into());
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ApprovalStatus {
    Pending,
    Queued,
    Dispatching,
    Delivered,
    DeliveryUnknown,
    Expired,
}

impl ApprovalStatus {
    pub fn is_unresolved(self) -> bool {
        matches!(self, Self::Pending | Self::Queued | Self::Dispatching)
    }
}

/// Actor identity must come from the authenticated host, not an untrusted
/// request body. The decision grants only this operation or this turn.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ApprovalDecision {
    pub approved: bool,
    pub actor_id: String,
    pub comment: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Approval {
    pub id: String,
    pub request: ApprovalRequest,
    pub digest: String,
    pub status: ApprovalStatus,
    pub version: i64,
    pub decision: Option<ApprovalDecision>,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum ApprovalError {
    #[error("run has unresolved approval requests")]
    UnresolvedApprovals,
    #[error("approval cannot perform this transition from {0:?}")]
    InvalidState(ApprovalStatus),
    #[error("approval does not belong to the current running execution")]
    StaleExecution,
    #[error("approval payload must be an object")]
    InvalidPayload,
    #[error("approval payload identity disagrees with its execution scope")]
    PayloadScopeMismatch,
    #[error("approval digest does not match the immutable request")]
    DigestMismatch,
    #[error("approval requires a decision by an authenticated host actor")]
    MissingDecision,
    #[error("approval comment exceeds 4000 characters")]
    CommentTooLong,
    #[error("approval does not support a one-time grant")]
    UnsupportedGrant,
    #[error("file changes must be fully recorded before granting approval")]
    MissingFileChanges,
    #[error("requested permission scope cannot be safely recognized")]
    InvalidPermissions,
    #[error("approval timestamp predates its previous update")]
    InvalidTimestamp,
}

impl Approval {
    pub fn new(
        id: String,
        request: ApprovalRequest,
        run: &Run,
        now_ms: i64,
    ) -> Result<Self, DomainError> {
        validate_identifier(&id, "approval_id")?;
        request.validate(run)?;
        let digest = request.digest()?;
        Ok(Self {
            id,
            request,
            digest,
            status: ApprovalStatus::Pending,
            version: 0,
            decision: None,
            created_at_ms: now_ms,
            updated_at_ms: now_ms,
        })
    }

    pub fn decide(
        &mut self,
        expected_version: i64,
        expected_digest: &str,
        decision: ApprovalDecision,
        run: &Run,
        now_ms: i64,
    ) -> Result<(), DomainError> {
        let next_version = self.prepare_transition(expected_version, now_ms)?;
        self.require_state(ApprovalStatus::Pending)?;
        self.request.validate(run)?;
        if expected_digest != self.digest {
            return Err(ApprovalError::DigestMismatch.into());
        }
        validate_identifier(&decision.actor_id, "approval_actor_id")?;
        if decision.comment.chars().count() > 4000 {
            return Err(ApprovalError::CommentTooLong.into());
        }
        self.response_for(decision.approved)?;
        self.decision = Some(decision);
        self.advance(ApprovalStatus::Queued, next_version, now_ms);
        Ok(())
    }

    /// Persist Dispatching before the first write. The host must independently
    /// verify that the exact typed request ID is still a live server callback.
    pub fn begin_dispatch(
        &mut self,
        expected_version: i64,
        execution: &RunExecution,
        run: &Run,
        now_ms: i64,
    ) -> Result<(), DomainError> {
        let next_version = self.prepare_transition(expected_version, now_ms)?;
        self.require_state(ApprovalStatus::Queued)?;
        self.request.validate(run)?;
        if execution != &self.request.execution {
            return Err(ApprovalError::StaleExecution.into());
        }
        self.response()?;
        self.advance(ApprovalStatus::Dispatching, next_version, now_ms);
        Ok(())
    }

    /// `delivered` attests only that the transport write completed. Uncertain
    /// writes are terminal and cannot be queued on another connection.
    pub fn finish_dispatch(
        &mut self,
        expected_version: i64,
        delivered: bool,
        now_ms: i64,
    ) -> Result<(), DomainError> {
        let next_version = self.prepare_transition(expected_version, now_ms)?;
        self.require_state(ApprovalStatus::Dispatching)?;
        let status = if delivered {
            ApprovalStatus::Delivered
        } else {
            ApprovalStatus::DeliveryUnknown
        };
        self.advance(status, next_version, now_ms);
        Ok(())
    }

    /// The store invokes invalidation under its writer transaction when the
    /// original execution ends. Already terminal records remain unchanged.
    pub fn invalidate(&mut self, now_ms: i64) -> Result<bool, DomainError> {
        let status = match self.status {
            ApprovalStatus::Pending | ApprovalStatus::Queued => ApprovalStatus::Expired,
            ApprovalStatus::Dispatching => ApprovalStatus::DeliveryUnknown,
            _ => return Ok(false),
        };
        let next_version = self.prepare_transition(self.version, now_ms)?;
        self.advance(status, next_version, now_ms);
        Ok(true)
    }

    /// Return only once/turn grants, and only for a recorded decision waiting
    /// for delivery. This is not authorization to resend a terminal approval.
    pub fn response(&self) -> Result<Value, DomainError> {
        if !matches!(
            self.status,
            ApprovalStatus::Queued | ApprovalStatus::Dispatching
        ) {
            return Err(ApprovalError::InvalidState(self.status).into());
        }
        self.require_integrity()?;
        let decision = self
            .decision
            .as_ref()
            .ok_or(ApprovalError::MissingDecision)?;
        self.response_for(decision.approved)
    }

    fn response_for(&self, approved: bool) -> Result<Value, DomainError> {
        let payload = &self.request.payload;
        if self.request.kind == ApprovalKind::Permissions {
            if !approved {
                return Ok(json!({"permissions": {}, "scope": "turn"}));
            }
            let requested = payload
                .get("permissions")
                .and_then(Value::as_object)
                .ok_or(ApprovalError::InvalidPermissions)?;
            if requested
                .keys()
                .any(|key| key != "network" && key != "fileSystem")
                || requested
                    .values()
                    .any(|value| !value.is_null() && !value.is_object())
            {
                return Err(ApprovalError::InvalidPermissions.into());
            }
            return Ok(json!({"permissions": requested, "scope": "turn"}));
        }
        if approved {
            if let Some(available) = payload.get("availableDecisions") {
                if !available.as_array().is_some_and(|values| {
                    values.iter().any(|value| value.as_str() == Some("accept"))
                }) {
                    return Err(ApprovalError::UnsupportedGrant.into());
                }
            }
            if self.request.kind == ApprovalKind::FileChange {
                let item = payload
                    .get("item")
                    .and_then(Value::as_object)
                    .ok_or(ApprovalError::MissingFileChanges)?;
                if item.get("type").and_then(Value::as_str) != Some("fileChange")
                    || item.get("id").and_then(Value::as_str) != Some(self.request.item_id.as_str())
                    || !item
                        .get("changes")
                        .and_then(Value::as_array)
                        .is_some_and(|changes| {
                            !changes.is_empty()
                                && changes.iter().all(|change| {
                                    change
                                        .get("path")
                                        .and_then(Value::as_str)
                                        .is_some_and(|path| !path.trim().is_empty())
                                        && change.get("diff").is_some_and(Value::is_string)
                                        && matches!(
                                            change.pointer("/kind/type").and_then(Value::as_str),
                                            Some("add" | "delete" | "update")
                                        )
                                })
                        })
                {
                    return Err(ApprovalError::MissingFileChanges.into());
                }
            }
        }
        Ok(json!({"decision": if approved { "accept" } else { "decline" }}))
    }

    fn require_integrity(&self) -> Result<(), DomainError> {
        if self.request.digest()? != self.digest {
            return Err(ApprovalError::DigestMismatch.into());
        }
        Ok(())
    }

    fn prepare_transition(&self, expected: i64, now_ms: i64) -> Result<i64, DomainError> {
        if self.version != expected {
            return Err(DomainError::VersionConflict {
                expected,
                actual: self.version,
            });
        }
        self.require_integrity()?;
        if now_ms < self.updated_at_ms {
            return Err(ApprovalError::InvalidTimestamp.into());
        }
        self.version.checked_add(1).ok_or(DomainError::Overflow)
    }

    fn require_state(&self, expected: ApprovalStatus) -> Result<(), DomainError> {
        if self.status != expected {
            Err(ApprovalError::InvalidState(self.status).into())
        } else {
            Ok(())
        }
    }

    fn advance(&mut self, status: ApprovalStatus, version: i64, now_ms: i64) {
        self.status = status;
        self.version = version;
        self.updated_at_ms = now_ms;
    }
}

// Make payload digests stable even if another dependency enables preserve_order.
fn canonicalize(value: Value) -> Value {
    match value {
        Value::Object(object) => {
            let mut entries: Vec<_> = object.into_iter().collect();
            entries.sort_by(|left, right| left.0.cmp(&right.0));
            Value::Object(
                entries
                    .into_iter()
                    .map(|(key, value)| (key, canonicalize(value)))
                    .collect(),
            )
        }
        Value::Array(values) => Value::Array(values.into_iter().map(canonicalize).collect()),
        scalar => scalar,
    }
}
