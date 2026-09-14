use super::*;

/// Map the product Responses parameter shape onto the public App Server
/// fields. Arbitrary provider parameters are deliberately rejected: the
/// App Server ignores unknown fields, which would make a frozen run lie about
/// the effective model configuration.
fn validate_keys(parameters: &Value) -> Result<&serde_json::Map<String, Value>> {
    let object = parameters.as_object().ok_or(BackendError::InvalidData)?;
    for key in object.keys() {
        // Provider parameters are intentionally accepted here even when the
        // App Server has no corresponding field.  They are validated against
        // the selected provider schema before this module is called; the App
        // Server simply uses its own defaults for fields such as
        // `max_output_tokens` and `temperature`.  Rejecting those fields used
        // to make otherwise valid gpt-5.6-sol/OpenAI Responses profiles fail
        // before the first turn with the misleading “Model parameter cannot
        // be expressed …” error.
        let app_server_key = matches!(key.as_str(), "reasoning" | "text");
        let provider_only_key = matches!(
            key.as_str(),
            "temperature"
                | "top_p"
                | "max_output_tokens"
                | "max_tokens"
                | "max_completion_tokens"
                | "presence_penalty"
                | "frequency_penalty"
                | "seed"
                | "reasoning_effort"
                | "stop"
                | "thinking"
                | "top_k"
                | "stop_sequences"
                | "output_config"
                | "topP"
                | "topK"
                | "maxOutputTokens"
                | "stopSequences"
                | "thinkingConfig"
                | "responseMimeType"
                | "responseSchema"
                | "parallel_tool_calls"
        );
        if !app_server_key && !provider_only_key {
            return Err(BackendError::UnsupportedConfiguration(
                "An unknown model parameter cannot be expressed by the Codex App Server",
            ));
        }
    }
    Ok(object)
}

pub(super) fn apply_thread(thread: &mut Value, parameters: &Value) -> Result<()> {
    let object = validate_keys(parameters)?;
    if let Some(text_value) = object.get("text") {
        let text = text_value.as_object().ok_or(BackendError::InvalidData)?;
        if let Some(verbosity) = text.get("verbosity") {
            let verbosity = verbosity
                .as_str()
                .filter(|v| matches!(*v, "low" | "medium" | "high"))
                .ok_or(BackendError::InvalidData)?;
            thread["config"]["model_verbosity"] = json!(verbosity);
        }
    }
    Ok(())
}

pub(super) fn apply_turn(turn: &mut Value, parameters: &Value) -> Result<()> {
    let object = validate_keys(parameters)?;
    if let Some(reasoning) = object.get("reasoning") {
        let reasoning = reasoning.as_object().ok_or(BackendError::InvalidData)?;
        for key in reasoning.keys() {
            if key != "effort" && key != "summary" {
                return Err(BackendError::UnsupportedConfiguration(
                    "Reasoning parameter cannot be expressed by turn/start",
                ));
            }
        }
        if let Some(effort) = reasoning.get("effort") {
            let effort = effort
                .as_str()
                .filter(|v| {
                    matches!(
                        *v,
                        "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max" | "ultra"
                    )
                })
                .ok_or(BackendError::InvalidData)?;
            turn["effort"] = json!(effort);
        }
        if let Some(summary) = reasoning.get("summary") {
            let summary = summary
                .as_str()
                .filter(|v| matches!(*v, "auto" | "concise" | "detailed" | "none"))
                .ok_or(BackendError::InvalidData)?;
            turn["summary"] = json!(summary);
        }
    }
    // OpenAI Chat profiles historically stored `reasoning_effort` as a flat
    // parameter.  Codex exposes the same setting as `effort` on turn/start;
    // preserve it instead of dropping a meaningful user selection.
    if let Some(effort) = object.get("reasoning_effort") {
        let effort = effort
            .as_str()
            .filter(|v| {
                matches!(
                    *v,
                    "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max" | "ultra"
                )
            })
            .ok_or(BackendError::InvalidData)?;
        turn["effort"] = json!(effort);
    }
    if let Some(text_value) = object.get("text") {
        let text = text_value.as_object().ok_or(BackendError::InvalidData)?;
        for key in text.keys() {
            if key != "verbosity" && key != "format" {
                return Err(BackendError::UnsupportedConfiguration(
                    "Text parameter cannot be expressed by the Codex App Server",
                ));
            }
        }
        if let Some(format) = text.get("format") {
            let format = format.as_object().ok_or(BackendError::InvalidData)?;
            let schema =
                format
                    .get("json_schema")
                    .ok_or(BackendError::UnsupportedConfiguration(
                        "Only text.format.json_schema is supported",
                    ))?;
            if !schema.is_object() {
                return Err(BackendError::InvalidData);
            }
            if format.keys().any(|key| key != "json_schema") {
                return Err(BackendError::UnsupportedConfiguration(
                    "Only text.format.json_schema is supported",
                ));
            }
            turn["outputSchema"] = schema.clone();
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maps_only_formal_responses_fields() {
        let mut thread = json!({"config": {}});
        let mut turn = json!({});
        apply_thread(
            &mut thread,
            &json!({
                "text": {"verbosity":"low"}
            }),
        )
        .unwrap();
        apply_turn(
            &mut turn,
            &json!({
                "reasoning": {"effort":"high", "summary":"concise"},
                "text": {"format":{"json_schema":{"type":"object"}}}
            }),
        )
        .unwrap();
        assert_eq!(turn["effort"], "high");
        assert_eq!(turn["summary"], "concise");
        assert_eq!(thread["config"]["model_verbosity"], "low");
        assert_eq!(turn["outputSchema"]["type"], "object");
    }

    #[test]
    fn ignores_provider_only_parameters_and_rejects_unknown_or_non_json_schema_parameters() {
        let mut turn = json!({});
        apply_turn(
            &mut turn,
            &json!({"max_output_tokens":4096,"temperature":0.2,"reasoning_effort":"high"}),
        )
        .unwrap();
        assert_eq!(turn["effort"], "high");
        assert!(apply_turn(&mut turn, &json!({"my_custom_parameter":true})).is_err());
        assert!(apply_turn(&mut turn, &json!({"text":{"format":{"type":"text"}}})).is_err());
    }
}
