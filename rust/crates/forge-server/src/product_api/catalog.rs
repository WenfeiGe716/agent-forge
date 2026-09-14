use super::*;

pub(super) fn request(
    c: &Connection,
    u: &User,
    m: &str,
    p: &[&str],
    b: &Value,
) -> Result<ProductReply> {
    let (status, v) = match (m, p) {
        ("GET", ["providers", id, "catalog"]) => (200, catalog(c, id)?),
        ("POST", ["providers", id, "catalog", action @ ("import" | "replace")]) => {
            fields(
                b,
                &["revision_id", "catalog_digest", "acknowledged", "model_ids"],
            )?;
            let cat = catalog(c, id)?;
            if b["acknowledged"] != true {
                return Err(bad("Catalog changes require explicit acknowledgement"));
            }
            if cat["revision_id"] != b["revision_id"]
                || cat["catalog_digest"] != b["catalog_digest"]
                || cat["catalog_digest"] == ""
            {
                return Err(conflict());
            }
            let available = cat["models"].as_array().unwrap();
            let selected = if *action == "replace" {
                available
                    .iter()
                    .filter_map(|m| m["id"].as_str().map(str::to_owned))
                    .collect::<Vec<_>>()
            } else {
                strings(b, "model_ids", 2000)?
            };
            if selected.is_empty()
                || selected
                    .iter()
                    .any(|id| !available.iter().any(|m| m["id"] == *id))
            {
                return Err(bad(
                    "Selected models are not in the current upstream catalog",
                ));
            }
            let profiles = configs(c, ConfigTable::Model)?
                .into_iter()
                .filter(|m| m["provider_id"] == *id)
                .collect::<Vec<_>>();
            let mut created = Vec::new();
            let mut retired = Vec::new();
            let mut restored = Vec::new();
            let mut kept = Vec::new();
            for mid in &selected {
                let matching = profiles
                    .iter()
                    .filter(|m| m["model_id"] == *mid)
                    .collect::<Vec<_>>();
                if matching.is_empty() {
                    let entry = available.iter().find(|m| m["id"] == *mid).unwrap();
                    let model = validate_model(
                        c,
                        &json!({"provider_id":id,"model_id":mid,"name":entry["name"].as_str().unwrap_or(mid),"enabled":false,"supports_tools":false,"allowed_parameters":[],"parameters":{},"context_window":entry["context_window"]}),
                    )?;
                    created.push(save_config(c, u, ConfigTable::Model, &uid(), model, None)?)
                } else {
                    for model in matching {
                        let changed=c.execute("UPDATE provider_catalog_model_states SET retired=0,updated_at=?1 WHERE id=?2 AND provider_id=?3 AND retired=1",params![now(c)?,model["id"].as_str(),id])?;
                        if changed > 0 {
                            restored.push(model.clone())
                        } else {
                            kept.push(model.clone())
                        }
                    }
                }
            }
            if *action == "replace" {
                for mut model in profiles {
                    if !selected.iter().any(|id| model["model_id"] == *id) {
                        let mid = model["id"].as_str().unwrap().to_owned();
                        let version = model["version"].as_u64();
                        model["enabled"] = json!(false);
                        let saved = save_config(c, u, ConfigTable::Model, &mid, model, version)?;
                        c.execute("INSERT INTO provider_catalog_model_states(id,provider_id,retired,updated_at) VALUES(?1,?2,1,?3) ON CONFLICT(id) DO UPDATE SET retired=1,updated_at=excluded.updated_at",params![mid,id,now(c)?])?;
                        retired.push(saved)
                    }
                }
            }
            emit(
                c,
                u,
                "provider.catalog.applied",
                json!({"provider_id":id,"revision_id":b["revision_id"],"catalog_digest":b["catalog_digest"],"action":action,"bindings_changed":false}),
            )?;
            (
                201,
                json!({"created":created,"retired":retired,"restored":restored,"kept":kept,"skipped":kept,"catalog":catalog(c,id)?}),
            )
        }
        ("POST", ["providers", id, "models", "bulk-state", "preview"]) => {
            (200, bulk_review(c, id, b)?)
        }
        ("POST", ["providers", id, "models", "bulk-state"]) => {
            if b["acknowledged"] != true {
                return Err(bad("Confirm the model state changes"));
            }
            let review = bulk_review(c, id, b)?;
            if b["digest"] != review["digest"] {
                return Err(conflict());
            }
            let mut changed = Vec::new();
            let mut unchanged = Vec::new();
            let mut changes = Vec::new();
            for item in review["items"].as_array().unwrap() {
                let mid = item["id"].as_str().unwrap();
                let mut model = get_config(c, ConfigTable::Model, mid)?;
                if model["enabled"] == b["enabled"] {
                    unchanged.push(model)
                } else {
                    let version = model["version"].as_u64().unwrap();
                    model["enabled"] = b["enabled"].clone();
                    changed.push(save_config(
                        c,
                        u,
                        ConfigTable::Model,
                        mid,
                        model,
                        Some(version),
                    )?);
                    changes.push(json!({"model_id":mid,"name":item["name"],"from_version":version,"to_version":version+1,"was_enabled":item["enabled"],"enabled":b["enabled"]}));
                }
            }
            let receipt = json!({"operation_id":uid(),"actor":u.id,"provider_id":id,"revision_id":b["revision_id"],"digest":b["digest"],"enabled":b["enabled"],"selected_model_ids":review["items"].as_array().unwrap().iter().map(|m|m["id"].clone()).collect::<Vec<_>>(),"changes":changes,"unchanged_model_ids":unchanged.iter().map(|m|m["id"].clone()).collect::<Vec<_>>(),"counts":review["counts"],"bindings_changed":false,"capabilities_changed":false});
            emit(c, u, "provider.models.bulk-state", receipt.clone())?;
            (
                200,
                json!({"changed":changed,"unchanged":unchanged,"receipt":receipt,"catalog":catalog(c,id)?}),
            )
        }
        ("GET", ["providers", id, "models", "bulk-state", "history"]) => {
            let mut s=c.prepare("SELECT id,data,created_at FROM events WHERE kind='provider.models.bulk-state' AND json_extract(data,'$.provider_id')=?1 ORDER BY id DESC LIMIT 30")?;
            let rows = s
                .query_map([id], |r| {
                    let mut v = decode(r.get(1)?)?;
                    v["event_id"] = r.get::<_, u64>(0)?.into();
                    v["created_at"] = r.get::<_, String>(2)?.into();
                    Ok(v)
                })?
                .collect::<std::result::Result<Vec<_>, _>>()?;
            (200, json!({"items":rows}))
        }
        ("GET", ["model-hub"]) => {
            let mut models = configs(c, ConfigTable::Model)?;
            for model in &mut models {
                let mid = model["id"].as_str().unwrap();
                let capability=c.query_row("SELECT a.id,a.published_version FROM design_assets a JOIN design_revisions r ON r.asset_id=a.id AND r.version=a.published_version WHERE a.kind='capability' AND r.status='published' AND json_extract(r.config,'$.model_profile_id')=?1 ORDER BY r.published_at DESC LIMIT 1",[mid],|r|Ok((r.get::<_,String>(0)?,r.get::<_,u64>(1)?))).optional()?;
                model["capability"] = capability
                    .map(|(id, v)| design::get(c, &id, Some(v)))
                    .transpose()?
                    .unwrap_or(Value::Null);
                let provider = c
                    .query_row(
                        "SELECT name,enabled,revision_id FROM providers WHERE id=?1",
                        [model["provider_id"].as_str().unwrap_or("")],
                        |r| {
                            Ok((
                                r.get::<_, String>(0)?,
                                r.get::<_, bool>(1)?,
                                r.get::<_, String>(2)?,
                            ))
                        },
                    )
                    .optional()?;
                model["provider_name"] =
                    json!(provider.as_ref().map(|p| p.0.as_str()).unwrap_or(""));
                model["provider_enabled"] = json!(provider.as_ref().is_some_and(|p| p.1));
                if !model["capability"].is_null() {
                    let mut scores = Map::new();
                    for dimension in [
                        "coding",
                        "reasoning",
                        "tool",
                        "latency",
                        "cost",
                        "reliability",
                    ] {
                        scores.insert(
                            dimension.into(),
                            model["capability"]["config"]["overrides"]
                                .get(dimension)
                                .unwrap_or(
                                    &model["capability"]["config"]["system_scores"][dimension],
                                )
                                .clone(),
                        );
                    }
                    let stale = if let Some(bid) =
                        model["capability"]["config"]["benchmark_id"].as_str()
                    {
                        let benchmark = c.query_row("SELECT model_version,provider_revision FROM model_benchmarks WHERE id=?1",[bid],|r|Ok((r.get::<_,u64>(0)?,r.get::<_,String>(1)?))).optional()?;
                        benchmark.is_none_or(|(version, revision)| {
                            model["version"] != version
                                || provider.as_ref().is_none_or(|p| p.2 != revision)
                        })
                    } else {
                        false
                    };
                    model["capability"]["final_scores"] = json!(scores);
                    model["capability"]["stale"] = json!(stale);
                }
            }
            let mut statement=c.prepare("SELECT id,model_id,model_version,provider_revision,status,config,result,actor,created_at FROM model_benchmarks ORDER BY created_at DESC LIMIT 100")?;
            let benchmarks=statement.query_map([],|r|Ok(json!({"id":r.get::<_,String>(0)?,"model_id":r.get::<_,String>(1)?,"model_version":r.get::<_,u64>(2)?,"provider_revision":r.get::<_,String>(3)?,"status":r.get::<_,String>(4)?,"config":decode(r.get(5)?)?,"result":decode(r.get(6)?)?,"actor":r.get::<_,String>(7)?,"created_at":r.get::<_,String>(8)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
            (
                200,
                json!({"models":models,"dimensions":{"coding":"Coding","reasoning":"Reasoning","tool":"Tool round trip","latency":"Latency","cost":"Cost","reliability":"Reliability"},"default_suite":network::benchmark_suite(),"benchmarks":benchmarks}),
            )
        }
        ("GET", ["model-hub", "models", id, "history"]) => {
            let model = get_config(c, ConfigTable::Model, id)?;
            let mut s=c.prepare("SELECT version,config,actor,created_at FROM model_asset_revisions WHERE model_id=?1 ORDER BY version DESC")?;
            let versions=s.query_map([id],|r|Ok(json!({"version":r.get::<_,u64>(0)?,"config":decode(r.get(1)?)?,"actor":r.get::<_,String>(2)?,"created_at":r.get::<_,String>(3)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
            let mut s=c.prepare("SELECT id,model_version,provider_revision,status,config,result,actor,created_at FROM model_benchmarks WHERE model_id=?1 ORDER BY created_at DESC")?;
            let benchmarks=s.query_map([id],|r|Ok(json!({"id":r.get::<_,String>(0)?,"model_id":id,"model_version":r.get::<_,u64>(1)?,"provider_revision":r.get::<_,String>(2)?,"status":r.get::<_,String>(3)?,"config":decode(r.get(4)?)?,"result":decode(r.get(5)?)?,"actor":r.get::<_,String>(6)?,"created_at":r.get::<_,String>(7)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
            (
                200,
                json!({"model_id":id,"model":model,"versions":versions,"revisions":versions,"benchmarks":benchmarks}),
            )
        }
        _ => return Err(missing()),
    };
    Ok(ProductReply::json(status, v))
}
fn provider(c: &Connection, id: &str) -> Result<Value> {
    c.query_row("SELECT name,revision_id,enabled FROM providers WHERE id=?1",[id],|r|Ok(json!({"id":id,"name":r.get::<_,String>(0)?,"revision_id":r.get::<_,String>(1)?,"enabled":r.get::<_,bool>(2)?}))).optional()?.ok_or_else(missing)
}
pub(super) fn catalog(c: &Connection, id: &str) -> Result<Value> {
    let p = provider(c, id)?;
    let key = format!("{id}:{}", p["revision_id"].as_str().unwrap());
    let cache=c.query_row("SELECT entries,last_synced_at,last_attempt_at,error,sync_token FROM provider_model_catalogs WHERE id=?1",[&key],|r|Ok(json!({"entries":decode(r.get(0)?)?,"last_synced_at":r.get::<_,String>(1)?,"last_attempt_at":r.get::<_,String>(2)?,"error":r.get::<_,String>(3)?,"sync_token":r.get::<_,String>(4)?}))).optional()?.unwrap_or(json!({"entries":[],"last_synced_at":"","last_attempt_at":"","error":"","sync_token":""}));
    let mut entries = cache["entries"]
        .as_array()
        .cloned()
        .ok_or_else(|| bad("Corrupt provider catalog"))?;
    let profiles = configs(c, ConfigTable::Model)?
        .into_iter()
        .filter(|m| m["provider_id"] == id)
        .collect::<Vec<_>>();
    let agents = configs(c, ConfigTable::Agent)?;
    let mut retired = Vec::new();
    for m in &profiles {
        let mid = m["model_id"].as_str().unwrap_or("");
        let is_retired = c
            .query_row(
                "SELECT retired FROM provider_catalog_model_states WHERE id=?1 AND provider_id=?2",
                params![m["id"].as_str(), id],
                |r| r.get::<_, bool>(0),
            )
            .optional()?
            .unwrap_or(false);
        if is_retired {
            retired.push(m["id"].clone())
        }
        if !entries.iter().any(|e| e["id"] == mid) {
            entries.push(json!({"id":mid,"label":mid,"available":if cache["last_synced_at"]==""{Value::Null}else{json!(false)},"first_seen_at":"","last_seen_at":"","removed_at":""}))
        }
        let e = entries.iter_mut().find(|e| e["id"] == mid).unwrap();
        if !e["profiles"].is_array() {
            e["profiles"] = json!([])
        }
        e["profiles"].as_array_mut().unwrap().push(json!({"id":m["id"],"name":m["name"],"enabled":m["enabled"],"version":m["version"],"retired":is_retired,"primary_bindings":agents.iter().filter(|a|a["model_profile_id"]==m["id"]).count(),"fallback_bindings":agents.iter().filter(|a|a["fallback_model_ids"].as_array().is_some_and(|v|v.contains(&m["id"]))).count()}));
    }
    entries.sort_by_key(|e| e["id"].as_str().unwrap_or("").to_owned());
    for e in &mut entries {
        if !e["profiles"].is_array() {
            e["profiles"] = json!([])
        }
    }
    let models = entries
        .iter()
        .filter(|e| e["available"] == true)
        .cloned()
        .map(|mut e| {
            e.as_object_mut().unwrap().remove("profiles");
            e
        })
        .collect::<Vec<_>>();
    let sum = if cache["last_synced_at"] == "" {
        String::new()
    } else {
        let source = models
            .iter()
            .map(|m| {
                let mut v = Map::new();
                for k in ["id", "name", "capabilities", "context_window"] {
                    if let Some(x) = m.get(k) {
                        v.insert(k.into(), x.clone());
                    }
                }
                Value::Object(v)
            })
            .collect::<Vec<_>>();
        digest(&json!({"revision_id":p["revision_id"],"models":source}))
    };
    let receipt = c
        .query_row(
            "SELECT data FROM provider_catalog_pull_receipts WHERE id=?1",
            [key],
            |r| decode(r.get(0)?),
        )
        .optional()?;
    Ok(
        json!({"provider_id":id,"revision_id":p["revision_id"],"catalog_digest":sum,"entries":entries,"models":models,"last_pull":receipt,"retired_profile_ids":retired,"last_synced_at":cache["last_synced_at"],"last_attempt_at":cache["last_attempt_at"],"error":cache["error"],"stale":cache["last_synced_at"]=="","ttl_seconds":3600,"sync_status":if cache["last_synced_at"]==""{"never"}else{"ready"},"counts":{"available":models.len(),"configured":profiles.len()-retired.len(),"retired_configured":retired.len(),"unconfigured":entries.iter().filter(|e|e["available"]==true&&e["profiles"].as_array().unwrap().is_empty()).count(),"missing":entries.iter().filter(|e|e["available"]==false).count(),"enabled":profiles.iter().filter(|m|m["enabled"]!=false&&!retired.contains(&m["id"])).count()},"installed":false,"note":""}),
    )
}
fn bulk_review(c: &Connection, id: &str, b: &Value) -> Result<Value> {
    let p = provider(c, id)?;
    if p["revision_id"] != b["revision_id"] || b["enabled"] == true && p["enabled"] != true {
        return Err(conflict());
    }
    let enabled = bool_or(b, "enabled", false)?;
    let selections = b["selections"]
        .as_array()
        .filter(|v| !v.is_empty() && v.len() <= 2000)
        .ok_or_else(|| bad("Invalid model selections"))?;
    let agents = configs(c, ConfigTable::Agent)?;
    let mut seen = BTreeSet::new();
    let mut items = Vec::new();
    for s in selections {
        let mid = string(s, "id", 1, 32)?;
        if !seen.insert(mid) {
            return Err(bad("Duplicate model selection"));
        }
        let mut model = get_config(c, ConfigTable::Model, mid)?;
        if model["provider_id"] != id || model["version"] != s["version"] {
            return Err(conflict());
        }
        let retired = c
            .query_row(
                "SELECT retired FROM provider_catalog_model_states WHERE id=?1 AND provider_id=?2",
                params![mid, id],
                |r| r.get::<_, bool>(0),
            )
            .optional()?
            .unwrap_or(false);
        if enabled && retired {
            return Err(conflict());
        }
        let primary = agents
            .iter()
            .filter(|a| a["model_profile_id"] == mid)
            .map(|a| json!({"id":a["id"],"name":a["name"],"version":a["version"]}))
            .collect::<Vec<_>>();
        let fallback = agents
            .iter()
            .filter(|a| {
                a["fallback_model_ids"]
                    .as_array()
                    .is_some_and(|v| v.contains(&json!(mid)))
            })
            .map(|a| json!({"id":a["id"],"name":a["name"],"version":a["version"]}))
            .collect::<Vec<_>>();
        let version = model["version"].clone();
        model.as_object_mut().unwrap().remove("id");
        model.as_object_mut().unwrap().remove("version");
        if enabled {
            validate_model(c, &model)?;
        }
        items.push(json!({"id":mid,"version":version,"name":model["name"],"model_id":model["model_id"],"enabled":model["enabled"],"target_enabled":enabled,"supports_tools":model["supports_tools"],"retired":retired,"config_digest":digest(&model),"primary":primary,"fallback":fallback}));
    }
    items.sort_by_key(|i| i["id"].as_str().unwrap().to_owned());
    let changed = items.iter().filter(|i| i["enabled"] != enabled).count();
    let mut review = json!({"provider_id":id,"provider_name":p["name"],"revision_id":p["revision_id"],"provider_enabled":p["enabled"],"enabled":enabled,"selections":items.iter().map(|i|json!({"id":i["id"],"version":i["version"]})).collect::<Vec<_>>(),"items":items,"counts":{"selected":items.len(),"changed":changed,"unchanged":items.len()-changed,"primary_bindings":items.iter().map(|i|i["primary"].as_array().unwrap().len()).sum::<usize>(),"fallback_bindings":items.iter().map(|i|i["fallback"].as_array().unwrap().len()).sum::<usize>(),"unconfirmed_tools":items.iter().filter(|i|i["supports_tools"]!=true).count()}});
    let sum = digest(&review);
    review["digest"] = json!(sum);
    Ok(review)
}
