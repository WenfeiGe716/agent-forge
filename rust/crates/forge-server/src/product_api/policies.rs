use super::*;

pub(super) fn request(
    c: &Connection,
    u: &User,
    m: &str,
    p: &[&str],
    q: &BTreeMap<String, String>,
    b: &Value,
) -> Result<ProductReply> {
    let mut ledger = config(
        c,
        "member_execution_policies",
        json!({"version":0,"teams":{},"agents":{}}),
    )?;
    let layout = config(
        c,
        "organization_layout",
        json!({"ref":null,"config":{"teams":[],"floors":[]}}),
    )?;
    let project = if let Some(pid) = q.get("project_id").filter(|x| !x.is_empty()) {
        Some(
            c.query_row(
                "SELECT config,owner_id FROM projects WHERE id=?1",
                [pid],
                |r| {
                    let mut v = decode(r.get(0)?)?;
                    v["owner_id"] = r.get::<_, String>(1)?.into();
                    v["id"] = json!(pid);
                    Ok(v)
                },
            )
            .optional()?
            .filter(|v| visible(v, u))
            .ok_or_else(missing)?,
        )
    } else {
        None
    };
    match (m, p) {
        ("GET", ["execution", "policies"]) => (),
        ("PUT", ["execution", "policies", scope @ ("team" | "agent"), id]) => {
            fields(
                b,
                &[
                    "permission_profile",
                    "expected_version",
                    "expected_layout_ref",
                    "full_access_confirmed",
                ],
            )?;
            if b["expected_version"] != ledger["version"]
                || b["expected_layout_ref"] != layout["ref"]
            {
                return Err(conflict());
            }
            let profile = b["permission_profile"].as_str();
            if profile.is_some_and(|s| !["ask", "auto", "full"].contains(&s))
                || !b["permission_profile"].is_null() && profile.is_none()
            {
                return Err(bad("Invalid permission profile"));
            }
            if profile == Some("full") && b["full_access_confirmed"] != true {
                return Err(bad("Full access requires explicit confirmation"));
            }
            if *scope == "agent" {
                get_config(c, ConfigTable::Agent, id)?;
            } else if !layout["config"]["teams"]
                .as_array()
                .is_some_and(|v| v.iter().any(|t| t["id"] == *id))
            {
                return Err(missing());
            }
            let group = if *scope == "agent" { "agents" } else { "teams" };
            if let Some(profile) = profile {
                ledger[group][*id] = json!({"profile":profile,"actor":u.id,"updated_at":now(c)?,"layout_id":layout["ref"]["id"]})
            } else {
                ledger[group]
                    .as_object_mut()
                    .ok_or_else(|| bad("Corrupt policy ledger"))?
                    .remove(*id);
            }
            ledger["version"] = json!(
                ledger["version"]
                    .as_u64()
                    .ok_or_else(|| bad("Corrupt policy version"))?
                    + 1
            );
            put_config(c, "member_execution_policies", &ledger)?;
            emit(
                c,
                u,
                "execution.policy.updated",
                json!({"scope":scope,"target_id":id,"permission_profile":profile,"version":ledger["version"],"existing_runs_changed":false}),
            )?;
        }
        _ => return Err(missing()),
    }
    let agents = configs(c, ConfigTable::Agent)?;
    let teams = layout["config"]["teams"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let mut team_out = Vec::new();
    for team in &teams {
        let mut t = team.clone();
        t["department"] = layout["config"]["floors"]
            .as_array()
            .and_then(|v| v.iter().find(|f| f["floor"] == t["floor"]))
            .map(|f| f["department"].clone())
            .unwrap_or(json!(""));
        t["members"]=json!(team["member_ids"].as_array().cloned().unwrap_or_default().iter().map(|id|json!({"id":id,"name":agents.iter().find(|a|a["id"]==*id).map(|a|a["name"].clone()).unwrap_or(json!(""))})).collect::<Vec<_>>());
        object(&t)?;
        t.as_object_mut().unwrap().extend(
            object(&resolved(
                &ledger,
                &layout,
                Some(team),
                None,
                project.as_ref(),
            ))?
            .clone(),
        );
        team_out.push(t);
    }
    let mut agent_out = Vec::new();
    for agent in agents {
        let team = teams.iter().find(|t| {
            t["member_ids"]
                .as_array()
                .is_some_and(|ids| ids.contains(&agent["id"]))
        });
        let mut a = json!({"id":agent["id"],"name":agent["name"],"role":agent["role"],"job_title":agent["job_title"],"enabled":agent["enabled"],"team_id":team.map(|t|t["id"].clone()),"team_name":team.map(|t|t["name"].clone()).unwrap_or(json!(""))});
        a.as_object_mut().unwrap().extend(
            object(&resolved(
                &ledger,
                &layout,
                team,
                agent["id"].as_str(),
                project.as_ref(),
            ))?
            .clone(),
        );
        agent_out.push(a)
    }
    Ok(ProductReply::json(
        200,
        json!({"version":ledger["version"],"layout_ref":layout["ref"],"project":project.map(|p|json!({"id":p["id"],"name":p["name"],"permission_profile":p["permission_profile"],"execution_mode":p["execution_mode"]})),"teams":team_out,"agents":agent_out}),
    ))
}
fn resolved(
    ledger: &Value,
    layout: &Value,
    team: Option<&Value>,
    agent: Option<&str>,
    project: Option<&Value>,
) -> Value {
    let base = project
        .map(|p| p["permission_profile"].clone())
        .unwrap_or(Value::Null);
    let mut effective = base.clone();
    let mut source = "project";
    let mut source_id = project.map(|p| p["id"].clone()).unwrap_or(Value::Null);
    let mut source_name = project.map(|p| p["name"].clone()).unwrap_or(json!(""));
    let mut override_profile = Value::Null;
    if let Some(team) = team {
        let entry = &ledger["teams"][team["id"].as_str().unwrap_or("")];
        if entry.is_object() && entry["layout_id"] == layout["ref"]["id"] {
            effective = entry["profile"].clone();
            source = "team";
            source_id = team["id"].clone();
            source_name = team["name"].clone();
            override_profile = effective.clone();
        }
    }
    let inherited = if agent.is_some() {
        effective.clone()
    } else {
        base
    };
    if let Some(aid) = agent {
        override_profile = Value::Null;
        let entry = &ledger["agents"][aid];
        if entry.is_object() {
            effective = entry["profile"].clone();
            source = "agent";
            source_id = json!(aid);
            source_name = json!("Member override");
            override_profile = effective.clone();
        }
    }
    json!({"permission_profile":override_profile,"effective_profile":effective,"inherited_profile":inherited,"source":source,"source_id":source_id,"source_name":source_name,"policy_version":ledger["version"]})
}
