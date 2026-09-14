use super::*;

pub(super) fn resolve(c: &Connection, body: &Value) -> Result<Value> {
    fields(body, &["text", "entry_agent_id"])?;
    let text = string(body, "text", 0, 30000)?;
    let agents: Vec<_> = configs(c, ConfigTable::Agent)?
        .into_iter()
        .filter(|a| a["enabled"] != false)
        .collect();
    let mut targets = Vec::new();
    let mut skip = 0;
    for (index, ch) in text.char_indices() {
        if index < skip
            || ch != '@'
            || index > 0 && !text[..index].chars().next_back().unwrap().is_whitespace()
        {
            continue;
        }
        let rest = &text[index + 1..];
        let department = rest
            .strip_prefix('{')
            .and_then(|s| s.find('}').filter(|n| *n > 0).map(|n| &s[..n]));
        let name = department.unwrap_or_else(|| {
            rest.split(|ch: char| {
                ch.is_whitespace() || [',', ':', '\u{ff0c}', '\u{ff1a}'].contains(&ch)
            })
            .next()
            .unwrap_or("")
        });
        if name.is_empty() {
            continue;
        }
        skip = index + 1 + name.len() + if department.is_some() { 2 } else { 0 };
        let matches: Vec<_> = agents
            .iter()
            .filter(|a| {
                if department.is_some() {
                    a["department"] == name
                } else {
                    a["id"] == name || a["name"] == name
                }
            })
            .map(|a| a["id"].as_str().unwrap().to_owned())
            .collect();
        if matches.is_empty() || department.is_none() && matches.len() != 1 {
            return Err(bad(format!(
                "Target '{name}' is unavailable or ambiguous; use an exact member ID"
            )));
        }
        for id in matches {
            if !targets.contains(&id) {
                targets.push(id);
            }
        }
    }
    let explicit = body
        .get("entry_agent_id")
        .filter(|v| !v.is_null())
        .map(|v| v.as_str().ok_or_else(|| bad("Entry member must be an ID")))
        .transpose()?
        .filter(|v| !v.is_empty());
    let entry = if let Some(id) = explicit {
        if !agents.iter().any(|a| a["id"] == id)
            || !targets.is_empty() && !targets.iter().any(|v| v == id)
        {
            return Err(bad(
                "Selected entry member conflicts with the directed recipients",
            ));
        }
        id.to_owned()
    } else if targets.len() == 1 {
        targets[0].clone()
    } else {
        let planners: Vec<_> = agents
            .iter()
            .filter(|a| {
                a["role"] == "planner"
                    && (targets.is_empty() || targets.iter().any(|id| a["id"] == *id))
            })
            .collect();
        if planners.len() != 1 {
            return Err(bad(
                "Select an explicit entry member; there is no unique planner",
            ));
        }
        planners[0]["id"].as_str().unwrap().to_owned()
    };
    if targets.is_empty() {
        targets.push(entry.clone());
    }
    let resolved: Vec<_> = targets.iter().map(|id| { let a = agents.iter().find(|a| a["id"] == *id).unwrap(); json!({"id":id,"name":a["name"],"department":a.get("department").cloned().unwrap_or(json!(""))}) }).collect();
    Ok(json!({"entry_agent_id":entry,"recipients":targets,"resolved":resolved}))
}
