use super::*;

const RESERVED: &[&str] = &[
    "model",
    "messages",
    "tools",
    "tool_choice",
    "system",
    "stream",
    "api_key",
    "base_url",
    "headers",
];

pub(super) fn normalize(c: &Value) -> Result<Value> {
    defaults(
        c,
        json!({"method":"POST","auth":{"header":"Authorization","value":"Bearer {{api_key}}"},"headers":{},"supported_parameters":[],"parameter_schema":{},"parameter_placement":"nested","message_templates":{"system":{"role":"system","content":"{{content}}"},"user":{"role":"user","content":"{{content}}"},"assistant":{"role":"assistant","content":"{{content}}","tool_calls":"{{calls}}"},"tool":{"role":"tool","tool_call_id":"{{call_id}}","content":"{{content}}"}},"call_template":{"id":"{{id}}","type":"function","function":{"name":"{{name}}","arguments":"{{arguments_json}}"}},"tool_template":{"type":"function","function":"{{tool}}"},"separate_system":false,"timeout_s":120,"max_response_bytes":8000000,"discovery":null}),
    )
}

/// Extract provider-wide static request headers. These are deliberately
/// limited to non-secret values: credentials remain in `encrypted_key` and
/// are injected by the protocol adapter at request time.
pub(crate) fn static_headers(c: &Value) -> Result<BTreeMap<String, String>> {
    let Some(object) = c.as_object() else {
        return if c.is_null() {
            Ok(BTreeMap::new())
        } else {
            Err(bad("Provider protocol_config must be an object"))
        };
    };
    let values = object
        .get("headers")
        .map(|value| {
            value
                .as_object()
                .ok_or_else(|| bad("Provider headers must be an object"))
        })
        .transpose()?;
    let values = values.cloned().unwrap_or_default();
    if values.len() > 20 {
        return Err(bad("Too many custom headers"));
    }
    let mut names = BTreeSet::new();
    let mut output = BTreeMap::new();
    for (name, value) in values {
        let lower = name.to_ascii_lowercase();
        let value = value
            .as_str()
            .ok_or_else(|| bad("Provider header values must be strings"))?;
        if protected_header(&lower)
            || ["auth", "token", "key", "secret"]
                .iter()
                .any(|part| lower.contains(part))
            || !names.insert(lower)
            || !valid_header_name(&name)
            || value.len() > 1000
            || !value.bytes().all(|byte| (32..=126).contains(&byte))
            || value.contains(['{', '}'])
        {
            return Err(bad("Custom headers contain unsafe characters or templates"));
        }
        output.insert(name.clone(), value.to_owned());
    }
    Ok(output)
}

