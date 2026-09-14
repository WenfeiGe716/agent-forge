use super::*;

pub(super) fn request(
    c: &Connection,
    u: &User,
    m: &str,
    p: &[&str],
    q: &BTreeMap<String, String>,
    b: &Value,
) -> Result<ProductReply> {
    let (status, v) = match (m, p) {
        ("GET", ["design", "assets"]) => {
            let kind = q.get("kind").cloned().unwrap_or_default();
            let mut s = c.prepare(
                "SELECT id FROM design_assets WHERE ?1='' OR kind=?1 ORDER BY created_at DESC,id",
            )?;
            let ids = s
                .query_map([kind], |r| r.get::<_, String>(0))?
                .collect::<std::result::Result<Vec<_>, _>>()?;
            (
                200,
                json!(ids
                    .into_iter()
                    .map(|id| get(c, &id, None))
                    .collect::<Result<Vec<_>>>()?),
            )
        }
        ("POST", ["design", "assets"]) => {
            fields(b, &["kind", "name", "config", "source"])?;
            let kind = string(b, "kind", 1, 24)?;
            if ![
                "capability",
                "routing",
                "team",
                "office",
                "workflow",
                "benchmark",
            ]
            .contains(&kind)
            {
                return Err(bad("Unknown design kind"));
            }
            if b.get("source").is_some_and(|s| s != "manual") {
                return Err(bad("Automated provenance is server-owned"));
            }
            let id = uid();
            c.execute("INSERT INTO design_assets(id,kind,latest_version,published_version,lock_version,created_by,created_at) VALUES(?1,?2,1,NULL,0,?3,?4)",params![id,kind,u.id,now(c)?])?;
            insert_revision(c, u, &id, kind, 1, b)?;
            audit(c, u, &get(c, &id, None)?, "proposed", "")?;
            (201, get(c, &id, None)?)
        }
        ("GET", ["design", "assets", id]) => {
            let mut v = get(c, id, None)?;
            let mut s = c.prepare(
                "SELECT version FROM design_revisions WHERE asset_id=?1 ORDER BY version DESC",
            )?;
            let versions = s
                .query_map([id], |r| r.get::<_, u64>(0))?
                .collect::<std::result::Result<Vec<_>, _>>()?;
            v["versions"] = json!(versions
                .into_iter()
                .map(|ver| get(c, id, Some(ver)))
                .collect::<Result<Vec<_>>>()?);
            let mut s=c.prepare("SELECT id,version,digest,action,actor,comment,created_at FROM design_audit WHERE asset_id=?1 ORDER BY created_at,id")?;
            v["audit"]=json!(s.query_map([id],|r|Ok(json!({"id":r.get::<_,String>(0)?,"version":r.get::<_,u64>(1)?,"digest":r.get::<_,String>(2)?,"action":r.get::<_,String>(3)?,"actor":r.get::<_,String>(4)?,"comment":r.get::<_,String>(5)?,"created_at":r.get::<_,String>(6)?})))?.collect::<std::result::Result<Vec<_>,_>>()?);
            (200, v)
        }
        ("GET", ["design", "assets", id, "versions", version]) => (
            200,
            get(
                c,
                id,
                Some(version.parse().map_err(|_| bad("Invalid design version"))?),
            )?,
        ),
        ("PUT", ["design", "assets", id]) => {
            fields(b, &["name", "config", "expected_version", "source"])?;
            let old = get(c, id, None)?;
            if b["expected_version"] != old["latest_version"] {
                return Err(conflict());
            }
            if b.get("source").is_some_and(|s| s != "manual") {
                return Err(bad("Automated provenance is server-owned"));
            }
            let version = old["latest_version"].as_u64().unwrap() + 1;
            insert_revision(c, u, id, old["kind"].as_str().unwrap(), version, b)?;
            c.execute("UPDATE design_assets SET latest_version=?1,lock_version=lock_version+1 WHERE id=?2",params![version,id])?;
            audit(c, u, &get(c, id, None)?, "revised", "")?;
            (200, get(c, id, None)?)
        }
        ("POST", ["design", "assets", id, action @ ("publish" | "revoke")]) => {
            fields(b, &["version", "digest", "comment"])?;
            let mut v = confirmed(c, id, b, false)?;
            let status = v["status"].as_str().unwrap_or("");
            if *action == "publish" {
                if status == "revoked" {
                    return Err(conflict());
                }
                validate(c, v["kind"].as_str().unwrap(), &v["config"])?;
                if status != "published" {
                    c.execute("UPDATE design_revisions SET status='published',published_by=?1,published_at=?2 WHERE asset_id=?3 AND version=?4",params![u.id,now(c)?,id,b["version"].as_u64()])?;
                    c.execute("UPDATE design_assets SET published_version=MAX(COALESCE(published_version,0),?1),lock_version=lock_version+1 WHERE id=?2",params![b["version"].as_u64(),id])?;
                    audit(c, u, &v, "published", b["comment"].as_str().unwrap_or(""))?
                }
            } else if status != "revoked" {
                if status != "published" {
                    return Err(conflict());
                }
                c.execute("UPDATE design_revisions SET status='revoked',revoked_by=?1,revoked_at=?2 WHERE asset_id=?3 AND version=?4",params![u.id,now(c)?,id,b["version"].as_u64()])?;
                c.execute("UPDATE design_assets SET published_version=(SELECT MAX(version) FROM design_revisions WHERE asset_id=?1 AND status='published'),lock_version=lock_version+1 WHERE id=?1",[id])?;
                audit(c, u, &v, "revoked", b["comment"].as_str().unwrap_or(""))?
            }
            v = get(c, id, b["version"].as_u64())?;
            (200, v)
        }
        ("POST", ["design", "assets", id, "instantiate"]) => {
            let v = confirmed(c, id, b, true)?;
            if v["kind"] != "team" {
                return Err(bad("Only team designs can be instantiated"));
            }
            validate(c, "team", &v["config"])?;
            let members = v["config"]["members"].as_array().unwrap();
            let mut mapping = Map::new();
            for member in members {
                mapping.insert(member["key"].as_str().unwrap().into(), json!(uid()));
            }
            let instance_id = uid();
            let mut relations = Vec::new();
            for member in members {
                let mut agent = member.clone();
                let key = agent["key"].as_str().unwrap().to_owned();
                agent.as_object_mut().unwrap().remove("key");
                let leader = agent["reports_to"]
                    .as_str()
                    .and_then(|k| mapping.get(k))
                    .cloned()
                    .unwrap_or(Value::Null);
                let recipients = agent["communication"]["recipients"]
                    .as_array()
                    .cloned()
                    .unwrap_or_default()
                    .into_iter()
                    .map(|r| mapping[r.as_str().unwrap()].clone())
                    .collect::<Vec<_>>();
                if agent["communication"].is_null() {
                    agent["communication"] = json!({"scope":"run","recipients":[]})
                }
                agent["reports_to"] = Value::Null;
                agent["communication"]["recipients"] = json!([]);
                let mut config = validate_agent(c, None, &agent, None)?;
                relations.push((
                    mapping[&key].as_str().unwrap().to_owned(),
                    leader,
                    recipients,
                ));
                config["design_ref"] = json!({"id":id,"version":b["version"],"digest":b["digest"],"member_key":key,"instance_id":instance_id});
                save_config(
                    c,
                    u,
                    ConfigTable::Agent,
                    mapping[&key].as_str().unwrap(),
                    config,
                    None,
                )?;
            }
            for (id, leader, recipients) in relations {
                let mut agent = get_config(c, ConfigTable::Agent, &id)?;
                agent["reports_to"] = leader;
                agent["communication"]["recipients"] = json!(recipients);
                agent.as_object_mut().unwrap().remove("id");
                agent.as_object_mut().unwrap().remove("version");
                c.execute(
                    "UPDATE agents SET config=?1 WHERE id=?2",
                    params![encode(&agent), id],
                )?;
            }
            audit(
                c,
                u,
                &v,
                "instantiated",
                b["comment"].as_str().unwrap_or(""),
            )?;
            let mut workflow = v["config"]["workflow"].clone();
            if let Some(members) = workflow["participants"].as_array() {
                let mapped = members
                    .iter()
                    .map(|m| {
                        mapping
                            .get(m.as_str().unwrap_or(""))
                            .cloned()
                            .ok_or_else(|| bad("Workflow member outside team"))
                    })
                    .collect::<Result<Vec<_>>>()?;
                workflow["participants"] = json!(mapped);
                if let Some(id) = workflow["assessor_id"].as_str() {
                    workflow["assessor_id"] = mapping
                        .get(id)
                        .cloned()
                        .ok_or_else(|| bad("Workflow assessor outside team"))?;
                }
            }
            (
                201,
                json!({"agent_ids":mapping.values().collect::<Vec<_>>(),"member_mapping":mapping,"team_ref":{"id":id,"version":b["version"]},"digest":b["digest"],"workflow":workflow,"instance_id":instance_id}),
            )
        }
        ("POST", ["design", "assets", id, "apply-office"]) => {
            let v = confirmed(c, id, b, true)?;
            if v["kind"] != "office" {
                return Err(bad("Only office layouts can be applied"));
            }
            validate(c, "office", &v["config"])?;
            let mut changed = Vec::new();
            for (aid, position) in object(&v["config"]["positions"])? {
                let mut agent = get_config(c, ConfigTable::Agent, aid)?;
                let version = agent["version"].as_u64();
                agent["office_position"] = position.clone();
                if let Some(floors) = v["config"]["floors"].as_array() {
                    if let Some(f) = floors.iter().find(|f| f["floor"] == position["floor"]) {
                        agent["department"] = f["department"].clone()
                    }
                }
                save_config(c, u, ConfigTable::Agent, aid, agent, version)?;
                changed.push(aid.clone());
            }
            let layout = json!({"ref":{"id":id,"version":b["version"],"digest":b["digest"]},"config":v["config"],"applied_at":now(c)?});
            put_config(c, "organization_layout", &layout)?;
            audit(c, u, &v, "applied", b["comment"].as_str().unwrap_or(""))?;
            (
                200,
                json!({"applied":true,"id":id,"version":b["version"],"organization":layout,"changed_agent_ids":changed}),
            )
        }
        _ => return Err(missing()),
    };
    Ok(ProductReply::json(status, v))
}
fn insert_revision(
    c: &Connection,
    u: &User,
    id: &str,
    kind: &str,
    version: u64,
    b: &Value,
) -> Result<()> {
    let name = string(b, "name", 1, 120)?;
    object(&b["config"])?;
    if b["config"].to_string().len() > 400000 {
        return Err(bad("Design configuration exceeds 400 KB"));
    }
    let sum = digest(&json!({"kind":kind,"name":name,"config":b["config"]}));
    c.execute("INSERT INTO design_revisions(id,asset_id,version,name,config,digest,source,generation,status,created_by,created_at,published_by,published_at,revoked_by,revoked_at) VALUES(?1,?2,?3,?4,?5,?6,'manual','{}','draft',?7,?8,'','','','')",params![format!("{id}:{version}"),id,version,name,encode(&b["config"]),sum,u.id,now(c)?])?;
    Ok(())
}
pub(super) fn get(c: &Connection, id: &str, version: Option<u64>) -> Result<Value> {
    c.query_row("SELECT a.kind,a.latest_version,a.published_version,r.version,r.name,r.config,r.digest,r.source,r.generation,r.status,r.created_by,r.created_at,r.published_by,r.published_at,r.revoked_by,r.revoked_at FROM design_assets a JOIN design_revisions r ON r.asset_id=a.id AND r.version=COALESCE(?2,a.latest_version) WHERE a.id=?1",params![id,version],|r|Ok(json!({"id":id,"kind":r.get::<_,String>(0)?,"latest_version":r.get::<_,u64>(1)?,"published_version":r.get::<_,Option<u64>>(2)?,"version":r.get::<_,u64>(3)?,"name":r.get::<_,String>(4)?,"config":decode(r.get(5)?)?,"digest":r.get::<_,String>(6)?,"source":r.get::<_,String>(7)?,"generation":decode(r.get(8)?)?,"status":r.get::<_,String>(9)?,"created_by":r.get::<_,String>(10)?,"created_at":r.get::<_,String>(11)?,"published_by":r.get::<_,String>(12)?,"published_at":r.get::<_,String>(13)?,"revoked_by":r.get::<_,String>(14)?,"revoked_at":r.get::<_,String>(15)?}))).optional()?.ok_or_else(missing)
}
fn confirmed(c: &Connection, id: &str, b: &Value, published: bool) -> Result<Value> {
    let version = b["version"]
        .as_u64()
        .filter(|n| *n > 0)
        .ok_or_else(|| bad("Invalid design version"))?;
    let v = get(c, id, Some(version))?;
    if v["digest"] != b["digest"]
        || digest(&json!({"kind":v["kind"],"name":v["name"],"config":v["config"]}))
            != v["digest"].as_str().unwrap_or("")
    {
        return Err(conflict());
    }
    if published && v["status"] != "published" {
        return Err(bad("Design version must be published"));
    }
    Ok(v)
}
pub(super) fn resolve(c: &Connection, reference: &Value) -> Result<Value> {
    let id = string(reference, "id", 1, 32)?;
    let mut resolved = reference.clone();
    if resolved["digest"].is_null() {
        let version = resolved["version"]
            .as_u64()
            .filter(|v| *v > 0)
            .ok_or_else(|| bad("Invalid design version"))?;
        resolved["digest"] = get(c, id, Some(version))?["digest"].clone();
    }
    confirmed(c, id, &resolved, true)
}
fn audit(c: &Connection, u: &User, v: &Value, action: &str, comment: &str) -> Result<()> {
    if comment.len() > 2000 {
        return Err(bad("Design comment too long"));
    }
    c.execute("INSERT INTO design_audit(id,asset_id,version,digest,action,actor,comment,created_at) VALUES(?1,?2,?3,?4,?5,?6,?7,?8)",params![uid(),v["id"].as_str(),v["version"].as_u64(),v["digest"].as_str(),action,u.id,comment,now(c)?])?;
    emit(
        c,
        u,
        &format!("design.{action}"),
        json!({"asset_id":v["id"],"kind":v["kind"],"version":v["version"],"digest":v["digest"],"comment":comment,"source":v["source"]}),
    )
}
pub(super) fn validate(c: &Connection, kind: &str, v: &Value) -> Result<()> {
    object(v)?;
    match kind {
        "routing" => {
            fields(
                v,
                &[
                    "task_type",
                    "assignments",
                    "budget",
                    "max_parallel",
                    "rules",
                    "description",
                ],
            )?;
            string(v, "task_type", 1, 120)?;
            if let Some(budget) = v.get("budget") {
                if !["low", "medium", "high"].contains(&budget.as_str().unwrap_or("")) {
                    return Err(bad("Invalid routing budget"));
                }
            }
            if v.get("max_parallel")
                .is_some_and(|n| n.as_u64().is_none_or(|n| !(1..=32).contains(&n)))
            {
                return Err(bad(
                    "Routing parallelism must be an integer between 1 and 32",
                ));
            }
            if v.get("description").is_some() {
                string(v, "description", 0, 4000)?;
            }
            fields(
                &v["assignments"],
                &["planner", "developer", "tester", "reviewer"],
            )?;
            if object(&v["assignments"])?.is_empty() {
                return Err(bad("Routing requires a model assignment"));
            }
            if let Some(rules) = v.get("rules") {
                fields(
                    rules,
                    &[
                        "allowed_provider_ids",
                        "allow_external",
                        "require_tools",
                        "max_model_calls",
                    ],
                )?;
                bool_or(rules, "allow_external", false)?;
                bool_or(rules, "require_tools", true)?;
                if rules
                    .get("max_model_calls")
                    .is_some_and(|n| n.as_u64().is_none_or(|n| !(1..=10000).contains(&n)))
                {
                    return Err(bad("Invalid routing call budget"));
                }
                if let Some(list) = rules.get("allowed_provider_ids") {
                    for pid in list
                        .as_array()
                        .ok_or_else(|| bad("Provider allowlist must be an array"))?
                    {
                        let enabled = c
                            .query_row(
                                "SELECT enabled FROM providers WHERE id=?1",
                                [pid.as_str()
                                    .ok_or_else(|| bad("Provider IDs must be strings"))?],
                                |r| r.get::<_, bool>(0),
                            )
                            .optional()?;
                        if enabled != Some(true) {
                            return Err(bad("Allowed provider is unavailable"));
                        }
                    }
                }
            }
            object(&v["assignments"])?;
            for model in object(&v["assignments"])?.values() {
                let m = get_config(
                    c,
                    ConfigTable::Model,
                    model
                        .as_str()
                        .ok_or_else(|| bad("Routing models must be IDs"))?,
                )?;
                if m["enabled"] == false {
                    return Err(bad("Routing model is disabled"));
                }
                let external=c.query_row("SELECT p.enabled,r.config FROM providers p JOIN provider_revisions r ON r.id=p.revision_id WHERE p.id=?1",[m["provider_id"].as_str().unwrap_or("")],|r|Ok((r.get::<_,bool>(0)?,decode(r.get(1)?)?)))?;
                if !external.0
                    || v["rules"]["allow_external"] == false && external.1["external"] != false
                {
                    return Err(bad("Routing provider violates policy"));
                }
                if v["rules"]["allowed_provider_ids"]
                    .as_array()
                    .is_some_and(|ids| !ids.is_empty() && !ids.contains(&m["provider_id"]))
                {
                    return Err(bad("Routing model is outside the provider allowlist"));
                }
                if v["rules"]["require_tools"] == true && m["supports_tools"] != true {
                    return Err(bad("Routing requires tool support"));
                }
            }
        }
        "team" => {
            fields(v, &["members", "workflow", "description"])?;
            let members = v["members"]
                .as_array()
                .ok_or_else(|| bad("Team requires members"))?;
            if members.is_empty() || members.len() > 30 {
                return Err(bad("Team member count must be 1..30"));
            }
            let keys: BTreeSet<_> = members
                .iter()
                .map(|m| m["key"].as_str().unwrap_or(""))
                .collect();
            if keys.len() != members.len() || keys.contains("") {
                return Err(bad("Team keys must be unique"));
            }
            for m in members {
                if let Some(recipients) = m["communication"]["recipients"].as_array() {
                    let mut used = BTreeSet::new();
                    for r in recipients {
                        let key = r
                            .as_str()
                            .ok_or_else(|| bad("Team recipients must be member keys"))?;
                        if !keys.contains(key) || !used.insert(key) {
                            return Err(bad("Team recipients must be distinct team members"));
                        }
                    }
                }
                let mut agent = m.clone();
                agent.as_object_mut().unwrap().remove("key");
                agent["reports_to"] = Value::Null;
                agent["communication"] = json!({"scope":"run","recipients":[]});
                validate_agent(c, None, &agent, None)?;
                let model = get_config(
                    c,
                    ConfigTable::Model,
                    m["model_profile_id"]
                        .as_str()
                        .ok_or_else(|| bad("Every team member needs a model"))?,
                )?;
                if model["enabled"] == false || model["supports_tools"] != true {
                    return Err(bad("Team model must be enabled and support tools"));
                }
                let mut seen = BTreeSet::from([m["key"].as_str().unwrap()]);
                let mut parent = m["reports_to"].as_str();
                while let Some(p) = parent {
                    if !keys.contains(p) || !seen.insert(p) {
                        return Err(bad("Invalid team reporting graph"));
                    }
                    parent = members
                        .iter()
                        .find(|m| m["key"] == p)
                        .and_then(|m| m["reports_to"].as_str());
                }
            }
        }
        "office" => {
            organization::validate(v)?;
            for (aid, position) in object(&v["positions"])? {
                if get_config(c, ConfigTable::Agent, aid)?["enabled"] == false {
                    return Err(bad("Office member is disabled"));
                }
                number(position, "x", -12., 12.)?;
                number(position, "z", -8., 8.)?;
                number(position, "floor", 1., 10.)?;
            }
        }
        "workflow" => validate_graph(c, &v["graph"])?,
        "capability" => {
            fields(
                v,
                &[
                    "model_profile_id",
                    "benchmark_id",
                    "system_scores",
                    "overrides",
                    "reason",
                ],
            )?;
            let model = get_config(c, ConfigTable::Model, string(v, "model_profile_id", 1, 32)?)?;
            for group in ["system_scores", "overrides"] {
                if let Some(scores) = v.get(group) {
                    fields(
                        scores,
                        &[
                            "coding",
                            "reasoning",
                            "tool",
                            "latency",
                            "cost",
                            "reliability",
                        ],
                    )?;
                    for key in object(scores)?.keys() {
                        if !scores[key].is_null() {
                            number(scores, key, 0., 100.)?;
                        }
                    }
                }
            }
            if v["overrides"].as_object().is_some_and(|m| !m.is_empty())
                && v["reason"].as_str().is_none_or(|s| s.trim().is_empty())
            {
                return Err(bad("Manual score overrides require evidence"));
            }
            if let Some(id) = v["benchmark_id"].as_str().filter(|s| !s.is_empty()) {
                let record=c.query_row("SELECT model_id,model_version,provider_revision,status,result FROM model_benchmarks WHERE id=?1",[id],|r|Ok((r.get::<_,String>(0)?,r.get::<_,u64>(1)?,r.get::<_,String>(2)?,r.get::<_,String>(3)?,decode(r.get(4)?)?))).optional()?.ok_or_else(||bad("Benchmark evidence does not exist"))?;
                let current: String = c.query_row(
                    "SELECT revision_id FROM providers WHERE id=?1",
                    [model["provider_id"].as_str().unwrap_or("")],
                    |r| r.get(0),
                )?;
                if model["id"] != record.0
                    || model["version"] != record.1
                    || record.2 != current
                    || record.3 != "completed"
                    || v["system_scores"] != record.4["scores"]
                {
                    return Err(bad(
                        "Capability scores require matching current benchmark evidence",
                    ));
                }
            } else if v["system_scores"]
                .as_object()
                .is_some_and(|s| s.values().any(|v| !v.is_null()))
            {
                return Err(bad("System scores require measured benchmark evidence"));
            }
        }
        "benchmark" => {
            fields(v, &["cases", "description"])?;
            let cases = v["cases"]
                .as_array()
                .filter(|v| !v.is_empty() && v.len() <= 12)
                .ok_or_else(|| bad("Benchmark requires 1..12 cases"))?;
            let mut ids = BTreeSet::new();
            for case in cases {
                fields(case, &["id", "dimension", "prompt", "expected", "match"])?;
                if !ids.insert(string(case, "id", 1, 64)?) {
                    return Err(bad("Benchmark case IDs must be unique"));
                }
                string(case, "prompt", 1, 12000)?;
                let expected = string(case, "expected", 1, 12000)?;
                if !["coding", "reasoning"].contains(&case["dimension"].as_str().unwrap_or("")) {
                    return Err(bad("Invalid benchmark dimension"));
                }
                match case["match"].as_str().unwrap_or("exact") {
                    "exact" | "contains" => (),
                    "json" => {
                        serde_json::from_str::<Value>(expected)
                            .map_err(|_| bad("Benchmark expected JSON is invalid"))?;
                    }
                    _ => return Err(bad("Invalid benchmark matcher")),
                }
            }
        }
        _ => return Err(bad("Unknown design kind")),
    }
    Ok(())
}

pub(super) fn generated(
    c: &Connection,
    user: &User,
    kind: &str,
    name: &str,
    config: &Value,
    generation: &Value,
    source: &str,
) -> Result<Value> {
    validate(c, kind, config)?;
    let id = uid();
    c.execute("INSERT INTO design_assets(id,kind,latest_version,published_version,lock_version,created_by,created_at) VALUES(?1,?2,1,NULL,0,?3,?4)",params![id,kind,user.id,now(c)?])?;
    insert_revision(c, user, &id, kind, 1, &json!({"name":name,"config":config}))?;
    c.execute(
        "UPDATE design_revisions SET source=?1,generation=?2 WHERE asset_id=?3 AND version=1",
        params![source, encode(generation), id],
    )?;
    let created = get(c, &id, Some(1))?;
    audit(c, user, &created, "proposed", "")?;
    Ok(created)
}
