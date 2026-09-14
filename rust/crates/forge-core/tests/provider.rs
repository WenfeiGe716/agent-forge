use forge_core::{
    DomainError, ExecutionPolicy, PermissionProfile, ProviderError, ProviderSnapshot, RouteBinding,
};
use serde_json::{json, Value};

fn snapshot_json() -> Value {
    json!({
        "provider_id": "provider", "provider_revision_id": "provider-v1",
        "upstream_base_url": "https://provider.example/api/v1",
        "credential_ref": "vault:provider-v1", "config_digest": "sha256:config",
        "models": [{
            "model_id": "model", "upstream_protocol": "anthropic_messages",
            "capability_evidence_ref": "evidence:tools"
        }]
    })
}

fn snapshot() -> ProviderSnapshot {
    serde_json::from_value(snapshot_json()).unwrap()
}

fn route_json() -> Value {
    json!({
        "provider_id": "provider", "provider_revision_id": "provider-v1",
        "model_id": "model", "upstream_protocol": "anthropic_messages",
        "gateway_route_id": "route-v1", "responses_base_url": "http://127.0.0.1:8787/v1",
        "config_digest": "sha256:config", "credential_ref": "vault:provider-v1",
        "capability_evidence_ref": "evidence:tools", "gateway_version": "gateway-pinned"
    })
}

#[test]
fn route_requires_every_frozen_provider_and_model_identity_to_match() {
    let provider = snapshot();
    let route = serde_json::from_value(route_json()).unwrap();
    assert_eq!(provider.validate_route(&route), Ok(()));
    assert!(provider.matches_route(&route));

    for field in [
        "provider_id",
        "provider_revision_id",
        "credential_ref",
        "config_digest",
        "model_id",
        "upstream_protocol",
        "capability_evidence_ref",
    ] {
        let mut candidate = route_json();
        candidate[field] = json!(format!("changed-{field}"));
        let route = serde_json::from_value(candidate).unwrap();
        assert!(!provider.matches_route(&route), "{field}");
        assert_eq!(
            provider.validate_route(&route),
            Err(ProviderError::RouteMismatch.into()),
            "{field}"
        );
    }
}

#[test]
fn matching_a_route_still_requires_valid_provider_and_route_snapshots() {
    let route: RouteBinding = serde_json::from_value(route_json()).unwrap();
    let mut provider = snapshot();
    provider.upstream_base_url = "file:///tmp/provider".into();
    assert!(provider.matches_route(&route));
    assert_eq!(
        provider.validate_route(&route),
        Err(ProviderError::InvalidUpstreamBaseUrl.into())
    );

    let mut route = route;
    route.gateway_route_id.clear();
    assert_eq!(
        snapshot().validate_route(&route),
        Err(DomainError::EmptyIdentifier("gateway_route_id"))
    );
}

#[test]
fn required_provider_and_model_references_reject_blank_values() {
    for field in [
        "provider_id",
        "provider_revision_id",
        "upstream_base_url",
        "credential_ref",
        "config_digest",
    ] {
        let mut value = snapshot_json();
        value[field] = json!(" \n\t");
        let provider: ProviderSnapshot = serde_json::from_value(value).unwrap();
        assert_eq!(
            provider.validate(),
            Err(DomainError::EmptyIdentifier(field)),
            "{field}"
        );
    }
    for field in ["model_id", "upstream_protocol", "capability_evidence_ref"] {
        let mut value = snapshot_json();
        value["models"][0][field] = json!(" \n\t");
        let provider: ProviderSnapshot = serde_json::from_value(value).unwrap();
        assert_eq!(
            provider.validate(),
            Err(DomainError::EmptyIdentifier(field)),
            "{field}"
        );
    }
}

