//! Domain transitions for the first Rust-owned execution path.
//!
//! A submitted result is evidence awaiting product acceptance; it is not a claim
//! that the work is correct. Expired execution is unknown and cannot be reclaimed
//! automatically, since a worker may already have performed external effects.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use thiserror::Error;

mod run;
pub use run::*;
mod approval;
pub use approval::*;
mod provider;
pub use provider::*;

pub const MAX_LEASE_DURATION_MS: i64 = 900_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskStatus {
    Ready,
    Running,
    Submitted,
    OutcomeUnknown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Claim {
    pub worker_id: String,
    pub token: String,
    pub generation: i64,
    pub expires_at_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Task {
    pub id: String,
    pub run_id: String,
    pub status: TaskStatus,
    pub version: i64,
    pub attempt_generation: i64,
    pub claim: Option<Claim>,
    pub result: Option<Value>,
    /// Member selected by the coordinator. `None` preserves legacy tasks that
    /// were implicitly owned by the active run connection.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub assigned_worker_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub assigned_by: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    CreateRun {
        run_id: String,
    },
    /// Creates a run with an immutable, host-authorized execution snapshot.
    CreateBoundRun {
        run_id: String,
        config: Box<RunConfig>,
    },
    /// Retains the provider revision and verifies the frozen route against it.
    CreateProvisionedRun {
        run_id: String,
        config: Box<RunConfig>,
        provider_snapshot: Box<ProviderSnapshot>,
    },
    RecordApproval {
        approval_id: String,
        request: Box<ApprovalRequest>,
    },
    DecideApproval {
        approval_id: String,
        expected_version: i64,
        digest: String,
        approved: bool,
        actor_id: String,
        comment: String,
    },
    /// Commit this intent once before writing the response to the live transport.
    BeginApprovalDispatch {
        approval_id: String,
        expected_version: i64,
        execution: RunExecution,
    },
    FinishApprovalDispatch {
        approval_id: String,
        expected_version: i64,
        execution: RunExecution,
        delivered: bool,
    },
    ExpireApprovals {
        run_id: String,
        connection: RunConnection,
    },
    TransitionRun {
        run_id: String,
        expected_version: i64,
        transition: RunTransition,
    },
    /// Records host-observed metadata only; notification bodies are not durable.
    RecordRuntimeEvent {
        run_id: String,
        connection: RunConnection,
        sequence: u64,
        thread_id: Option<String>,
        turn_id: Option<String>,
        item_id: Option<String>,
        kind: String,
        terminal_status: Option<String>,
    },
    CreateTask {
        run_id: String,
        task_id: String,
    },
    CreateAssignedTask {
        run_id: String,
        task_id: String,
        coordinator_id: String,
        worker_id: String,
    },
    /// Assigns a ready task to a frozen team member. The store verifies that
    /// `coordinator_id` is the run coordinator and fences on task version.
    AssignTask {
        task_id: String,
        coordinator_id: String,
        worker_id: String,
        expected_version: i64,
    },
    ClaimTask {
        task_id: String,
        worker_id: String,
        expected_version: i64,
        lease_duration_ms: i64,
    },
    RenewClaim {
        task_id: String,
        claim_token: String,
        attempt_generation: i64,
        lease_duration_ms: i64,
    },
    ReleaseTask {
        task_id: String,
        claim_token: String,
        attempt_generation: i64,
    },
    SubmitTask {
        task_id: String,
        claim_token: String,
        attempt_generation: i64,
        result: Value,
    },
    MarkOutcomeUnknown {
        task_id: String,
        expected_version: i64,
    },
    /// A trusted coordinator explicitly attests that retry is safe. The host
    /// must authorize this command; evidence is an audit reference, not a proof.
    ReconcileTask {
        task_id: String,
        expected_version: i64,
        evidence_ref: String,
    },
    ReopenTask {
        task_id: String,
        expected_version: i64,
        evidence_ref: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", content = "value", rename_all = "snake_case")]
pub enum CommandResult {
    Run(Run),
    Task(Task),
    Approval(Box<Approval>),
    Approvals(Vec<Approval>),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Receipt {
    pub command_id: String,
    pub event_id: i64,
    pub result: CommandResult,
}

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum DomainError {
    #[error(transparent)]
    Run(#[from] RunError),
    #[error(transparent)]
    Provider(#[from] ProviderError),
    #[error(transparent)]
    Approval(#[from] ApprovalError),
    #[error("{0} must not be empty")]
    EmptyIdentifier(&'static str),
    #[error("state version mismatch: expected {expected}, actual {actual}")]
    VersionConflict { expected: i64, actual: i64 },
    #[error("task cannot perform this transition from {0:?}")]
    InvalidState(TaskStatus),
    #[error("claim token or attempt generation is stale")]
    StaleClaim,
    #[error("claim lease has expired; execution must be reconciled")]
    LeaseExpired,
    #[error("claim lease has not expired")]
    LeaseActive,
    #[error("lease duration must be between 1 and {MAX_LEASE_DURATION_MS} milliseconds")]
    InvalidLeaseDuration,
    #[error("state counter or time overflow")]
    Overflow,
}

pub fn validate_identifier(value: &str, name: &'static str) -> Result<(), DomainError> {
    if value.trim().is_empty() {
        Err(DomainError::EmptyIdentifier(name))
    } else {
        Ok(())
    }
}

impl Task {
    pub fn new(id: String, run_id: String) -> Result<Self, DomainError> {
        validate_identifier(&id, "task_id")?;
        validate_identifier(&run_id, "run_id")?;
        Ok(Self {
            id,
            run_id,
            status: TaskStatus::Ready,
            version: 0,
            attempt_generation: 0,
            claim: None,
            result: None,
            assigned_worker_id: None,
            assigned_by: None,
        })
    }

    pub fn assign(
        &mut self,
        coordinator_id: String,
        worker_id: String,
        expected_version: i64,
    ) -> Result<(), DomainError> {
        self.require_version(expected_version)?;
        if self.status != TaskStatus::Ready {
            return Err(DomainError::InvalidState(self.status));
        }
        validate_identifier(&coordinator_id, "coordinator_id")?;
        validate_identifier(&worker_id, "worker_id")?;
        let version = self.version.checked_add(1).ok_or(DomainError::Overflow)?;
        self.assigned_by = Some(coordinator_id);
        self.assigned_worker_id = Some(worker_id);
        self.version = version;
        Ok(())
    }

    pub fn claim(
        &mut self,
        worker_id: String,
        token: String,
        expected_version: i64,
        now_ms: i64,
        lease_duration_ms: i64,
    ) -> Result<(), DomainError> {
        self.require_version(expected_version)?;
        if self.status != TaskStatus::Ready {
            return Err(DomainError::InvalidState(self.status));
        }
        validate_identifier(&worker_id, "worker_id")?;
        if self
            .assigned_worker_id
            .as_deref()
            .is_some_and(|assigned| assigned != worker_id)
        {
            return Err(RunError::UnauthorizedWorker.into());
        }
        validate_identifier(&token, "claim_token")?;
        if !(1..=MAX_LEASE_DURATION_MS).contains(&lease_duration_ms) {
            return Err(DomainError::InvalidLeaseDuration);
        }
        let expires_at_ms = now_ms
            .checked_add(lease_duration_ms)
            .ok_or(DomainError::Overflow)?;
        let generation = self
            .attempt_generation
            .checked_add(1)
            .ok_or(DomainError::Overflow)?;
        let version = self.version.checked_add(1).ok_or(DomainError::Overflow)?;
        self.claim = Some(Claim {
            worker_id,
            token,
            generation,
            expires_at_ms,
        });
        self.attempt_generation = generation;
        self.status = TaskStatus::Running;
        self.version = version;
        Ok(())
    }

    pub fn release(
        &mut self,
        token: &str,
        generation: i64,
        now_ms: i64,
    ) -> Result<(), DomainError> {
        self.require_claim(token, generation, now_ms)?;
        let version = self.version.checked_add(1).ok_or(DomainError::Overflow)?;
        self.claim = None;
        self.status = TaskStatus::Ready;
        self.version = version;
        Ok(())
    }

    pub fn renew_claim(
        &mut self,
        token: &str,
        generation: i64,
        now_ms: i64,
        lease_duration_ms: i64,
    ) -> Result<(), DomainError> {
        self.require_claim(token, generation, now_ms)?;
        if !(1..=MAX_LEASE_DURATION_MS).contains(&lease_duration_ms) {
            return Err(DomainError::InvalidLeaseDuration);
        }
        let requested_expiry = now_ms
            .checked_add(lease_duration_ms)
            .ok_or(DomainError::Overflow)?;
        let version = self.version.checked_add(1).ok_or(DomainError::Overflow)?;
        let claim = self.claim.as_mut().ok_or(DomainError::StaleClaim)?;
        // Renew from host time and never shorten an already granted lease.
        claim.expires_at_ms = claim.expires_at_ms.max(requested_expiry);
        self.version = version;
        Ok(())
    }

    pub fn submit(
        &mut self,
        token: &str,
        generation: i64,
        now_ms: i64,
        result: Value,
    ) -> Result<(), DomainError> {
        self.require_claim(token, generation, now_ms)?;
        let version = self.version.checked_add(1).ok_or(DomainError::Overflow)?;
        self.claim = None;
        self.result = Some(result);
        self.status = TaskStatus::Submitted;
        self.version = version;
        Ok(())
    }

    pub fn mark_outcome_unknown(
        &mut self,
        expected_version: i64,
        now_ms: i64,
    ) -> Result<(), DomainError> {
        self.require_version(expected_version)?;
        if self.status != TaskStatus::Running {
            return Err(DomainError::InvalidState(self.status));
        }
        if self
            .claim
            .as_ref()
            .ok_or(DomainError::StaleClaim)?
            .expires_at_ms
            > now_ms
        {
            return Err(DomainError::LeaseActive);
        }
        let version = self.version.checked_add(1).ok_or(DomainError::Overflow)?;
        self.claim = None;
        self.status = TaskStatus::OutcomeUnknown;
        self.version = version;
        Ok(())
    }

    pub fn reconcile(
        &mut self,
        expected_version: i64,
        evidence_ref: &str,
    ) -> Result<(), DomainError> {
        self.require_version(expected_version)?;
        if self.status != TaskStatus::OutcomeUnknown {
            return Err(DomainError::InvalidState(self.status));
        }
        validate_identifier(evidence_ref, "evidence_ref")?;
        let version = self.version.checked_add(1).ok_or(DomainError::Overflow)?;
        self.claim = None;
        self.result = None;
        self.status = TaskStatus::Ready;
        self.version = version;
        // A subsequent claim increments generation, fencing the prior worker.
        Ok(())
    }

    pub fn reopen(&mut self, expected_version: i64, evidence_ref: &str) -> Result<(), DomainError> {
        self.require_version(expected_version)?;
        if self.status != TaskStatus::Submitted {
            return Err(DomainError::InvalidState(self.status));
        }
        validate_identifier(evidence_ref, "evidence_ref")?;
        self.version = self.version.checked_add(1).ok_or(DomainError::Overflow)?;
        self.result = None;
        self.status = TaskStatus::Ready;
        Ok(())
    }

    fn require_version(&self, expected: i64) -> Result<(), DomainError> {
        if self.version != expected {
            Err(DomainError::VersionConflict {
                expected,
                actual: self.version,
            })
        } else {
            Ok(())
        }
    }

    fn require_claim(&self, token: &str, generation: i64, now_ms: i64) -> Result<(), DomainError> {
        if self.status != TaskStatus::Running {
            return Err(DomainError::InvalidState(self.status));
        }
        let claim = self.claim.as_ref().ok_or(DomainError::StaleClaim)?;
        if claim.token != token || claim.generation != generation {
            return Err(DomainError::StaleClaim);
        }
        if claim.expires_at_ms <= now_ms {
            return Err(DomainError::LeaseExpired);
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn expired_execution_requires_reconciliation_and_is_not_reclaimable() {
        let mut task = Task::new("t".into(), "r".into()).unwrap();
        task.claim("w".into(), "old".into(), 0, 1_000, 100).unwrap();
        assert_eq!(
            task.submit("old", 1, 1_100, Value::Null),
            Err(DomainError::LeaseExpired)
        );
        task.mark_outcome_unknown(1, 1_100).unwrap();
        assert_eq!(
            task.claim("w2".into(), "new".into(), 2, 1_101, 100),
            Err(DomainError::InvalidState(TaskStatus::OutcomeUnknown))
        );
    }

    #[test]
    fn released_claim_cannot_submit_into_a_new_attempt() {
        let mut task = Task::new("t".into(), "r".into()).unwrap();
        task.claim("w".into(), "old".into(), 0, 100, 100).unwrap();
        task.release("old", 1, 110).unwrap();
        task.claim("w".into(), "new".into(), 2, 120, 100).unwrap();
        assert_eq!(
            task.submit("old", 1, 130, Value::Null),
            Err(DomainError::StaleClaim)
        );
        assert_eq!(
            task.submit("new", 1, 130, Value::Null),
            Err(DomainError::StaleClaim)
        );
        task.submit("new", 2, 130, Value::Null).unwrap();
        assert_eq!(task.status, TaskStatus::Submitted);
    }
}
