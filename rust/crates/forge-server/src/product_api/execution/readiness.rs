use super::*;

fn check(code: &str, ok: bool, message: &str, target: &str) -> Value {
    json!({"code":code,"status":if ok{"ok"}else{"error"},"message":message,"target":target})
}
fn ready(checks: &[Value]) -> bool {
    checks.iter().all(|c| c["status"] != "error")
}

impl ProductApi {
    pub(super) fn readiness(
        &self,
        c: &Connection,
        u: &User,
        q: &BTreeMap<String, String>,
        input: Option<&Value>,
    ) -> Result<Value> {
        if let Some(input) = input {
            validate_input(input)?;
        }
        let mode = if input.is_some() {
            "autonomous"
        } else {
            q.get("mode").map(String::as_str).unwrap_or("autonomous")
        };
        if !["autonomous", "development", "discussion"].contains(&mode) {
            return Err(bad("Invalid execution mode"));
        }
        let project_id = input
            .and_then(|v| v["project_id"].as_str())
            .or_else(|| q.get("project_id").map(String::as_str))
            .filter(|s| !s.is_empty());
        let project = project_id.map(|id| project(c, u, id)).transpose()?;
        if input.is_some() {
            if let Some(id) = project_id {
                self.store.authorize_project(u, id, true)?;
            }
        }
        let project_checks = if let Some(input) = input {
            input
                .get("run_project_checks")
                .map(|v| {
                    v.as_bool()
                        .ok_or_else(|| bad("run_project_checks must be boolean"))
                })
                .transpose()?
                .unwrap_or(false)
        } else {
            q.get("run_project_checks")
                .map(|v| match v.as_str() {
                    "true" | "1" => Ok(true),
                    "false" | "0" => Ok(false),
                    _ => Err(bad("run_project_checks must be boolean")),
                })
                .transpose()?
                .unwrap_or(false)
        };
        let service = self.runtime.as_ref().and_then(|r| r.service.upgrade());
        let available = service.as_ref().is_some_and(|s| s.enabled());
        let mut checks = vec![
            check(
                "runner.available",
                available,
                if available {
                    "Rust Codex host is configured"
                } else {
                    "Rust Codex executable is unavailable"
                },
                "runner",
            ),
            check(
                "project.selected",
                project.is_some(),
                if project.is_some() {
                    "Project selected"
                } else {
                    "Select a project before starting a run"
                },
                "projects",
            ),
        ];
        let execution_mode = project
            .as_ref()
            .and_then(|p| p["execution_mode"].as_str())
            .unwrap_or("container");
        let permission = project
            .as_ref()
            .and_then(|p| p["permission_profile"].as_str())
            .unwrap_or("ask");
        if let Some(p) = &project {
            checks.push(check(
                "project.active",
                p["archived"] != true,
                "Project must be active",
                "projects",
            ));
        }
        checks.push(check(
            "execution.backend",
            execution_mode == "local",
            if execution_mode == "local" {
                "Commands use the Codex process sandbox"
            } else {
                "Rust runtime does not support the container execution backend"
            },
            "execution",
        ));
        checks.push(check(
            "execution.strategy",
            mode == "autonomous",
            "The current Rust host implements autonomous Codex runs",
            "execution",
        ));
        checks.push(check(
            "execution.project_checks",
            !project_checks,
            "Automatic project checks are not implemented by the current Rust host",
            "execution",
        ));
        let mut policy_query = BTreeMap::new();
        if let Some(pid) = project_id {
            policy_query.insert("project_id".into(), pid.into());
        }
        let resolved = policies::request(
            c,
            u,
            "GET",
            &["execution", "policies"],
            &policy_query,
            &json!({}),
        )?
        .body;
        let all_agents = configs(c, ConfigTable::Agent)?;
        let chosen = if let Some(input) = input {
            let ids = input["agent_ids"]
                .as_array()
                .ok_or_else(|| bad("agent_ids must be an array"))?;
            if ids.iter().any(|id| id.as_str().is_none()) {
                return Err(bad("Invalid agent identifier"));
            }
            ids.clone()
        } else {
            all_agents
                .iter()
                .filter(|a| a["enabled"] != false)
                .map(|a| a["id"].clone())
                .collect()
        };
        checks.push(check(
            "agents.selected",
            !chosen.is_empty(),
            "At least one enabled member is required",
            "agents",
        ));
        let mut agents = Vec::new();
        for id in &chosen {
            let Some(agent) = all_agents.iter().find(|a| a["id"] == *id) else {
                checks.push(check(
                    "agent.exists",
                    false,
                    "Selected member does not exist",
                    "agents",
                ));
                continue;
            };
            let mut member = vec![check(
                "agent.enabled",
                agent["enabled"] != false,
                "Member must be enabled",
                "agents",
            )];
            let mid = agent["model_profile_id"].as_str().unwrap_or("");
            member.push(check(
                "model.bound",
                !mid.is_empty(),
                "Member requires a primary model profile",
                "agents",
            ));
            if !mid.is_empty() {
                match get_config(c, ConfigTable::Model, mid) {
                    Ok(model) => {
                        member.push(check(
                            "model.enabled",
                            model["enabled"] != false,
                            "Model profile must be enabled",
                            "providers",
                        ));
                        member.push(json!({
                            "code": "model.tools",
                            "status": if model["supports_tools"] == true { "ok" } else { "warning" },
                            "message": if model["supports_tools"] == true {
                                "Tool capability is recorded; tools are still executed by Forge"
                            } else {
                                "Tool capability has not been probed; the real run will validate the provider request"
                            },
                            "target": "providers",
                        }));
                        let pid = model["provider_id"].as_str().unwrap_or("");
                        let provider:Option<(String,Value)>=c.query_row("SELECT r.id,r.config FROM providers p JOIN provider_revisions r ON r.id=p.revision_id AND r.provider_id=p.id WHERE p.id=?1 AND p.enabled=1",[pid],|r|Ok((r.get(0)?,decode(r.get(1)?)?))).optional()?;
                        member.push(check(
                            "provider.enabled",
                            provider.is_some(),
                            "An enabled current provider revision is required",
                            "providers",
                        ));
                        if let Some((revision, provider)) = provider {
                            let mut params = model["parameters"].clone();
                            if params.is_null() {
                                params = json!({});
                            }
                            if let Some(overrides) = agent["model_parameters"].as_object() {
                                object(&params)?;
                                params.as_object_mut().unwrap().extend(overrides.clone());
                            }
                            let valid = protocols::parameters(
                                provider["protocol"].as_str().unwrap_or(""),
                                &model["allowed_parameters"],
                                &params,
                                &provider["protocol_config"],
                            );
                            member.push(check(
                                "model.parameters",
                                valid.is_ok(),
                                &valid
                                    .err()
                                    .map(|e| e.detail)
                                    .unwrap_or_else(|| "Model parameters are valid".into()),
                                "providers",
                            ));
                            if provider["external"] == true {
                                member.push(check("project.external_models",project.as_ref().is_some_and(|p|p["allow_external_models"]==true),"External providers require the project's explicit data-transfer permission","projects"));
                            }
                            let probe = config(c, &format!("probe:{mid}"), json!({}))?;
                            if probe["model_version"] != model["version"]
                                || probe["revision_id"] != revision
                            {
                                member.push(json!({"code":"model.live_probe","status":"warning","message":"No current live tool-roundtrip evidence is recorded","target":"providers"}));
                            }
                        }
                    }
                    Err(error) if error.status == 404 => member.push(check(
                        "model.exists",
                        false,
                        "Model profile does not exist",
                        "providers",
                    )),
                    Err(error) => return Err(error),
                }
            }
            let policy = resolved["agents"]
                .as_array()
                .and_then(|a| a.iter().find(|a| a["id"] == *id))
                .cloned()
                .unwrap_or(json!({}));
            if let (Some(service), Some(pid)) = (&service, project_id) {
                let sample = json!({"project_id":pid,"title":"Execution readiness","request":"Validate frozen runtime configuration","agent_ids":[id]});
                let result = if u.role == "viewer" {
                    Err(BackendError::Forbidden)
                } else {
                    service.preview(u, sample)
                };
                let (valid, message) = match result {
                    Ok(_) => (true, "Frozen runtime configuration is supported".into()),
                    Err(e) => (false, e.to_string()),
                };
                member.push(check("agent.runtime", valid, &message, "execution"));
            }
            for check in &mut member {
                check["agent_id"] = id.clone();
                check["model_profile_id"] = json!(mid);
            }
            agents.push(json!({"id":id,"name":agent["name"],"role":agent["role"],"job_title":agent["job_title"],"department":agent["department"],"ready":ready(&member),"model_profile_id":mid,"tools":agent["tool_permissions"].as_array().cloned().unwrap_or_default(),"execution_policy":policy,"checks":member,"workstation":{}}));
            checks.extend(member);
        }
        let mut snapshot_valid = None;
        if let Some(input) = input {
            let result = service
                .as_ref()
                .ok_or(BackendError::Unavailable)
                .and_then(|s| s.preview(u, input.clone()));
            let (valid, message) = match result {
                Ok(_) => (true, "Run snapshot validated without creating a run".into()),
                Err(e) => (false, e.to_string()),
            };
            checks.push(check("run.snapshot", valid, &message, "runs"));
            snapshot_valid = Some(valid);
        }
        checks.push(json!({"code":"model.inference_not_executed","status":"warning","message":"Readiness checks do not invoke models or execute project commands","target":"providers"}));
        let ready = ready(&checks);
        let mut result = json!({"ready":ready,"checked_at":now(c)?,"inference_tested":false,"checks":checks,"agents":agents,"projects":project.iter().collect::<Vec<_>>(),"project_id":project_id,"runner":{"ready":available,"orchestrator":"rust","status":if available{"available"}else{"unavailable"}},"sandbox":{"ready":false,"state":"unsupported","runtime":"codex","detail":"Container execution is not implemented by this Rust host"},"execution":{"mode":execution_mode,"permission_profile":permission,"ready":ready,"runtime":"codex-app-server","network":if permission=="full"{"unrestricted"}else{"approval_policy"},"workspace_scope":"run_working_copy","workspace_path_pattern":self.data_root.join("runs/{run_id}/workspace"),"workspace_created":false,"member_policies":resolved["agents"],"environments":{},"limits":{},"detail":"Execution uses the actual immutable Codex runtime configuration"},"requires_sandbox":execution_mode=="container","requires_command_environment":mode!="discussion"||project_checks,"execution_strategy":if mode=="autonomous"{"autonomous"}else{"workflow"},"run_project_checks":project_checks});
        if let Some(valid) = snapshot_valid {
            result["snapshot_valid"] = json!(valid);
            result["run_created"] = json!(false);
        }
        Ok(result)
    }
}

