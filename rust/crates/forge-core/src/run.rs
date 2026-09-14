//! Frozen run configuration and execution facts, with no process or storage I/O.
//! Hosts authenticate callers and verify referenced assets/evidence. Transitions
//! record those decisions; an evidence reference alone is not authorization.

use crate::{validate_identifier, DomainError};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    collections::{BTreeMap, HashSet},
    path::PathBuf,
};
use thiserror::Error;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RunOwner {
    #[default]
    Rust,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RunStatus {
    Pending,
    Starting,
    Running,
    Paused,
    OutcomeUnknown,
    Submitted,
    Integrated,
    Accepted,
    Cancelled,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PermissionProfile {
    Ask,
    Auto,
    Full,
}

/// The immutable gateway route selected by the trusted host. Credentials are
/// references, never raw secrets. The upstream protocol can differ from the
/// Responses protocol used between Codex and the gateway. The host validates
/// endpoint policy, asset revisions, capability evidence and credential access.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RouteBinding {
    pub provider_id: String,
    pub provider_revision_id: String,
    pub model_id: String,
    pub upstream_protocol: String,
    pub gateway_route_id: String,
    pub responses_base_url: String,
    pub config_digest: String,
    pub credential_ref: String,
    pub capability_evidence_ref: String,
    pub gateway_version: String,
}

impl RouteBinding {
    pub fn validate(&self) -> Result<(), DomainError> {
        for (value, name) in [
            (&self.provider_id, "provider_id"),
            (&self.provider_revision_id, "provider_revision_id"),
            (&self.model_id, "model_id"),
            (&self.upstream_protocol, "upstream_protocol"),
            (&self.gateway_route_id, "gateway_route_id"),
            (&self.responses_base_url, "responses_base_url"),
            (&self.config_digest, "config_digest"),
            (&self.credential_ref, "credential_ref"),
            (&self.capability_evidence_ref, "capability_evidence_ref"),
            (&self.gateway_version, "gateway_version"),
        ] {
            validate_identifier(value, name)?;
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunMember {
    pub worker_id: String,
    /// Reference to a retained, immutable member configuration.
    pub config_revision: String,
    /// Public, frozen capability metadata. These fields intentionally default
    /// for pre-team snapshots written before the team runtime was introduced.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub role: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub instructions: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub tools: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub skills: Vec<Value>,
    /// A redacted summary of runtime permissions, never credentials.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub permission_summary: Option<Value>,
    #[serde(default, skip_serializing_if = "is_false")]
    pub is_coordinator: bool,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub communication_recipients: Vec<String>,
}

fn is_false(value: &bool) -> bool {
    !*value
}

/// The shared route and permission profile of a frozen run.
/// Referenced configurations must remain available for the lifetime of the run.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunConfig {
    pub project_id: String,
    pub owner_id: String,
    pub members: Vec<RunMember>,
    pub permission_profile: PermissionProfile,
    pub working_directory: PathBuf,
    pub runtime_home: PathBuf,
    pub runtime_version: String,
    pub route_binding: RouteBinding,
}

impl RunConfig {
    pub fn validate(&self) -> Result<(), DomainError> {
        validate_identifier(&self.project_id, "project_id")?;
        validate_identifier(&self.owner_id, "owner_id")?;
        validate_identifier(&self.runtime_version, "runtime_version")?;
        self.route_binding.validate()?;
        if !self.working_directory.is_absolute() || !self.runtime_home.is_absolute() {
            return Err(RunError::RelativePath.into());
        }
        if self.members.is_empty() {
            return Err(RunError::EmptyMembers.into());
        }
        let mut workers = HashSet::new();
        let mut coordinators = 0;
        for member in &self.members {
            validate_identifier(&member.worker_id, "worker_id")?;
            validate_identifier(&member.config_revision, "config_revision")?;
            if !workers.insert(&member.worker_id) {
                return Err(RunError::DuplicateMember.into());
            }
            if member.is_coordinator {
                coordinators += 1;
            }
            if member
                .communication_recipients
                .iter()
                .any(|recipient| !self.permits_worker(recipient))
            {
                return Err(RunError::UnauthorizedWorker.into());
            }
        }
        if coordinators > 1 {
            return Err(RunError::MultipleCoordinators.into());
        }
        Ok(())
    }

    pub fn permits_worker(&self, worker_id: &str) -> bool {
        self.members
            .iter()
            .any(|member| member.worker_id == worker_id)
    }

    /// Returns the explicit coordinator, or the first member for legacy
    /// snapshots that had no coordinator marker.
    pub fn coordinator_id(&self) -> Option<&str> {
        self.members
            .iter()
            .find(|member| member.is_coordinator)
            .or_else(|| self.members.first())
            .map(|member| member.worker_id.as_str())
    }

    pub fn member(&self, worker_id: &str) -> Option<&RunMember> {
        self.members
            .iter()
            .find(|member| member.worker_id == worker_id)
    }
}

/// Connection identity scopes notifications and approval requests. A resumed
/// connection receives a new generation, even when it resumes the same thread.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunConnection {
    pub worker_id: String,
    pub connection_id: String,
    pub generation: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunExecution {
    pub connection: RunConnection,
    pub thread_id: Option<String>,
    pub turn_id: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MemberSessionStatus {
    Starting,
    Running,
    Stopped,
    OutcomeUnknown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MemberSession {
    pub execution: RunExecution,
    pub status: MemberSessionStatus,
}

/// Safe notification facts. Raw payloads, approval tokens and item bodies never
/// cross this boundary. Sequence numbers are scoped to a runtime connection.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunRuntimeEvent {
    pub connection: RunConnection,
    pub sequence: u64,
    pub thread_id: Option<String>,
    pub turn_id: Option<String>,
    pub item_id: Option<String>,
    pub kind: String,
    pub terminal_status: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Run {
    pub id: String,
    pub status: RunStatus,
    #[serde(default)]
    pub owner: RunOwner,
    #[serde(default)]
    pub version: i64,
    /// None only for the stage-one kernel records; these cannot be started.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub config: Option<Box<RunConfig>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub execution: Option<RunExecution>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub sessions: BTreeMap<String, MemberSession>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum RunTransition {
    /// Commit before starting/resuming the external runtime. A lost response
    /// after this intent must be reconciled; it must not be executed again.
    Begin {
        worker_id: String,
        connection_id: String,
    },
    BindThread {
        connection: RunConnection,
        thread_id: String,
    },
    BindTurn {
        connection: RunConnection,
        turn_id: String,
    },
    BeginMember {
        worker_id: String,
        connection_id: String,
    },
    BindMemberThread {
        connection: RunConnection,
        thread_id: String,
    },
    BindMemberTurn {
        connection: RunConnection,
        turn_id: String,
    },
    EndMember {
        connection: RunConnection,
        evidence_ref: String,
    },
    MarkMemberOutcomeUnknown {
        connection: RunConnection,
    },
    ReconcileMember {
        worker_id: String,
        evidence_ref: String,
    },
    /// A trusted host attests that the execution has stopped safely.
    Pause {
        connection: RunConnection,
        evidence_ref: String,
    },
    MarkOutcomeUnknown {
        connection: RunConnection,
    },
    /// Verify process, workspace and route before authorizing a later Begin.
    Reconcile {
        evidence_ref: String,
    },
    /// A completed Codex turn is only a candidate result.
    Submit {
        execution: RunExecution,
        result: Value,
    },
    Integrate {
        evidence_ref: String,
    },
    Accept {
        evidence_ref: String,
    },
    Cancel {
        evidence_ref: String,
    },
    Reopen {
        evidence_ref: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum RunError {
    #[error("run has no frozen configuration")]
    Unbound,
    #[error("run cannot perform this transition from {0:?}")]
    InvalidState(RunStatus),
    #[error("run connection or execution identity is stale")]
    StaleExecution,
    #[error("run must resume its original thread")]
    ThreadMismatch,
    #[error("worker is outside the frozen run membership")]
    UnauthorizedWorker,
    #[error("run directories must be absolute")]
    RelativePath,
    #[error("run requires at least one frozen member")]
    EmptyMembers,
    #[error("run member identifiers must be unique")]
    DuplicateMember,
    #[error("run has more than one coordinator")]
    MultipleCoordinators,
    #[error("run has unresolved tasks")]
    UnresolvedTasks,
    #[error("runtime event terminal status or scope is invalid")]
    InvalidRuntimeEvent,
    #[error("run has unresolved member sessions")]
    UnresolvedMemberSessions,
}

impl Run {
    pub fn new(id: String) -> Result<Self, DomainError> {
        validate_identifier(&id, "run_id")?;
        Ok(Self {
            id,
            status: RunStatus::Pending,
            owner: RunOwner::Rust,
            version: 0,
            config: None,
            execution: None,
            sessions: BTreeMap::new(),
            result: None,
        })
    }

    pub fn with_config(id: String, config: RunConfig) -> Result<Self, DomainError> {
        config.validate()?;
        let mut run = Self::new(id)?;
        run.config = Some(Box::new(config));
        Ok(run)
    }

    /// Applies a complete transition or leaves the snapshot unchanged.
    pub fn transition(
        &mut self,
        expected_version: i64,
        transition: RunTransition,
    ) -> Result<(), DomainError> {
        if expected_version != self.version {
            return Err(DomainError::VersionConflict {
                expected: expected_version,
                actual: self.version,
            });
        }
        let next_version = self.version.checked_add(1).ok_or(DomainError::Overflow)?;
        let mut next = self.clone();
        next.apply(transition)?;
        next.version = next_version;
        *self = next;
        Ok(())
    }

    fn apply(&mut self, transition: RunTransition) -> Result<(), DomainError> {
        let config = self.config.as_ref().ok_or(RunError::Unbound)?;
        match transition {
            RunTransition::Begin {
                worker_id,
                connection_id,
            } => {
                self.require_state(&[RunStatus::Pending, RunStatus::Paused])?;
                self.require_resolved_sessions()?;
                validate_identifier(&connection_id, "connection_id")?;
                if self
                    .sessions
                    .values()
                    .any(|session| session.execution.connection.connection_id == connection_id)
                {
                    return Err(RunError::StaleExecution.into());
                }
                if !config.permits_worker(&worker_id) {
                    return Err(RunError::UnauthorizedWorker.into());
                }
                let generation = match &self.execution {
                    Some(previous) => {
                        if previous.connection.connection_id == connection_id {
                            return Err(RunError::StaleExecution.into());
                        }
                        previous
                            .connection
                            .generation
                            .checked_add(1)
                            .ok_or(DomainError::Overflow)?
                    }
                    None => 1,
                };
                let thread_id = self
                    .execution
                    .as_ref()
                    .and_then(|execution| execution.thread_id.clone());
                self.execution = Some(RunExecution {
                    connection: RunConnection {
                        worker_id,
                        connection_id,
                        generation,
                    },
                    thread_id,
                    turn_id: None,
                });
                self.status = RunStatus::Starting;
            }
            RunTransition::BindThread {
                connection,
                thread_id,
            } => {
                self.require_state(&[RunStatus::Starting])?;
                self.require_leader_connection(&connection)?;
                validate_identifier(&thread_id, "thread_id")?;
                let execution = self.execution.as_mut().ok_or(RunError::StaleExecution)?;
                if execution
                    .thread_id
                    .as_ref()
                    .is_some_and(|id| id != &thread_id)
                {
                    return Err(RunError::ThreadMismatch.into());
                }
                execution.thread_id = Some(thread_id);
            }
            RunTransition::BindTurn {
                connection,
                turn_id,
            } => {
                self.require_state(&[RunStatus::Starting, RunStatus::Running])?;
                // The leader may handle a bounded teammate reply while other
                // members run. Final submission still requires all sessions to
                // be stopped; the store also fences the preceding leader turn.
                self.require_leader_connection(&connection)?;
                validate_identifier(&turn_id, "turn_id")?;
                let execution = self.execution.as_mut().ok_or(RunError::StaleExecution)?;
                if execution.thread_id.is_none() {
                    return Err(RunError::StaleExecution.into());
                }
                if execution.turn_id.as_ref() == Some(&turn_id) {
                    return Err(RunError::StaleExecution.into());
                }
                execution.turn_id = Some(turn_id);
                self.status = RunStatus::Running;
            }
            RunTransition::BeginMember {
                worker_id,
                connection_id,
            } => {
                self.require_state(&[RunStatus::Running])?;
                validate_identifier(&connection_id, "connection_id")?;
                if !config.permits_worker(&worker_id)
                    || self
                        .execution
                        .as_ref()
                        .is_some_and(|execution| execution.connection.worker_id == worker_id)
                {
                    return Err(RunError::UnauthorizedWorker.into());
                }
                if self
                    .execution
                    .as_ref()
                    .is_some_and(|execution| execution.connection.connection_id == connection_id)
                    || self
                        .sessions
                        .values()
                        .any(|session| session.execution.connection.connection_id == connection_id)
                {
                    return Err(RunError::StaleExecution.into());
                }
                let previous = self.sessions.get(&worker_id);
                if previous.is_some_and(|session| session.status != MemberSessionStatus::Stopped) {
                    return Err(RunError::UnresolvedMemberSessions.into());
                }
                let generation = previous
                    .map(|session| {
                        session
                            .execution
                            .connection
                            .generation
                            .checked_add(1)
                            .ok_or(DomainError::Overflow)
                    })
                    .transpose()?
                    .unwrap_or(1);
                let thread_id = previous.and_then(|session| session.execution.thread_id.clone());
                self.sessions.insert(
                    worker_id.clone(),
                    MemberSession {
                        execution: RunExecution {
                            connection: RunConnection {
                                worker_id,
                                connection_id,
                                generation,
                            },
                            thread_id,
                            turn_id: None,
                        },
                        status: MemberSessionStatus::Starting,
                    },
                );
            }
            RunTransition::BindMemberThread {
                connection,
                thread_id,
            } => {
                self.require_state(&[RunStatus::Running])?;
                self.require_member_connection(&connection)?;
                validate_identifier(&thread_id, "thread_id")?;
                let session = self
                    .sessions
                    .get_mut(&connection.worker_id)
                    .ok_or(RunError::StaleExecution)?;
                if session.status != MemberSessionStatus::Starting {
                    return Err(RunError::StaleExecution.into());
                }
                if session
                    .execution
                    .thread_id
                    .as_ref()
                    .is_some_and(|id| id != &thread_id)
                {
                    return Err(RunError::ThreadMismatch.into());
                }
                session.execution.thread_id = Some(thread_id);
            }
            RunTransition::BindMemberTurn {
                connection,
                turn_id,
            } => {
                self.require_state(&[RunStatus::Running])?;
                self.require_member_connection(&connection)?;
                validate_identifier(&turn_id, "turn_id")?;
                let session = self
                    .sessions
                    .get_mut(&connection.worker_id)
                    .ok_or(RunError::StaleExecution)?;
                if session.status != MemberSessionStatus::Starting
                    || session.execution.thread_id.is_none()
                {
                    return Err(RunError::StaleExecution.into());
                }
                session.execution.turn_id = Some(turn_id);
                session.status = MemberSessionStatus::Running;
            }
            RunTransition::EndMember {
                connection,
                evidence_ref,
            } => {
                self.require_state(&[RunStatus::Running])?;
                self.require_member_connection(&connection)?;
                validate_identifier(&evidence_ref, "evidence_ref")?;
                self.sessions
                    .get_mut(&connection.worker_id)
                    .ok_or(RunError::StaleExecution)?
                    .status = MemberSessionStatus::Stopped;
            }
            RunTransition::MarkMemberOutcomeUnknown { connection } => {
                self.require_state(&[RunStatus::Running])?;
                self.require_member_connection(&connection)?;
                self.sessions
                    .get_mut(&connection.worker_id)
                    .ok_or(RunError::StaleExecution)?
                    .status = MemberSessionStatus::OutcomeUnknown;
            }
            RunTransition::ReconcileMember {
                worker_id,
                evidence_ref,
            } => {
                self.require_state(&[
                    RunStatus::Running,
                    RunStatus::Paused,
                    RunStatus::OutcomeUnknown,
                ])?;
                validate_identifier(&evidence_ref, "evidence_ref")?;
                let session = self
                    .sessions
                    .get_mut(&worker_id)
                    .ok_or(RunError::StaleExecution)?;
                if session.status != MemberSessionStatus::OutcomeUnknown {
                    return Err(RunError::StaleExecution.into());
                }
                session.status = MemberSessionStatus::Stopped;
            }
            RunTransition::Pause {
                connection,
                evidence_ref,
            } => {
                self.require_state(&[RunStatus::Starting, RunStatus::Running])?;
                self.require_leader_connection(&connection)?;
                validate_identifier(&evidence_ref, "evidence_ref")?;
                self.invalidate_active_sessions();
                self.status = RunStatus::Paused;
            }
            RunTransition::MarkOutcomeUnknown { connection } => {
                self.require_state(&[RunStatus::Starting, RunStatus::Running])?;
                self.require_leader_connection(&connection)?;
                self.invalidate_active_sessions();
                self.status = RunStatus::OutcomeUnknown;
            }
            RunTransition::Reconcile { evidence_ref } => {
                self.require_state(&[RunStatus::OutcomeUnknown])?;
                validate_identifier(&evidence_ref, "evidence_ref")?;
                self.status = RunStatus::Paused;
            }
            RunTransition::Submit { execution, result } => {
                self.require_state(&[RunStatus::Running])?;
                self.require_resolved_sessions()?;
                if self.execution.as_ref() != Some(&execution) {
                    return Err(RunError::StaleExecution.into());
                }
                self.result = Some(result);
                self.status = RunStatus::Submitted;
            }
            RunTransition::Integrate { evidence_ref } => {
                self.require_state(&[RunStatus::Submitted])?;
                validate_identifier(&evidence_ref, "evidence_ref")?;
                self.status = RunStatus::Integrated;
            }
            RunTransition::Accept { evidence_ref } => {
                self.require_state(&[RunStatus::Integrated])?;
                validate_identifier(&evidence_ref, "evidence_ref")?;
                self.status = RunStatus::Accepted;
            }
            RunTransition::Cancel { evidence_ref } => {
                self.require_state(&[RunStatus::Pending, RunStatus::Paused])?;
                validate_identifier(&evidence_ref, "evidence_ref")?;
                self.status = RunStatus::Cancelled;
            }
            RunTransition::Reopen { evidence_ref } => {
                self.require_state(&[RunStatus::Submitted, RunStatus::Integrated])?;
                validate_identifier(&evidence_ref, "evidence_ref")?;
                self.status = RunStatus::Paused;
                self.result = None;
            }
        }
        Ok(())
    }

    fn require_state(&self, permitted: &[RunStatus]) -> Result<(), DomainError> {
        if !permitted.contains(&self.status) {
            return Err(RunError::InvalidState(self.status).into());
        }
        Ok(())
    }

    fn require_resolved_sessions(&self) -> Result<(), DomainError> {
        if self
            .sessions
            .values()
            .any(|session| session.status != MemberSessionStatus::Stopped)
        {
            return Err(RunError::UnresolvedMemberSessions.into());
        }
        Ok(())
    }

    fn invalidate_active_sessions(&mut self) {
        for session in self.sessions.values_mut() {
            if matches!(
                session.status,
                MemberSessionStatus::Starting | MemberSessionStatus::Running
            ) {
                session.status = MemberSessionStatus::OutcomeUnknown;
            }
        }
    }

    fn require_leader_connection(&self, connection: &RunConnection) -> Result<(), DomainError> {
        if self
            .execution
            .as_ref()
            .map(|execution| &execution.connection)
            != Some(connection)
        {
            return Err(RunError::StaleExecution.into());
        }
        Ok(())
    }

    fn require_member_connection(&self, connection: &RunConnection) -> Result<(), DomainError> {
        let session = self
            .sessions
            .get(&connection.worker_id)
            .ok_or(RunError::StaleExecution)?;
        if &session.execution.connection != connection
            || !matches!(
                session.status,
                MemberSessionStatus::Starting | MemberSessionStatus::Running
            )
        {
            return Err(RunError::StaleExecution.into());
        }
        Ok(())
    }

    /// The leader keeps its original execution record. Member callbacks only
    /// match a currently active member session, never a completed generation.
    pub fn execution_for(&self, worker_id: &str) -> Option<&RunExecution> {
        if let Some(execution) = &self.execution {
            if execution.connection.worker_id == worker_id {
                return Some(execution);
            }
        }
        self.sessions
            .get(worker_id)
            .filter(|session| {
                matches!(
                    session.status,
                    MemberSessionStatus::Starting | MemberSessionStatus::Running
                )
            })
            .map(|session| &session.execution)
    }

    /// Validate approval/event connection scope without mutating the run. The
    /// transport still owns request IDs; this does not authorize an approval.
    pub fn require_connection(&self, connection: &RunConnection) -> Result<(), DomainError> {
        self.require_state(&[RunStatus::Starting, RunStatus::Running])?;
        if self
            .execution_for(&connection.worker_id)
            .map(|execution| &execution.connection)
            != Some(connection)
        {
            return Err(RunError::StaleExecution.into());
        }
        Ok(())
    }

    /// Checks observed scope without advancing execution or the run version.
    /// Notifications can omit thread/turn scope, but any supplied scope must
    /// already be bound. A turn or item cannot be attached to a global event.
    pub fn require_runtime_event(&self, event: &RunRuntimeEvent) -> Result<(), DomainError> {
        self.config.as_ref().ok_or(RunError::Unbound)?;
        self.require_connection(&event.connection)?;
        validate_identifier(&event.kind, "runtime_event_kind")?;
        i64::try_from(event.sequence).map_err(|_| DomainError::Overflow)?;
        for (value, name) in [
            (&event.thread_id, "thread_id"),
            (&event.turn_id, "turn_id"),
            (&event.item_id, "item_id"),
        ] {
            if let Some(value) = value {
                validate_identifier(value, name)?;
            }
        }
        let execution = self
            .execution_for(&event.connection.worker_id)
            .ok_or(RunError::StaleExecution)?;
        if event.thread_id.is_some() && event.thread_id != execution.thread_id {
            return Err(RunError::ThreadMismatch.into());
        }
        if event.turn_id.is_some()
            && (event.thread_id.is_none() || event.turn_id != execution.turn_id)
        {
            return Err(RunError::StaleExecution.into());
        }
        if event.item_id.is_some() && event.turn_id.is_none() {
            return Err(RunError::InvalidRuntimeEvent.into());
        }
        if let Some(status) = event.terminal_status.as_deref() {
            let consistent = match event.kind.as_str() {
                "turn/completed" => matches!(status, "completed" | "interrupted" | "failed"),
                "turn/interrupted" => status == "interrupted",
                "turn/failed" => status == "failed",
                _ => false,
            };
            if !consistent || event.turn_id.is_none() {
                return Err(RunError::InvalidRuntimeEvent.into());
            }
        }
        Ok(())
    }
}