pub(super) fn validate(c: &Value) -> Result<()> {
    let normalized = normalize(c)?;
    let c = &normalized;
    fields(
        c,
        &[
            "endpoint",
            "method",
            "body",
            "response",
            "auth",
            "headers",
            "supported_parameters",
            "parameter_schema",
            "parameter_placement",
            "message_templates",
            "call_template",
            "tool_template",
            "separate_system",
            "timeout_s",
            "max_response_bytes",
            "discovery",
        ],
    )?;
    endpoint(string(c, "endpoint", 1, 1000)?)?;
    if !["POST", "PUT", "PATCH"].contains(&c["method"].as_str().unwrap_or("")) {
        return Err(bad("Custom HTTP method must be POST, PUT, or PATCH"));
    }
    let body = object(&c["body"])?;
    template(
        &c["body"],
        &["model", "messages", "tools", "parameters", "system"],
        0,
    )?;
    if !has(&c["body"], "model") || !has(&c["body"], "messages") {
        return Err(bad("Custom request must include model and messages"));
    }
    let supported = strings(c, "supported_parameters", 60)?;
    if supported.iter().any(|x| {
        RESERVED.contains(&x.as_str())
            || x.is_empty()
            || x.len() > 64
            || !x
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
            || !x.as_bytes()[0].is_ascii_alphabetic()
    }) {
        return Err(bad("Custom parameters may not replace protected fields"));
    }
    if bool_or(c, "separate_system", false)? && !has(&c["body"], "system") {
        return Err(bad(
            "Separate system messages require a system template reference",
        ));
    }
    let schema = object(&c["parameter_schema"])?;
    if schema.len() > 60 {
        return Err(bad("Too many custom parameter rules"));
    }
    for (name, rule) in schema {
        if !supported.contains(name) {
            return Err(bad("Schema names an unsupported parameter"));
        }
        fields(rule, &["type", "minimum", "maximum", "enum", "required"])?;
        let kind = rule["type"].as_str().unwrap_or("");
        if !["string", "number", "integer", "boolean", "object", "array"].contains(&kind) {
            return Err(bad("Invalid custom parameter type"));
        }
        bool_or(rule, "required", false)?;
        for field in ["minimum", "maximum"] {
            if let Some(value) = rule.get(field).filter(|v| !v.is_null()) {
                if !["number", "integer"].contains(&kind) || value.as_f64().is_none() {
                    return Err(bad("Parameter bounds require numeric types and values"));
                }
            }
        }
        if rule["minimum"]
            .as_f64()
            .zip(rule["maximum"].as_f64())
            .is_some_and(|(lo, hi)| lo > hi)
        {
            return Err(bad("Parameter minimum exceeds maximum"));
        }
        if let Some(values) = rule.get("enum").filter(|v| !v.is_null()) {
            if values.as_array().is_none_or(|v| v.len() > 100) {
                return Err(bad("Parameter enum must contain at most 100 values"));
            }
        }
    }
    {
        if c["parameter_placement"] == "nested"
            && !supported.is_empty()
            && !has(&c["body"], "parameters")
        {
            return Err(bad("Custom request must include declared parameters"));
        }
    }
    {
        if c["parameter_placement"] == "root" && supported.iter().any(|x| body.contains_key(x)) {
            return Err(bad("Root parameters overlap request template"));
        }
    }
    {
        if !["root", "nested"].contains(&c["parameter_placement"].as_str().unwrap_or("")) {
            return Err(bad("Invalid custom parameter placement"));
        }
    }
    let templates = object(&c["message_templates"])?;
    if templates.len() != 4
        || !templates
            .keys()
            .all(|k| ["system", "user", "assistant", "tool"].contains(&k.as_str()))
    {
        return Err(bad(
            "Custom messages must define system, user, assistant, and tool templates",
        ));
    }
    for (role, template_value) in templates {
        let refs = if role == "assistant" {
            &["role", "content", "calls"][..]
        } else if role == "tool" {
            &["role", "content", "call_id"][..]
        } else {
            &["role", "content"][..]
        };
        template(template_value, refs, 0)?;
        if !has(template_value, "content") {
            return Err(bad("Custom message templates must retain content"));
        }
    }
    template(
        &c["call_template"],
        ["id", "name", "arguments", "arguments_json"].as_ref(),
        0,
    )?;
    template(
        &c["tool_template"],
        ["tool", "name", "description", "parameters"].as_ref(),
        0,
    )?;
    fields(
        &c["response"],
        &[
            "text",
            "calls",
            "call_fields",
            "input_tokens",
            "output_tokens",
        ],
    )?;
    let response = object(&c["response"])?;
    for key in ["text", "calls", "input_tokens", "output_tokens"] {
        if let Some(value) = response.get(key).filter(|v| !v.is_null()) {
            pointer(value)?;
        }
    }
    pointer(&c["response"]["text"])?;
    if response.get("calls").is_some_and(|x| !x.is_null()) {
        fields(&c["response"]["call_fields"], &["id", "name", "arguments"])?;
        for key in ["id", "name", "arguments"] {
            pointer(&c["response"]["call_fields"][key])?;
        }
        if !has(&c["body"], "tools")
            || !has(&c["message_templates"]["assistant"], "calls")
            || !has(&c["message_templates"]["tool"], "call_id")
            || !has(&c["call_template"], "id")
            || !has(&c["call_template"], "name")
            || !(has(&c["call_template"], "arguments")
                || has(&c["call_template"], "arguments_json"))
            || !(has(&c["tool_template"], "tool")
                || has(&c["tool_template"], "name") && has(&c["tool_template"], "parameters"))
        {
            return Err(bad(
                "Tool protocol must retain definitions, calls, arguments, and result IDs",
            ));
        }
    } else if !c["response"]["call_fields"].is_null() {
        return Err(bad("Call field mappings require a calls pointer"));
    }
    let header_names = static_headers(c)?
        .keys()
        .map(|name| name.to_ascii_lowercase())
        .collect::<BTreeSet<_>>();
    if !c["auth"].is_null() {
        fields(&c["auth"], &["header", "value"])?;
        let name = string(&c["auth"], "header", 1, 100)?;
        let value = string(&c["auth"], "value", 1, 1000)?;
        if [
            "host",
            "content-length",
            "transfer-encoding",
            "connection",
            "proxy-authorization",
            "cookie",
            "set-cookie",
            "content-type",
        ]
        .contains(&name.to_ascii_lowercase().as_str())
            || !valid_header_name(name)
            || header_names.contains(&name.to_ascii_lowercase())
            || !value.bytes().all(|b| (32..=126).contains(&b))
            || value.matches("{{api_key}}").count() != 1
            || value.replace("{{api_key}}", "").contains(['{', '}'])
        {
            return Err(bad("Auth must use one api_key template"));
        }
    }
    number(c, "timeout_s", 1., 1800.)?;
    number(c, "max_response_bytes", 1024., 8000000.)?;
    if !c["discovery"].is_null() {
        fields(&c["discovery"], &["endpoint", "items", "id", "name"])?;
        endpoint(string(&c["discovery"], "endpoint", 1, 1000)?)?;
        pointer(&c["discovery"]["items"])?;
        pointer(&c["discovery"]["id"])?;
        if !c["discovery"]["name"].is_null() {
            pointer(&c["discovery"]["name"])?;
        }
    }
    Ok(())
}