#[test]
fn upstream_url_requires_http_host_and_cannot_embed_credentials_or_request_options() {
    for upstream_base_url in [
        "not a url",
        "/api/v1",
        "https://",
        "https://provider .example/v1",
        "file:///tmp/provider",
        "ftp://provider.example/v1",
        "https://user:secret@provider.example/v1",
        "https://user@provider.example/v1",
        "https://:secret@provider.example/v1",
        "https://@provider.example/v1",
        "https://provider.example/v1?token=secret",
        "https://provider.example/v1?",
        "https://provider.example/v1#fragment",
        "https://provider.example/v1#",
    ] {
        let mut provider = snapshot();
        provider.upstream_base_url = upstream_base_url.into();
        assert_eq!(
            provider.validate(),
            Err(ProviderError::InvalidUpstreamBaseUrl.into()),
            "{upstream_base_url}"
        );
    }
    for upstream_base_url in [
        "https://provider.example/api/v1",
        "http://127.0.0.1:9000/v1",
        "http://[::1]:9000/v1",
    ] {
        let mut provider = snapshot();
        provider.upstream_base_url = upstream_base_url.into();
        assert_eq!(provider.validate(), Ok(()), "{upstream_base_url}");
    }
}

#[test]
fn model_protocol_pairs_are_unique_and_model_membership_is_required() {
    let mut provider = snapshot();
    provider.models.clear();
    assert_eq!(provider.validate(), Err(ProviderError::EmptyModels.into()));

    let mut provider = snapshot();
    let mut duplicate = provider.models[0].clone();
    duplicate.capability_evidence_ref = "evidence:replacement".into();
    provider.models.push(duplicate);
    assert_eq!(
        provider.validate(),
        Err(ProviderError::DuplicateModel.into())
    );

    provider.models[1].upstream_protocol = "openai_responses".into();
    assert_eq!(provider.validate(), Ok(()));
    let mut route: RouteBinding = serde_json::from_value(route_json()).unwrap();
    route.upstream_protocol = "openai_responses".into();
    assert_eq!(
        provider.validate_route(&route),
        Err(ProviderError::RouteMismatch.into())
    );
    route.capability_evidence_ref = "evidence:replacement".into();
    assert_eq!(provider.validate_route(&route), Ok(()));
}

#[test]
fn provider_snapshot_round_trips_and_rejects_unknown_fields_at_every_level() {
    let provider = snapshot();
    assert_eq!(serde_json::to_value(&provider).unwrap(), snapshot_json());
    let mut value = snapshot_json();
    value["api_key"] = json!("raw-secret");
    assert!(serde_json::from_value::<ProviderSnapshot>(value).is_err());

    let mut value = snapshot_json();
    value["models"][0]["unchecked_capability"] = json!(true);
    assert!(serde_json::from_value::<ProviderSnapshot>(value).is_err());
}

#[test]
fn permission_profiles_have_complete_deterministic_execution_policies() {
    for (profile, expected) in [
        (
            PermissionProfile::Ask,
            json!({"approval_policy": "on-request", "approvals_reviewer": "user", "sandbox": "workspace-write"}),
        ),
        (
            PermissionProfile::Auto,
            json!({"approval_policy": "on-request", "approvals_reviewer": "auto_review", "sandbox": "workspace-write"}),
        ),
        (
            PermissionProfile::Full,
            json!({"approval_policy": "never", "approvals_reviewer": "user", "sandbox": "danger-full-access"}),
        ),
    ] {
        let policy = profile.execution_policy();
        assert_eq!(serde_json::to_value(&policy).unwrap(), expected);
        assert_eq!(policy, profile.execution_policy());
        assert_eq!(
            serde_json::from_value::<ExecutionPolicy>(expected).unwrap(),
            policy
        );
    }
    for profile in [json!("default"), json!("AUTO"), json!(""), Value::Null] {
        assert!(serde_json::from_value::<PermissionProfile>(profile).is_err());
    }
    let mut policy = serde_json::to_value(PermissionProfile::Ask.execution_policy()).unwrap();
    policy["unsafe_override"] = json!(true);
    assert!(serde_json::from_value::<ExecutionPolicy>(policy).is_err());
}