fn validate_input(v: &Value) -> Result<()> {
    fields(
        v,
        &[
            "execution_strategy",
            "run_project_checks",
            "routing_ref",
            "team_ref",
            "office_team_id",
            "office_layout_ref",
            "workflow_ref",
            "routing_overrides",
            "entry_agent_id",
            "collaboration_flow",
            "orchestration",
            "template_id",
            "template_version",
            "project_id",
            "title",
            "request",
            "agent_ids",
            "runtime",
            "parent_run_id",
            "attachments",
        ],
    )?;
    string(v, "project_id", 1, 256)?;
    string(v, "title", 1, 180)?;
    string(v, "request", 3, 30000)?;
    let agents = v["agent_ids"]
        .as_array()
        .filter(|v| !v.is_empty() && v.len() <= 30)
        .ok_or_else(|| bad("Select 1 to 30 members"))?;
    let mut seen = BTreeSet::new();
    for id in agents {
        let id = id
            .as_str()
            .ok_or_else(|| bad("Invalid member identifier"))?;
        if !seen.insert(id) {
            return Err(bad("Duplicate member identifier"));
        }
    }
    if v.get("execution_strategy")
        .is_some_and(|v| v != "autonomous")
    {
        return Err(bad("Invalid execution strategy"));
    }
    for key in ["run_project_checks", "collaboration_flow"] {
        if v.get(key).is_some_and(|v| !v.is_boolean()) {
            return Err(bad(format!("{key} must be boolean")));
        }
    }
    for key in ["entry_agent_id", "template_id", "parent_run_id"] {
        if v.get(key).is_some_and(|v| !v.is_null() && !v.is_string()) {
            return Err(bad(format!("{key} must be a string")));
        }
    }
    for key in [
        "routing_ref",
        "team_ref",
        "workflow_ref",
        "office_layout_ref",
    ] {
        if let Some(value) = v.get(key).filter(|v| !v.is_null()) {
            fields(
                value,
                if key == "office_layout_ref" {
                    &["id", "version", "digest"]
                } else {
                    &["id", "version"]
                },
            )?;
            string(value, "id", 1, 32)?;
            if value["version"].as_u64().is_none_or(|n| n < 1) {
                return Err(bad("Invalid reference version"));
            }
            if key == "office_layout_ref" {
                hex(value, "digest")?;
            }
        }
    }
    if let Some(runtime) = v.get("runtime") {
        validate_runtime(runtime, false)?;
    }
    if let Some(attachments) = v.get("attachments") {
        let list = attachments
            .as_array()
            .ok_or_else(|| bad("attachments must be an array"))?;
        if list.len() > 8 {
            return Err(bad("At most 8 attachments are allowed"));
        }
        let mut total = 0usize;
        for item in list {
            fields(item, &["filename", "content_base64", "media_type"])?;
            let filename = string(item, "filename", 1, 240)?;
            if filename == "." || filename == ".." || filename.contains(['/', '\\']) {
                return Err(bad("Invalid attachment filename"));
            }
            let encoded = string(item, "content_base64", 1, 12_000_000)?;
            let bytes = base64::engine::general_purpose::STANDARD
                .decode(encoded)
                .map_err(|_| bad("Invalid attachment encoding"))?;
            if bytes.len() > 8 * 1024 * 1024 {
                return Err(bad("Attachment exceeds the 8 MB per-file limit"));
            }
            total = total.saturating_add(bytes.len());
            if total > 20 * 1024 * 1024 {
                return Err(bad("Attachments exceed the 20 MB total limit"));
            }
            if item.get("media_type").is_some_and(|v| !v.is_string()) {
                return Err(bad("media_type must be a string"));
            }
        }
    }
    if let Some(overrides) = v.get("routing_overrides") {
        for (role, id) in object(overrides)? {
            if !["planner", "developer", "tester", "reviewer"].contains(&role.as_str())
                || !id.is_string()
            {
                return Err(bad("Invalid routing override"));
            }
        }
        if !object(overrides)?.is_empty() && v["routing_ref"].is_null() {
            return Err(bad(
                "Routing overrides require a published routing reference",
            ));
        }
    }
    if let Some(team) = v.get("office_team_id").filter(|v| !v.is_null()) {
        team.as_str()
            .filter(|s| {
                !s.is_empty()
                    && s.len() <= 64
                    && s.as_bytes()[0].is_ascii_alphabetic()
                    && s.bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
            })
            .ok_or_else(|| bad("Invalid office team identifier"))?;
        if !v["team_ref"].is_null() {
            return Err(bad(
                "Office team and published team template cannot be combined",
            ));
        }
    }
    if v["office_team_id"].is_null() != v["office_layout_ref"].is_null() {
        return Err(bad(
            "Office team and layout revision must be supplied together",
        ));
    }
    if !v["orchestration"].is_null()
        || !v["workflow_ref"].is_null()
        || v["template_id"].as_str().is_some_and(|s| !s.is_empty())
    {
        return Err(bad("Autonomous runs cannot use fixed orchestration"));
    }
    if !v["template_version"].is_null() {
        return Err(bad("Template version requires a template"));
    }
    Ok(())
}
