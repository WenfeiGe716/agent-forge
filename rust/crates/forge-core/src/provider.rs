//! Retained provider revisions contain references, never resolved credentials.
//! Endpoint policy and access to referenced secrets remain host responsibilities.

use crate::{validate_identifier, DomainError, PermissionProfile, RouteBinding};
use serde::{Deserialize, Serialize};
use std::{cell::Cell, collections::HashSet};
use thiserror::Error;
use url::{SyntaxViolation, Url};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderSnapshot {
    pub provider_id: String,
    pub provider_revision_id: String,
    pub upstream_base_url: String,
    pub credential_ref: String,
    pub config_digest: String,
    pub models: Vec<ProviderModelSnapshot>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderModelSnapshot {
    pub model_id: String,
    pub upstream_protocol: String,
    pub capability_evidence_ref: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum ProviderError {
    #[error("provider requires at least one model")]
    EmptyModels,
    #[error("provider model and upstream protocol pairs must be unique")]
    DuplicateModel,
    #[error("provider upstream URL requires an HTTP(S) host without userinfo, query or fragment")]
    InvalidUpstreamBaseUrl,
    #[error("route binding does not match the retained provider snapshot")]
    RouteMismatch,
}

impl ProviderSnapshot {
    pub fn validate(&self) -> Result<(), DomainError> {
        for (value, name) in [
            (&self.provider_id, "provider_id"),
            (&self.provider_revision_id, "provider_revision_id"),
            (&self.upstream_base_url, "upstream_base_url"),
            (&self.credential_ref, "credential_ref"),
            (&self.config_digest, "config_digest"),
        ] {
            validate_identifier(value, name)?;
        }
        // The parser removes empty userinfo, so retain its syntax signal too.
        let has_userinfo = Cell::new(false);
        let on_violation = |violation| {
            if violation == SyntaxViolation::EmbeddedCredentials {
                has_userinfo.set(true);
            }
        };
        let url = Url::options()
            .syntax_violation_callback(Some(&on_violation))
            .parse(&self.upstream_base_url)
            .map_err(|_| ProviderError::InvalidUpstreamBaseUrl)?;
        if !matches!(url.scheme(), "http" | "https")
            || url.host_str().is_none()
            || has_userinfo.get()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
        {
            return Err(ProviderError::InvalidUpstreamBaseUrl.into());
        }
        if self.models.is_empty() {
            return Err(ProviderError::EmptyModels.into());
        }
        let mut models = HashSet::new();
        for model in &self.models {
            validate_identifier(&model.model_id, "model_id")?;
            validate_identifier(&model.upstream_protocol, "upstream_protocol")?;
            validate_identifier(&model.capability_evidence_ref, "capability_evidence_ref")?;
            if !models.insert((&model.model_id, &model.upstream_protocol)) {
                return Err(ProviderError::DuplicateModel.into());
            }
        }
        Ok(())
    }

    /// Compares frozen identities; validation is available through validate_route.
    pub fn matches_route(&self, route: &RouteBinding) -> bool {
        self.provider_id == route.provider_id
            && self.provider_revision_id == route.provider_revision_id
            && self.credential_ref == route.credential_ref
            && self.config_digest == route.config_digest
            && self.models.iter().any(|model| {
                model.model_id == route.model_id
                    && model.upstream_protocol == route.upstream_protocol
                    && model.capability_evidence_ref == route.capability_evidence_ref
            })
    }

    pub fn validate_route(&self, route: &RouteBinding) -> Result<(), DomainError> {
        self.validate()?;
        route.validate()?;
        if !self.matches_route(route) {
            return Err(ProviderError::RouteMismatch.into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutionPolicy {
    pub approval_policy: String,
    pub approvals_reviewer: String,
    pub sandbox: String,
}

impl PermissionProfile {
    pub fn execution_policy(self) -> ExecutionPolicy {
        let (approval_policy, approvals_reviewer, sandbox) = match self {
            Self::Ask => ("on-request", "user", "workspace-write"),
            Self::Auto => ("on-request", "auto_review", "workspace-write"),
            Self::Full => ("never", "user", "danger-full-access"),
        };
        ExecutionPolicy {
            approval_policy: approval_policy.into(),
            approvals_reviewer: approvals_reviewer.into(),
            sandbox: sandbox.into(),
        }
    }
}
