use super::*;
use std::io::{Cursor, Read};

pub(super) fn request(
    c: &Connection,
    u: &User,
    m: &str,
    p: &[&str],
    q: &BTreeMap<String, String>,
    b: &Value,
    raw: &[u8],
) -> Result<ProductReply> {
    let (status, v) = match (m, p) {
        ("GET", ["skill-store", "sources"]) => {
            let mut s = c.prepare("SELECT id FROM skill_store_sources ORDER BY id")?;
            let ids = s
                .query_map([], |r| r.get::<_, String>(0))?
                .collect::<std::result::Result<Vec<_>, _>>()?;
            (
                200,
                json!(ids
                    .into_iter()
                    .map(|id| source(c, &id))
                    .collect::<Result<Vec<_>>>()?),
            )
        }
        ("POST", ["skill-store", "sources"]) => {
            let cfg = source_config(b)?;
            let id = uid();
            if cfg["kind"]=="github"&&c.query_row("SELECT COUNT(*) FROM skill_store_sources WHERE json_extract(config,'$.repository')=?1 AND json_extract(config,'$.ref')=?2",params![cfg["repository"].as_str(),cfg["ref"].as_str()],|r|r.get::<_,u64>(0))?>0{return Err(conflict())}
            c.execute("INSERT INTO skill_store_sources(id,config,version,enabled,last_sync,resolved_ref,last_error,summary) VALUES(?1,?2,1,1,'','','','{}')",params![id,encode(&cfg)])?;
            emit(
                c,
                u,
                "skill_store.source.created",
                json!({"source_id":id,"config":cfg}),
            )?;
            (201, source(c, &id)?)
        }
        ("PUT", ["skill-store", "sources", id]) => {
            let cfg = source_config(b)?;
            let old = source(c, id)?;
            if old["version"] != b["expected_version"] {
                return Err(conflict());
            }
            if old["kind"] != cfg["kind"] || old["repository"] != cfg["repository"] {
                return Err(bad("Source kind and repository cannot change"));
            }
            if c.execute("UPDATE skill_store_sources SET config=?1,version=version+1 WHERE id=?2 AND version=?3",params![encode(&cfg),id,b["expected_version"].as_u64()])?!=1{return Err(conflict())}
            emit(
                c,
                u,
                "skill_store.source.updated",
                json!({"source_id":id,"config":cfg}),
            )?;
            (200, source(c, id)?)
        }
        ("POST", ["skill-store", "sources", id, "enabled"]) => {
            fields(b, &["enabled", "expected_version"])?;
            let enabled = bool_or(b, "enabled", true)?;
            if c.execute("UPDATE skill_store_sources SET enabled=?1,version=version+1 WHERE id=?2 AND version=?3",params![enabled,id,b["expected_version"].as_u64()])?!=1{return Err(conflict())}
            emit(
                c,
                u,
                "skill_store.source.enabled",
                json!({"source_id":id,"enabled":enabled}),
            )?;
            (200, source(c, id)?)
        }
        ("POST", ["skill-store", "sources", id, "archive"]) => {
            let version = q
                .get("expected_version")
                .and_then(|v| v.parse::<u64>().ok())
                .ok_or_else(|| bad("expected_version is required"))?;
            let src = source(c, id)?;
            if src["version"] != version || src["enabled"] != true {
                return Err(conflict());
            }
            if !["github", "archive"].contains(&src["kind"].as_str().unwrap_or("")) {
                return Err(bad("Source does not accept archives"));
            }
            let files = archive_files(raw)?;
            let origin = json!({"verification":"user_archive","archive_sha256":format!("{:x}",Sha256::digest(raw)),"commit":""});
            let summary = apply_archive(c, u, id, version, &files, &origin)?;
            (200, json!({"source":source(c,id)?,"summary":summary}))
        }
        ("GET", [kind @ ("skill-store" | "role-templates"), "items"]) => {
            let native = if *kind == "skill-store" {
                "skill"
            } else {
                "role_template"
            };
            let offset = q
                .get("offset")
                .and_then(|v| v.parse::<usize>().ok())
                .unwrap_or(0);
            let limit = q
                .get("limit")
                .and_then(|v| v.parse::<usize>().ok())
                .unwrap_or(24);
            if !(1..=100).contains(&limit) {
                return Err(bad("limit must be 1..100"));
            }
            let search = q.get("q").cloned().unwrap_or_default().to_lowercase();
            if search.len() > 200 {
                return Err(bad("Search too long"));
            }
            let mut s = c.prepare("SELECT id FROM skill_store_items ORDER BY id")?;
            let ids = s
                .query_map([], |r| r.get::<_, String>(0))?
                .collect::<std::result::Result<Vec<_>, _>>()?;
            let mut values = Vec::new();
            let mut categories = Map::new();
            for id in ids {
                let v = item(c, &id, None, false)?;
                if v["kind"] != native {
                    continue;
                }
                if let Some(category) = v["category"].as_str() {
                    categories.insert(category.into(), v["category_label"].clone());
                }
                if q.get("source_id")
                    .is_some_and(|x| !x.is_empty() && v["source_id"] != *x)
                    || q.get("category")
                        .is_some_and(|x| !x.is_empty() && v["category"] != *x)
                    || q.get("installed")
                        .is_some_and(|x| x == "true" && v["skill_id"].is_null())
                {
                    continue;
                }
                if !search.is_empty()
                    && !format!("{} {} {}", v["title"], v["description"], v["path"])
                        .to_lowercase()
                        .contains(&search)
                {
                    continue;
                }
                values.push(v)
            }
            values.sort_by_key(|v| v["title"].as_str().unwrap_or("").to_lowercase());
            let total = values.len();
            let values = values
                .into_iter()
                .skip(offset)
                .take(limit)
                .collect::<Vec<_>>();
            (
                200,
                json!({"items":values,"total":total,"offset":offset,"has_more":offset+limit<total,"categories":categories}),
            )
        }
        ("GET", [kind @ ("skill-store" | "role-templates"), "items", id]) => {
            let v = item(c, id, q.get("revision_id").map(String::as_str), true)?;
            if (v["kind"] == "skill") != (*kind == "skill-store") {
                return Err(bad("Use the catalog for this asset kind"));
            }
            (200, v)
        }
        ("POST", ["skill-store", "items", id, "install"]) => {
            fields(
                b,
                &[
                    "revision_id",
                    "digest",
                    "expected_version",
                    "reviewed_source",
                ],
            )?;
            if b["reviewed_source"] != true {
                return Err(bad("Review source and license before installation"));
            }
            let v = item(c, id, b["revision_id"].as_str(), true)?;
            if v["kind"] != "skill" {
                return Err(bad("Only native Skills can be installed"));
            }
            verify_item(&v, b, true)?;
            let rid = v["revision_id"].as_str().unwrap();
            if let Some((sid, version)) = c
                .query_row(
                    "SELECT skill_id,skill_version FROM skill_store_installs WHERE revision_id=?1",
                    [rid],
                    |r| Ok((r.get::<_, String>(0)?, r.get::<_, u64>(1)?)),
                )
                .optional()?
            {
                (
                    200,
                    json!({"skill":skills::get(c,&sid,Some(version),true)?,"created":false,"item":v}),
                )
            } else {
                let sid = v["skill_id"].as_str();
                if sid.is_some() && b["expected_version"] != v["local_latest_version"] {
                    return Err(conflict());
                }
                let saved = skills::save(
                    c,
                    u,
                    sid,
                    sid.and_then(|_| b["expected_version"].as_u64()),
                    &v["files"],
                )?;
                let sid = saved["id"].as_str().unwrap();
                let version = saved["version"].as_u64().unwrap();
                c.execute("INSERT INTO skill_store_installs(revision_id,item_id,skill_id,skill_version,digest,created_at) VALUES(?1,?2,?3,?4,?5,?6)",params![rid,id,sid,version,saved["digest"].as_str(),now(c)?])?;
                c.execute(
                    "UPDATE skill_store_items SET skill_id=?1,installed_version=?2 WHERE id=?3",
                    params![sid, version, id],
                )?;
                emit(
                    c,
                    u,
                    "skill_store.installed",
                    json!({"item_id":id,"revision_id":rid,"skill_id":sid,"version":version}),
                )?;
                (
                    200,
                    json!({"skill":saved,"created":true,"item":item(c,id,Some(rid),true)?}),
                )
            }
        }
        ("POST", ["skill-store", "items", id, "bind"]) => {
            fields(
                b,
                &[
                    "revision_id",
                    "agent_id",
                    "expected_agent_version",
                    "mode",
                    "parameters",
                ],
            )?;
            let v = item(c, id, b["revision_id"].as_str(), false)?;
            let sid = v["skill_id"]
                .as_str()
                .ok_or_else(|| bad("Install and approve this Skill first"))?;
            let version = v["installed_version"]
                .as_u64()
                .ok_or_else(|| bad("Revision not installed"))?;
            let aid = string(b, "agent_id", 1, 32)?;
            let mut a = get_config(c, ConfigTable::Agent, aid)?;
            if a["version"] != b["expected_agent_version"] {
                return Err(conflict());
            }
            let mode = b["mode"].as_str().unwrap_or("on_demand");
            if !["always", "on_demand"].contains(&mode) {
                return Err(bad("Invalid Skill activation mode"));
            }
            let parameters = b.get("parameters").cloned().unwrap_or(json!({}));
            object(&parameters)?;
            let binding = json!({"skill_id":sid,"version":version,"mode":mode,"enabled":true,"allow_scripts":false,"parameters":parameters});
            let mut bindings = a["skills"].as_array().cloned().unwrap_or_default();
            bindings.retain(|b| b["skill_id"] != sid);
            bindings.push(binding);
            skills::validate_bindings(c, &json!(bindings))?;
            a["skills"] = json!(bindings);
            (
                200,
                save_config(
                    c,
                    u,
                    ConfigTable::Agent,
                    aid,
                    a,
                    b["expected_agent_version"].as_u64(),
                )?,
            )
        }
        ("POST", ["skill-store", "items", id, "uninstall"]) => {
            let v = item(c, id, None, false)?;
            if v["local_latest_version"] != b["expected_version"] {
                return Err(conflict());
            }
            let sid = v["skill_id"]
                .as_str()
                .ok_or_else(|| bad("Item is not installed"))?;
            if configs(c, ConfigTable::Agent)?.iter().any(|a| {
                a["skills"]
                    .as_array()
                    .is_some_and(|bs| bs.iter().any(|b| b["skill_id"] == sid))
            }) {
                return Err(bad("Remove all member bindings before uninstalling"));
            }
            c.execute("UPDATE skills SET enabled=0 WHERE id=?1", [sid])?;
            emit(
                c,
                u,
                "skill_store.uninstalled",
                json!({"item_id":id,"skill_id":sid}),
            )?;
            (200, json!({"disabled":true,"history_preserved":true}))
        }
        ("POST", ["skill-store", "publish"]) => {
            let sid = string(b, "skill_id", 1, 32)?;
            let v = skills::get(c, sid, b["version"].as_u64(), true)?;
            if v["enabled"] != true || v["status"] != "approved" || v["digest"] != b["digest"] {
                return Err(conflict());
            }
            string(b, "title", 1, 160)?;
            let category = b["category"].as_str().unwrap_or("team");
            c.execute("INSERT OR IGNORE INTO skill_store_sources(id,config,version,enabled,last_sync,resolved_ref,last_error,summary) VALUES('team-library',?1,1,1,'','','','{}')",[encode(&json!({"name":"Team library","kind":"team","repository":"","ref":""}))])?;
            let src = source(c, "team-library")?;
            if src["enabled"] != true {
                return Err(conflict());
            }
            let details = json!({"kind":"skill","title":b["title"],"description":v["description"],"category":category,"category_label":category,"adapter":"native-skill-v1","license":"Team content","warnings":[],"scripts":[],"declared_tools":""});
            let path = format!("{sid}/SKILL.md");
            let item_id = save_item(
                c,
                "team-library",
                &path,
                &v["files"],
                &details,
                &json!({"verification":"team_publish","skill_id":sid,"version":v["version"],"actor":u.id}),
            )?;
            emit(
                c,
                u,
                "skill_store.published",
                json!({"skill_id":sid,"version":v["version"],"item_id":item_id}),
            )?;
            (201, item(c, &item_id, None, true)?)
        }
        ("POST", ["role-templates", "items", id, "proposal" | "propose-member"]) => {
            (200, proposal(c, id, b)?)
        }
        ("POST", ["role-templates", "compose"]) => {
            let name = string(b, "name", 1, 120)?;
            let selections = b["items"]
                .as_array()
                .filter(|v| !v.is_empty() && v.len() <= 30)
                .ok_or_else(|| bad("Select 1..30 role templates"))?;
            let mut members = Vec::new();
            let mut keys = BTreeSet::new();
            for s in selections {
                let mid = string(s, "item_id", 1, 32)?;
                let mut member = proposal(c, mid, s)?["member"].clone();
                let original = member["key"].as_str().unwrap_or("member").to_owned();
                let mut key = original.clone();
                let mut n = 1;
                while !keys.insert(key.clone()) {
                    n += 1;
                    key = format!("{original}_{n}");
                }
                member["key"] = json!(key);
                members.push(member);
            }
            return design::request(
                c,
                u,
                "POST",
                &["design", "assets"],
                q,
                &json!({"kind":"team","name":name,"config":{"members":members,"description":"","workflow":null}}),
            );
        }
        _ => return Err(missing()),
    };
    Ok(ProductReply::json(status, v))
}
pub(super) fn source(c: &Connection, id: &str) -> Result<Value> {
    c.query_row("SELECT config,version,enabled,last_sync,resolved_ref,last_error,summary FROM skill_store_sources WHERE id=?1",[id],|r|{let mut v=decode(r.get(0)?)?;v["id"]=json!(id);v["version"]=r.get::<_,u64>(1)?.into();v["enabled"]=r.get::<_,bool>(2)?.into();v["last_sync"]=r.get::<_,String>(3)?.into();v["resolved_ref"]=r.get::<_,String>(4)?.into();v["last_error"]=r.get::<_,String>(5)?.into();v["summary"]=decode(r.get(6)?)?;Ok(v)}).optional()?.ok_or_else(missing).and_then(|mut v|{let mut s=c.prepare("SELECT json_extract(r.details,'$.kind'),COUNT(*) FROM skill_store_items i JOIN skill_store_revisions r ON r.id=i.latest_revision WHERE i.source_id=?1 AND i.available=1 GROUP BY json_extract(r.details,'$.kind')")?;let rows=s.query_map([id],|r|Ok((r.get::<_,String>(0)?,r.get::<_,u64>(1)?)))?.collect::<std::result::Result<Vec<_>,_>>()?;let mut counts=json!({"skill":0,"role_template":0});let mut total=0;for (kind,n) in rows{counts[&kind]=json!(n);total+=n;}v["counts"]=counts;v["count"]=json!(total);Ok(v)})
}
fn source_config(b: &Value) -> Result<Value> {
    fields(
        b,
        &["name", "kind", "repository", "ref", "expected_version"],
    )?;
    let name = string(b, "name", 1, 100)?;
    let kind = b["kind"].as_str().unwrap_or("github");
    let repository = b["repository"].as_str().unwrap_or("");
    let reference = b["ref"].as_str().unwrap_or("main");
    if reference.is_empty() || reference.len() > 180 {
        return Err(bad("Invalid repository ref"));
    }
    if kind == "github" {
        let url = url::Url::parse(repository).map_err(|_| bad("Invalid GitHub repository"))?;
        let parts = url.path().trim_matches('/').split('/').collect::<Vec<_>>();
        if url.scheme() != "https"
            || url.host_str() != Some("github.com")
            || url.query().is_some()
            || url.fragment().is_some()
            || !url.username().is_empty()
            || parts.len() != 2
            || parts.iter().any(|x| {
                x.is_empty()
                    || x.starts_with('.')
                    || !x
                        .bytes()
                        .all(|c| c.is_ascii_alphanumeric() || b"_.-".contains(&c))
            })
        {
            return Err(bad(
                "Use a public https://github.com/owner/repository root URL",
            ));
        }
    } else if kind != "archive" {
        return Err(bad("Invalid source kind"));
    }
    Ok(
        json!({"name":name,"kind":kind,"repository":repository.trim_end_matches('/').trim_end_matches(".git"),"ref":reference}),
    )
}
fn item(c: &Connection, id: &str, revision: Option<&str>, full: bool) -> Result<Value> {
    let mut v=c.query_row("SELECT i.source_id,i.path,i.latest_revision,i.available,i.skill_id,i.installed_version,r.id,r.files,r.details,r.origin,r.digest,r.created_at FROM skill_store_items i JOIN skill_store_revisions r ON r.item_id=i.id AND r.id=COALESCE(NULLIF(?2,''),i.latest_revision) WHERE i.id=?1",params![id,revision],|r|{let mut v=decode(r.get(8)?)?;v["id"]=json!(id);v["source_id"]=r.get::<_,String>(0)?.into();v["path"]=r.get::<_,String>(1)?.into();v["latest_revision"]=r.get::<_,String>(2)?.into();v["available"]=r.get::<_,bool>(3)?.into();v["skill_id"]=r.get::<_,String>(4)?.into();v["installed_version"]=r.get::<_,u64>(5)?.into();v["revision_id"]=r.get::<_,String>(6)?.into();v["files"]=decode(r.get(7)?)?;v["origin"]=decode(r.get(9)?)?;v["digest"]=r.get::<_,String>(10)?.into();v["created_at"]=r.get::<_,String>(11)?.into();Ok(v)}).optional()?.ok_or_else(missing)?;
    let src = source(c, v["source_id"].as_str().unwrap())?;
    v["source_name"] = src["name"].clone();
    v["source_enabled"] = src["enabled"].clone();
    let sid = v["skill_id"]
        .as_str()
        .filter(|s| !s.is_empty())
        .map(str::to_owned);
    let local = sid
        .as_ref()
        .map(|sid| skills::get(c, sid, None, full))
        .transpose()?;
    v["skill_id"] = json!(sid);
    v["local_latest_version"] = local
        .as_ref()
        .map(|v| v["latest_version"].clone())
        .unwrap_or(Value::Null);
    v["local_enabled"] = local
        .as_ref()
        .map(|v| v["enabled"].clone())
        .unwrap_or(Value::Null);
    let installed=c.query_row("SELECT s.skill_version,r.status,r.digest FROM skill_store_installs s JOIN skill_revisions r ON r.skill_id=s.skill_id AND r.version=s.skill_version WHERE s.revision_id=?1",[v["revision_id"].as_str().unwrap()],|r|Ok((r.get::<_,u64>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?))).optional()?;
    v["installed_version"] = installed
        .as_ref()
        .map(|i| json!(i.0))
        .unwrap_or(Value::Null);
    v["installed_status"] = installed
        .as_ref()
        .map(|i| json!(i.1))
        .unwrap_or(Value::Null);
    v["installed_digest"] = installed
        .as_ref()
        .map(|i| json!(i.2))
        .unwrap_or(Value::Null);
    v["update_available"] = json!(sid.is_some() && installed.is_none());
    let mut bindings = Vec::new();
    if let Some(sid) = &sid {
        for agent in configs(c, ConfigTable::Agent)? {
            for b in agent["skills"].as_array().cloned().unwrap_or_default() {
                if b["skill_id"] == *sid {
                    bindings.push(json!({"agent_id":agent["id"],"name":agent["name"],"version":b["version"],"enabled":b["enabled"]}))
                }
            }
        }
    }
    v["bindings"] = json!(bindings);
    if v["kind"] == "role_template" {
        let doc = v["files"]
            .get("ROLE.md")
            .or_else(|| v["files"].get("references/original-agent.md"))
            .cloned()
            .ok_or_else(|| bad("Role revision lacks original document"))?;
        v["document"] = json!({"path":"ROLE.md","content":doc,"format":"markdown"});
    }
    if full {
        let mut s=c.prepare("SELECT id,digest,origin,created_at FROM skill_store_revisions WHERE item_id=?1 ORDER BY created_at DESC")?;
        v["versions"]=json!(s.query_map([id],|r|Ok(json!({"revision_id":r.get::<_,String>(0)?,"digest":r.get::<_,String>(1)?,"origin":decode(r.get(2)?)?,"created_at":r.get::<_,String>(3)?})))?.collect::<std::result::Result<Vec<_>,_>>()?);
        v["diff"] = local
            .as_ref()
            .map(|local| file_diffs(&local["files"], &v["files"]))
            .unwrap_or(json!([]));
    } else {
        v.as_object_mut().unwrap().remove("files");
        v.as_object_mut().unwrap().remove("document");
    }
    Ok(v)
}
fn file_diffs(local: &Value, remote: &Value) -> Value {
    let paths: BTreeSet<_> = local
        .as_object()
        .into_iter()
        .flat_map(|v| v.keys())
        .chain(remote.as_object().into_iter().flat_map(|v| v.keys()))
        .collect();
    json!(paths
        .into_iter()
        .filter(|p| local[*p] != remote[*p])
        .take(80)
        .map(|path| {
            let before: Vec<_> = local[path].as_str().unwrap_or("").lines().collect();
            let after: Vec<_> = remote[path].as_str().unwrap_or("").lines().collect();
            let prefix = before
                .iter()
                .zip(&after)
                .take_while(|(a, b)| a == b)
                .count();
            let suffix = before[prefix..]
                .iter()
                .rev()
                .zip(after[prefix..].iter().rev())
                .take_while(|(a, b)| a == b)
                .count();
            let start = prefix.saturating_sub(3);
            let old_end = (before.len() - suffix + 3).min(before.len());
            let new_end = (after.len() - suffix + 3).min(after.len());
            let mut diff = format!(
                "--- installed/{path}\n+++ store/{path}\n@@ -{},{} +{},{} @@\n",
                if before.is_empty() { 0 } else { start + 1 },
                old_end - start,
                if after.is_empty() { 0 } else { start + 1 },
                new_end - start
            );
            for line in &before[start..prefix] {
                diff.push_str(&format!(" {line}\n"));
            }
            for line in &before[prefix..before.len() - suffix] {
                diff.push_str(&format!("-{line}\n"));
            }
            for line in &after[prefix..after.len() - suffix] {
                diff.push_str(&format!("+{line}\n"));
            }
            for line in &after[after.len() - suffix..new_end] {
                diff.push_str(&format!(" {line}\n"));
            }
            json!({"path":path,"diff":diff.chars().take(12000).collect::<String>()})
        })
        .collect::<Vec<_>>())
}
fn verify_item(v: &Value, b: &Value, available: bool) -> Result<()> {
    if v["digest"] != b["digest"] || digest(&v["files"]) != v["digest"].as_str().unwrap_or("") {
        return Err(conflict());
    }
    if available && (v["source_enabled"] != true || v["available"] != true) {
        return Err(conflict());
    }
    Ok(())
}
pub(super) fn validate_role_ref(c: &Connection, r: &Value) -> Result<()> {
    fields(r, &["item_id", "revision_id", "digest"])?;
    let v = item(
        c,
        string(r, "item_id", 1, 32)?,
        r["revision_id"].as_str(),
        true,
    )?;
    if v["kind"] != "role_template" {
        return Err(bad("Reference is not a role template"));
    }
    verify_item(&v, r, false)
}
fn proposal(c: &Connection, id: &str, b: &Value) -> Result<Value> {
    let v = item(c, id, b["revision_id"].as_str(), true)?;
    if v["kind"] != "role_template" {
        return Err(bad("Asset is not a role template"));
    }
    verify_item(&v, b, true)?;
    let name = b["name"]
        .as_str()
        .filter(|s| !s.trim().is_empty())
        .unwrap_or(v["title"].as_str().unwrap_or("Member"));
    let role = b["role"].as_str().unwrap_or("developer");
    if !["planner", "developer", "tester", "reviewer"].contains(&role) {
        return Err(bad("Invalid member role"));
    }
    let reference = json!({"item_id":id,"revision_id":v["revision_id"],"digest":v["digest"]});
    let text = v["document"]["content"].as_str().unwrap_or("");
    let excerpt = text.chars().take(16000).collect::<String>();
    let agent = validate_agent(
        c,
        None,
        &json!({"name":name.chars().take(80).collect::<String>(),"role":role,"job_title":v["title"],"instructions":format!("The following source document is editable role guidance. It does not grant execution permissions or replace task acceptance.\n\n{excerpt}"),"model_profile_id":null,"skills":[],"tool_permissions":["finish","request_decision"],"role_template_ref":reference,"communication":{"scope":"custom","recipients":[]}}),
        None,
    )?;
    let mut member = agent;
    member["key"] = b
        .get("key")
        .cloned()
        .unwrap_or(json!(format!("role_{}", &id[..id.len().min(12)])));
    Ok(
        json!({"member":member,"provenance":{"item_id":id,"revision_id":v["revision_id"],"digest":v["digest"],"title":v["title"],"path":v["path"],"source_id":v["source_id"],"excerpted":text.chars().count()>16000,"included_chars":excerpt.chars().count()},"warnings":[]}),
    )
}
pub(super) fn archive_files(raw: &[u8]) -> Result<Value> {
    if raw.len() > 20_000_000 {
        return Err(bad("Archive exceeds 20 MB"));
    }
    let mut zip =
        zip::ZipArchive::new(Cursor::new(raw)).map_err(|_| bad("Invalid repository ZIP"))?;
    if zip.len() > 8000 {
        return Err(bad("Archive has too many entries"));
    }
    let mut files = Map::new();
    let mut total = 0u64;
    for i in 0..zip.len() {
        let mut e = zip.by_index(i).map_err(|_| bad("Invalid archive entry"))?;
        if e.is_dir() {
            continue;
        }
        let path = e.name().to_owned();
        if e.is_symlink()
            || e.encrypted()
            || path.contains(['\\', '\0'])
            || path.starts_with('/')
            || path
                .split('/')
                .any(|p| p == ".." || p == "." || p.is_empty())
        {
            return Err(bad("Unsafe archive path"));
        }
        total += e.size();
        if total > 60_000_000 {
            return Err(bad("Expanded archive exceeds 60 MB"));
        }
        if e.size() > 200000 {
            files.insert(path, Value::Null);
            continue;
        }
        let mut text = String::new();
        if e.read_to_string(&mut text).is_err() {
            files.insert(path, Value::Null);
            continue;
        }
        if files
            .insert(path, json!(text.replace("\r\n", "\n")))
            .is_some()
        {
            return Err(bad("Duplicate archive entry"));
        }
    }
    let prefixes = files
        .keys()
        .filter_map(|p| p.split_once('/').map(|(prefix, _)| prefix.to_owned()))
        .collect::<BTreeSet<_>>();
    if prefixes.len() == 1 && files.keys().all(|p| p.contains('/')) {
        let prefix = format!("{}/", prefixes.first().unwrap());
        files = files
            .into_iter()
            .map(|(k, v)| (k.strip_prefix(&prefix).unwrap().to_owned(), v))
            .collect();
    }
    Ok(Value::Object(files))
}
pub(super) fn apply_archive(
    c: &Connection,
    u: &User,
    id: &str,
    version: u64,
    files: &Value,
    origin: &Value,
) -> Result<Value> {
    let files = object(files)?;
    let roots = files
        .keys()
        .filter(|p| p.as_str() == "SKILL.md" || p.ends_with("/SKILL.md"))
        .filter_map(|p| p.strip_suffix("SKILL.md"))
        .collect::<Vec<_>>();
    let license = files
        .iter()
        .find(|(name, _)| {
            ["LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE"].contains(&name.as_str())
        })
        .map(|(_, v)| v.clone());
    let mut candidates = Vec::new();
    let mut rejected = Vec::new();
    for root in &roots {
        if roots.iter().any(|r| r != root && r.starts_with(root)) {
            rejected.push(json!({"path":format!("{root}SKILL.md"),"reason":"Nested Skill roots must be separate packages"}));
            continue;
        }
        let mut subset = files
            .iter()
            .filter_map(|(p, v)| {
                p.strip_prefix(root).map(|rel| {
                    (
                        if ["LICENSE", "LICENCE", "NOTICE"].contains(&rel) {
                            format!("{rel}.txt")
                        } else {
                            rel.to_owned()
                        },
                        v.clone(),
                    )
                })
            })
            .collect::<Map<_, _>>();
        if let Some(license) = &license {
            subset.insert(
                "references/forge-upstream-license.txt".into(),
                license.clone(),
            );
        }
        match skills::validate_files(&Value::Object(subset)) {
            Ok((files, manifest)) => {
                let category = manifest["metadata"]["category"].as_str().unwrap_or("team");
                let details = json!({"kind":"skill","title":manifest["name"],"description":manifest["description"],"category":category,"category_label":category,"adapter":"native-skill-v1","license":if license.is_some(){"See source license"}else{"No license text supplied"},"warnings":[],"scripts":object(&files)?.keys().filter(|p|p.starts_with("scripts/")).collect::<Vec<_>>(),"declared_tools":manifest.get("allowed-tools").cloned().unwrap_or(json!(""))});
                candidates.push((format!("{root}SKILL.md"), files, details));
            }
            Err(error) => {
                rejected.push(json!({"path":format!("{root}SKILL.md"),"reason":error.detail}))
            }
        }
    }
    for (path, raw) in files {
        let text = raw.as_str().unwrap_or("");
        if !path.ends_with(".md")
            || path.ends_with("SKILL.md")
            || roots.iter().any(|r| path.starts_with(r))
            || [
                "scripts/",
                "integrations/",
                "examples/",
                "node_modules/",
                "docs/",
            ]
            .iter()
            .any(|r| path.starts_with(r))
        {
            continue;
        }
        if let Some((header, body)) = text
            .strip_prefix("---\n")
            .and_then(|s| s.split_once("\n---\n"))
        {
            let metadata = serde_yaml::from_str::<serde_yaml::Value>(header)
                .ok()
                .and_then(|v| serde_json::to_value(v).ok());
            if let Some(meta) = metadata {
                if let (Some(name), Some(description)) =
                    (meta["name"].as_str(), meta["description"].as_str())
                {
                    if !name.is_empty() && !description.is_empty() && !body.trim().is_empty() {
                        let category = path.split('/').next().unwrap_or("specialized");
                        let mut package = json!({"ROLE.md":text});
                        if let Some(license) = &license {
                            package["references/forge-upstream-license.txt"] = license.clone();
                        }
                        candidates.push((path.clone(),package,json!({"kind":"role_template","title":name,"description":description,"category":category,"category_label":category,"adapter":"forge-role-document-v2","original_metadata":meta,"warnings":[],"license":if license.is_some(){"See source license"}else{"No license text supplied"},"scripts":[],"declared_tools":""})));
                    }
                }
            }
        }
    }
    if candidates.is_empty() || candidates.len() > 1500 {
        return Err(bad(
            "Archive has no compatible assets or exceeds 1500 items",
        ));
    }
    if c.execute(
        "UPDATE skill_store_sources SET version=version+1 WHERE id=?1 AND version=?2 AND enabled=1",
        params![id, version],
    )? != 1
    {
        return Err(conflict());
    }
    let mut added = 0;
    let mut updated = 0;
    let mut same = 0;
    let mut visible = BTreeSet::new();
    for (path, files, details) in candidates {
        let item_id = digest(&json!([id, path]))[..32].to_owned();
        let prior = c
            .query_row(
                "SELECT latest_revision FROM skill_store_items WHERE id=?1",
                [&item_id],
                |r| r.get::<_, String>(0),
            )
            .optional()?;
        let revision = digest(&json!([item_id, files, details]));
        match prior {
            None => added += 1,
            Some(old) if old == revision => same += 1,
            _ => updated += 1,
        }
        visible.insert(save_item(c, id, &path, &files, &details, origin)?);
    }
    let mut s = c.prepare("SELECT id FROM skill_store_items WHERE source_id=?1 AND available=1")?;
    let existing = s
        .query_map([id], |r| r.get::<_, String>(0))?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    let mut removed = 0;
    for old in existing {
        if !visible.contains(&old) {
            c.execute(
                "UPDATE skill_store_items SET available=0 WHERE id=?1",
                [old],
            )?;
            removed += 1
        }
    }
    let summary = json!({"added":added,"updated":updated,"unchanged":same,"removed":removed,"rejected_count":rejected.len(),"rejected":rejected});
    c.execute("UPDATE skill_store_sources SET last_sync=?1,resolved_ref=?2,last_error='',summary=?3 WHERE id=?4",params![now(c)?,origin["commit"].as_str().unwrap_or(""),encode(&summary),id])?;
    emit(
        c,
        u,
        "skill_store.synced",
        json!({"source_id":id,"summary":summary,"origin":origin}),
    )?;
    Ok(summary)
}
fn save_item(
    c: &Connection,
    source_id: &str,
    path: &str,
    files: &Value,
    details: &Value,
    origin: &Value,
) -> Result<String> {
    let id = digest(&json!([source_id, path]))[..32].to_owned();
    let revision = digest(&json!([id, files, details]));
    let mut origin = origin.clone();
    origin["source_id"] = json!(source_id);
    origin["path"] = json!(path);
    origin["source_name"] = source(c, source_id)?["name"].clone();
    origin["adapter"] = details["adapter"].clone();
    c.execute("INSERT OR IGNORE INTO skill_store_revisions(id,item_id,files,details,origin,digest,created_at) VALUES(?1,?2,?3,?4,?5,?6,?7)",params![revision,id,encode(files),encode(details),encode(&origin),digest(files),now(c)?])?;
    c.execute("INSERT INTO skill_store_items(id,source_id,path,latest_revision,available,skill_id,installed_version) VALUES(?1,?2,?3,?4,1,'',0) ON CONFLICT(id) DO UPDATE SET latest_revision=excluded.latest_revision,available=1",params![id,source_id,path,revision])?;
    Ok(id)
}
