use super::*;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(super) struct RuntimeLimits {
    #[serde(default = "default_parallel")]
    pub max_parallel: u64,
    #[serde(default = "default_tasks")]
    pub max_tasks: u64,
    #[serde(default = "default_messages")]
    pub max_agent_messages: u64,
    pub max_model_calls: u64,
    pub task_timeout_s: u64,
    pub call_timeout_s: u64,
    pub command_timeout_s: u64,
    pub max_file_bytes: u64,
    pub max_context_chars: u64,
}
fn default_parallel() -> u64 {
    3
}
fn default_tasks() -> u64 {
    20
}
fn default_messages() -> u64 {
    200
}

impl Default for RuntimeLimits {
    fn default() -> Self {
        Self {
            max_parallel: default_parallel(),
            max_tasks: default_tasks(),
            max_agent_messages: default_messages(),
            max_model_calls: 100,
            task_timeout_s: 3600,
            call_timeout_s: 120,
            command_timeout_s: 120,
            max_file_bytes: 200_000,
            max_context_chars: 120_000,
        }
    }
}

fn merge_object(target: &mut RuntimeLimits, value: Option<&Value>) -> Result<()> {
    let Some(value) = value else {
        return Ok(());
    };
    let object = value.as_object().ok_or(BackendError::InvalidData)?;
    for (key, slot, min, max) in [
        ("max_parallel", &mut target.max_parallel, 1, 32),
        ("max_tasks", &mut target.max_tasks, 1, 100),
        (
            "max_agent_messages",
            &mut target.max_agent_messages,
            0,
            5_000,
        ),
        ("max_model_calls", &mut target.max_model_calls, 1, 10_000),
        ("task_timeout_s", &mut target.task_timeout_s, 30, 86_400),
        ("call_timeout_s", &mut target.call_timeout_s, 5, 1_800),
        ("command_timeout_s", &mut target.command_timeout_s, 1, 3_600),
        (
            "max_file_bytes",
            &mut target.max_file_bytes,
            1_024,
            5_000_000,
        ),
        (
            "max_context_chars",
            &mut target.max_context_chars,
            4_096,
            1_000_000,
        ),
    ] {
        if let Some(raw) = object.get(key) {
            let n = raw.as_u64().ok_or(BackendError::InvalidData)?;
            if !(min..=max).contains(&n) {
                return Err(BackendError::InvalidData);
            }
            *slot = n;
        }
    }
    Ok(())
}

pub(super) fn resolve_runtime(
    c: &rusqlite::Connection,
    project: &Value,
    agent: &Value,
    input: Option<&Value>,
) -> Result<RuntimeLimits> {
    let mut limits = RuntimeLimits::default();
    let platform: Option<String> = c
        .query_row(
            "SELECT value FROM platform_config WHERE id='runtime'",
            [],
            |r| r.get(0),
        )
        .optional()
        .map_err(db)?;
    if let Some(raw) = platform {
        let value: Value = serde_json::from_str(&raw).map_err(|_| BackendError::InvalidData)?;
        merge_object(&mut limits, Some(&value))?;
    }
    merge_object(&mut limits, project.get("runtime"))?;
    merge_object(&mut limits, agent.get("runtime"))?;
    merge_object(&mut limits, input.and_then(|v| v.get("runtime")))?;
    Ok(limits)
}

// Validate fields that are copied into the frozen run. Runtime limits are
// merged by resolve_runtime; model parameters are validated by the caller
// against the selected protocol before they are persisted.
pub(super) fn validate(
    _c: &rusqlite::Connection,
    _project: &Value,
    agent: &Value,
    model: &Value,
    provider: &Value,
) -> Result<()> {
    for value in [agent.get("model_parameters"), model.get("parameters")]
        .into_iter()
        .flatten()
    {
        if !value.is_object() {
            return Err(BackendError::InvalidData);
        }
    }
    if let Some(value) = agent.get("fallback_model_ids") {
        let ids = value.as_array().ok_or(BackendError::InvalidData)?;
        if ids.len() > 5 || ids.iter().any(|id| id.as_str().is_none_or(str::is_empty)) {
            return Err(BackendError::InvalidData);
        }
        let unique: std::collections::BTreeSet<_> = ids.iter().filter_map(Value::as_str).collect();
        if unique.len() != ids.len() {
            return Err(BackendError::InvalidData);
        }
    }
    if let Some(value) = provider.get("protocol_config") {
        if !value.is_object() {
            return Err(BackendError::InvalidData);
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;
    use serde_json::json;

    #[test]
    fn runtime_limits_merge_in_precedence_order() {
        let c = Connection::open_in_memory().unwrap();
        c.execute(
            "CREATE TABLE platform_config (id TEXT PRIMARY KEY, value JSON NOT NULL)",
            [],
        )
        .unwrap();
        c.execute(
            "INSERT INTO platform_config VALUES ('runtime', ?1)",
            [json!({"call_timeout_s": 60, "max_model_calls": 12}).to_string()],
        )
        .unwrap();
        let limits = resolve_runtime(
            &c,
            &json!({"runtime":{"call_timeout_s":90}}),
            &json!({"runtime":{"max_model_calls":4}}),
            Some(&json!({"runtime":{"call_timeout_s":30}})),
        )
        .unwrap();
        assert_eq!(limits.call_timeout_s, 30);
        assert_eq!(limits.max_model_calls, 4);
    }

    #[test]
    fn model_parameters_and_fallback_ids_are_structurally_checked() {
        let project = json!({});
        let model = json!({"parameters":{"temperature":0.3}});
        let provider = json!({"protocol_config":{}});
        let valid = json!({"model_parameters":{"temperature":0.7},"fallback_model_ids":["backup"]});
        validate(
            &Connection::open_in_memory().unwrap(),
            &project,
            &valid,
            &model,
            &provider,
        )
        .unwrap();
        let duplicate = json!({"fallback_model_ids":["backup","backup"]});
        assert!(validate(
            &Connection::open_in_memory().unwrap(),
            &project,
            &duplicate,
            &model,
            &provider
        )
        .is_err());
    }
}