fn endpoint(value: &str) -> Result<()> {
    if !value.starts_with('/')
        || value.contains("//")
        || value.contains(['?', '#', '\\', ':', '%'])
        || value.split('/').any(|v| v == "." || v == "..")
        || value.chars().any(|v| v.is_control() || v.is_whitespace())
    {
        return Err(bad("Custom endpoint must be a safe same-origin path"));
    }
    Ok(())
}
fn pointer(value: &Value) -> Result<()> {
    let value = value
        .as_str()
        .ok_or_else(|| bad("Response mappings must be RFC 6901 pointers"))?;
    if value.len() > 1000 || !value.is_empty() && !value.starts_with('/') {
        return Err(bad("Invalid JSON pointer"));
    }
    let mut bytes = value.bytes();
    while let Some(b) = bytes.next() {
        if b == b'~' && !matches!(bytes.next(), Some(b'0' | b'1')) {
            return Err(bad("Invalid JSON pointer escape"));
        }
    }
    Ok(())
}
fn protected_header(name: &str) -> bool {
    [
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "content-type",
    ]
    .contains(&name)
}
fn valid_header_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 100
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&b))
}

fn has(v: &Value, name: &str) -> bool {
    match v {
        Value::Object(m) => m.values().any(|x| has(x, name)),
        Value::Array(a) => a.iter().any(|x| has(x, name)),
        Value::String(s) => s == &format!("{{{{{name}}}}}"),
        _ => false,
    }
}
fn template(v: &Value, refs: &[&str], depth: usize) -> Result<()> {
    if depth > 20 {
        return Err(bad("Custom template nesting exceeds 20 levels"));
    }
    match v {
        Value::Object(m) => {
            for (k, x) in m {
                if k.contains("{{") || k.contains("}}") {
                    return Err(bad("Template keys must be fixed"));
                }
                template(x, refs, depth + 1)?
            }
        }
        Value::Array(a) => {
            for x in a {
                template(x, refs, depth + 1)?
            }
        }
        Value::String(s) if s.contains("{{") || s.contains("}}") => {
            let name = s
                .strip_prefix("{{")
                .and_then(|x| x.strip_suffix("}}"))
                .ok_or_else(|| bad("Template references must replace the whole value"))?;
            if !refs.contains(&name) {
                return Err(bad("Unknown template reference"));
            }
        }
        _ => (),
    }
    Ok(())
}

pub(super) fn parameters(
    protocol: &str,
    declared: &Value,
    values: &Value,
    config: &Value,
) -> Result<()> {
    if protocol != "custom_http" {
        return super::validate_parameters(protocol, declared, values);
    }
    let normalized = normalize(config)?;
    let config = &normalized;
    let supported = config["supported_parameters"]
        .as_array()
        .ok_or_else(|| bad("Custom supported_parameters must be an array"))?;
    let declared = declared
        .as_array()
        .ok_or_else(|| bad("Declared parameters must be an array"))?;
    if declared
        .iter()
        .any(|v| v.as_str().is_none_or(|v| !supported.contains(&json!(v))))
    {
        return Err(bad("Model declares an unsupported custom parameter"));
    }
    for key in object(values)?.keys() {
        if !declared.contains(&json!(key)) || RESERVED.contains(&key.as_str()) {
            return Err(bad("Custom parameter is not permitted"));
        }
    }
    for (name, rule) in object(&config["parameter_schema"])? {
        let value = match values.get(name) {
            Some(v) => v,
            None => {
                if rule["required"] == true {
                    return Err(bad("Required custom parameter is missing"));
                }
                continue;
            }
        };
        let valid = match rule["type"].as_str() {
            Some("string") => value.is_string(),
            Some("number") => value.is_number(),
            Some("integer") => value.is_i64() || value.is_u64(),
            Some("boolean") => value.is_boolean(),
            Some("object") => value.is_object(),
            Some("array") => value.is_array(),
            _ => false,
        };
        if !valid {
            return Err(bad("Custom parameter has wrong type"));
        }
        if let Some(n) = value.as_f64() {
            if rule["minimum"].as_f64().is_some_and(|m| n < m)
                || rule["maximum"].as_f64().is_some_and(|m| n > m)
            {
                return Err(bad("Custom parameter is out of range"));
            }
        }
        if rule["enum"]
            .as_array()
            .is_some_and(|items| !items.contains(value))
        {
            return Err(bad("Custom parameter is not allowed"));
        }
    }
    Ok(())
}
