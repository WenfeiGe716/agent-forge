use super::*;
use std::time::Instant;

fn rounded(value: f64) -> f64 {
    (value * 100.).round() / 100.
}

pub(super) fn request(
    api: &ProductApi,
    user: &User,
    path: &str,
    query: &BTreeMap<String, String>,
    body: &Value,
) -> Result<ProductReply> {
    let parts: Vec<_> = path.trim_matches('/').split('/').collect();
    let (operation, model_id, status) = match parts.as_slice() {
        ["api", "model-hub", "models", id, "benchmark"] => ("benchmark", Some(*id), 200),
        ["api", "design", "recommend"] => ("recommend", None, 201),
        _ => return Err(missing()),
    };
    let key = query.get("__idempotency_key").cloned().unwrap_or_else(uid);
    if key.is_empty() || key.len() > 200 {
        return Err(bad("Invalid idempotency key"));
    }
    let ident = digest(&json!([user.id, operation, key]));
    let fingerprint = digest(&json!({"model_id":model_id,"body":body}));
    let mut db = api.store.connect()?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    authorize_transaction(&tx, user)?;
    let prior = tx.query_row("SELECT request_digest,status,status_code,response_body FROM model_hub_invocations WHERE id=?1", [&ident], |r| Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,u16>(2)?,decode(r.get(3)?)?))).optional()?;
    if let Some((expected, state, code, reply)) = prior {
        if expected != fingerprint {
            return Err(conflict());
        }
        if state == "running" {
            return Err(ProductError{status:409, detail:"The model operation is running or its outcome is uncertain after interruption; it will not be replayed".into()});
        }
        return Ok(ProductReply::json(
            if (200..=599).contains(&code) {
                code
            } else {
                409
            },
            reply,
        ));
    }
    tx.execute("INSERT INTO model_hub_invocations(id,actor,operation,request_digest,status,status_code,response_body,created_at,finished_at) VALUES(?1,?2,?3,?4,'running',0,'{}',?5,'')",params![ident,user.id,operation,fingerprint,now(&tx)?])?;
    tx.commit()?;
    let result = if let Some(id) = model_id {
        benchmark(api, user, id, body)
    } else {
        recommend(api, user, body)
    };
    let (state, code, value) = match result {
        Ok(v) => ("completed", status, v),
        Err(error) => ("failed", error.status, json!({"detail":error.detail})),
    };
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    tx.execute("UPDATE model_hub_invocations SET status=?1,status_code=?2,response_body=?3,finished_at=?4 WHERE id=?5 AND status='running'",params![state,code,encode(&value),now(&tx)?,ident])?;
    emit(
        &tx,
        user,
        &format!("model_hub.invocation.{state}"),
        json!({"operation":operation,"invocation_id":ident,"status_code":code}),
    )?;
    tx.commit()?;
    Ok(ProductReply::json(code, value))
}

struct Snapshot {
    model: Value,
    connection: Value,
    revision: String,
    encrypted: String,
}
impl Snapshot {
    fn load(c: &Connection, id: &str) -> Result<Self> {
        let model = get_config(c, ConfigTable::Model, id)?;
        if model["enabled"] == false {
            return Err(bad("Model is disabled"));
        }
        let (revision, connection, encrypted) = c.query_row("SELECT p.revision_id,r.config,r.encrypted_key FROM providers p JOIN provider_revisions r ON r.id=p.revision_id WHERE p.id=?1 AND p.enabled=1",[model["provider_id"].as_str().ok_or_else(missing)?],|r|Ok((r.get(0)?,decode(r.get(1)?)?,r.get(2)?))).optional()?.ok_or_else(||bad("Model provider is disabled or unavailable"))?;
        Ok(Self {
            model,
            connection,
            revision,
            encrypted,
        })
    }
    fn current(&self, c: &Connection, user: &User) -> Result<()> {
        authorize_transaction(c, user)?;
        let current = Self::load(c, self.model["id"].as_str().unwrap())?;
        if current.model != self.model || current.revision != self.revision {
            return Err(conflict());
        }
        Ok(())
    }
    fn call(
        &self,
        api: &ProductApi,
        user: &User,
        messages: &[Value],
        tools: &[Value],
    ) -> Result<Reply> {
        self.current(&api.store.connect()?, user)?;
        complete(
            &self.connection,
            &self.model,
            &api.store.decrypt_secret(&self.encrypted)?,
            messages,
            tools,
        )
    }
}

pub(super) fn suite() -> Value {
    json!({"id":"forge-core-smoke","version":1,"description":"Protocol and bounded question tests only; code answers are compared and never executed.","cases":[
        {"id":"off-by-one","dimension":"coding","prompt":"Python range(n) emits integers from 0 through n-1. For n=4, return ONLY the JSON array of values, no markdown.","expected":"[0,1,2,3]","match":"json"},
        {"id":"boundary","dimension":"coding","prompt":"A function should accept a nonempty string s. Which Python expression rejects both None and the empty string: A: s is None; B: not s; C: len(s)>0. Return only the letter.","expected":"B","match":"exact"},
        {"id":"arithmetic","dimension":"reasoning","prompt":"A shelf has 17 books. 8 are borrowed and 3 are returned. How many remain? Return only the integer.","expected":"12","match":"exact"},
        {"id":"ordering","dimension":"reasoning","prompt":"Task C depends on B, and B depends on A. Return the valid execution order as only a JSON array of task letters.","expected":"[\"A\",\"B\",\"C\"]","match":"json"}
    ]})
}

#[derive(Default)]
struct Measurements {
    requests: usize,
    successes: usize,
    input: u64,
    output: u64,
    latencies: Vec<f64>,
}
impl Measurements {
    fn call(
        &mut self,
        snapshot: &Snapshot,
        api: &ProductApi,
        user: &User,
        messages: &[Value],
        tools: &[Value],
    ) -> Result<Reply> {
        snapshot.current(&api.store.connect()?, user)?;
        self.requests += 1;
        let start = Instant::now();
        let reply = complete(
            &snapshot.connection,
            &snapshot.model,
            &api.store.decrypt_secret(&snapshot.encrypted)?,
            messages,
            tools,
        )?;
        self.successes += 1;
        self.latencies
            .push(rounded(start.elapsed().as_secs_f64() * 1000.));
        self.input += reply.input;
        self.output += reply.output;
        Ok(reply)
    }
}

fn benchmark(api: &ProductApi, user: &User, id: &str, body: &Value) -> Result<Value> {
    fields(body, &["acknowledge", "suite_ref"])?;
    if body["acknowledge"] != true {
        return Err(bad(
            "Explicit acknowledgment is required for paid model evaluation",
        ));
    }
    let mut c = api.store.connect()?;
    let tx = c.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let snapshot = Snapshot::load(&tx, id)?;
    snapshot.current(&tx, user)?;
    let suite = if body["suite_ref"].is_null() {
        suite()
    } else {
        let published = design::resolve(&tx, &body["suite_ref"])?;
        if published["kind"] != "benchmark" {
            return Err(bad("Select a published benchmark suite"));
        }
        design::validate(&tx, "benchmark", &published["config"])?;
        let mut config = published["config"].clone();
        for key in ["id", "version", "digest"] {
            config[key] = published[key].clone();
        }
        config
    };
    let bid = uid();
    let created = now(&tx)?;
    let config = json!({"suite":suite,"model":snapshot.model});
    tx.execute("INSERT INTO model_benchmarks(id,model_id,model_version,provider_revision,status,config,result,actor,created_at) VALUES(?1,?2,?3,?4,'running',?5,'{}',?6,?7)",params![bid,id,snapshot.model["version"].as_u64(),snapshot.revision,encode(&config),user.id,created])?;
    emit(
        &tx,
        user,
        "benchmark.started",
        json!({"benchmark_id":bid,"model_id":id}),
    )?;
    tx.commit()?;
    let mut measured = Measurements::default();
    let mut cases = Vec::new();
    let work = (|| -> Result<()> {
        for case in suite["cases"]
            .as_array()
            .ok_or_else(|| bad("Benchmark cases are unavailable"))?
        {
            let reply = measured.call(
                &snapshot,
                api,
                user,
                &[json!({"role":"user","content":case["prompt"]})],
                &[],
            )?;
            let expected = case["expected"].as_str().unwrap_or("").trim();
            let passed = reply.calls.is_empty()
                && match case["match"].as_str().unwrap_or("exact") {
                    "contains" => reply.text.trim().contains(expected),
                    "json" => serde_json::from_str::<Value>(reply.text.trim())
                        .ok()
                        .zip(serde_json::from_str::<Value>(expected).ok())
                        .is_some_and(|(a, b)| a == b),
                    _ => reply.text.trim() == expected,
                };
            cases.push(json!({"id":case["id"],"dimension":case["dimension"],"passed":passed,"latency_ms":measured.latencies.last()}));
        }
        let nonce = uid();
        let tool = json!({"name":"forge_benchmark_echo","description":"Read-only benchmark; echo the nonce.","parameters":{"type":"object","properties":{"nonce":{"type":"string"}},"required":["nonce"],"additionalProperties":false}});
        let mut messages = vec![
            json!({"role":"user","content":format!("Benchmark only. Call forge_benchmark_echo exactly once with nonce {nonce}. Do not answer in text.")}),
        ];
        let first = measured.call(&snapshot, api, user, &messages, &[tool])?;
        let valid = first.calls.len() == 1
            && first.calls[0]["name"] == "forge_benchmark_echo"
            && first.calls[0]["arguments"] == json!({"nonce":nonce})
            && first.calls[0]["id"].as_str().is_some_and(|s| !s.is_empty());
        cases.push(json!({"id":"tool-call","dimension":"tool","passed":valid,"latency_ms":measured.latencies.last()}));
        let mut roundtrip = false;
        if valid {
            messages.push(json!({"role":"assistant","content":first.text,"calls":first.calls,"native":first.native}));
            messages.push(json!({"role":"tool","call_id":first.calls[0]["id"],"content":json!({"nonce":nonce,"ok":true}).to_string()}));
            messages.push(json!({"role":"user","content":"The tool succeeded. Reply only OK and do not call a tool."}));
            let second = measured.call(&snapshot, api, user, &messages, &[])?;
            roundtrip = second.calls.is_empty() && second.text.trim() == "OK";
        }
        cases.push(json!({"id":"tool-roundtrip","dimension":"tool","passed":roundtrip}));
        Ok(())
    })();
    let tx = c.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let error = work
        .and_then(|_| snapshot.current(&tx, user))
        .err()
        .map(|e| e.detail)
        .unwrap_or_default();
    let mut scores = Map::new();
    for dimension in ["coding", "reasoning", "tool"] {
        let selected: Vec<_> = cases
            .iter()
            .filter(|c| c["dimension"] == dimension)
            .collect();
        scores.insert(
            dimension.into(),
            if selected.is_empty() {
                Value::Null
            } else {
                json!(
                    100. * selected.iter().filter(|c| c["passed"] == true).count() as f64
                        / selected.len() as f64
                )
            },
        );
    }
    scores.insert(
        "reliability".into(),
        if measured.requests == 0 {
            Value::Null
        } else {
            json!(100. * measured.successes as f64 / measured.requests as f64)
        },
    );
    let latency = (!measured.latencies.is_empty())
        .then(|| measured.latencies.iter().sum::<f64>() / measured.latencies.len() as f64);
    scores.insert(
        "latency".into(),
        json!(latency.map(|l| (100. * (1. - l / 30000.)).max(0.))),
    );
    let costs = snapshot.model["input_cost_per_million"]
        .as_f64()
        .zip(snapshot.model["output_cost_per_million"].as_f64());
    scores.insert(
        "cost".into(),
        json!(costs.map(|(i, o)| (100. - (i + o) * 2.5).max(0.))),
    );
    for value in scores.values_mut() {
        if let Some(number) = value.as_f64() {
            *value = json!(rounded(number));
        }
    }
    let result = json!({"scores":scores,"cases":cases,"requests":measured.requests,"successful_requests":measured.successes,"input_tokens":measured.input,"output_tokens":measured.output,"mean_latency_ms":latency.map(rounded),"estimated_cost":costs.map(|(i,o)|(measured.input as f64*i+measured.output as f64*o)/1_000_000.),"error":error,"finished_at":now(&tx)?,"methodology":"Question and tool scores are pass rates; latency score is max(0,100*(1-mean_ms/30000)); cost is max(0,100-2.5*(input+output price per million)); reliability is successful requests / attempts. Measures only this suite; code is never executed."});
    let state = if error.is_empty() {
        "completed"
    } else {
        "failed"
    };
    tx.execute(
        "UPDATE model_benchmarks SET status=?1,result=?2 WHERE id=?3",
        params![state, encode(&result), bid],
    )?;
    let draft = if error.is_empty() {
        design::generated(
            &tx,
            user,
            "capability",
            &format!(
                "{} evaluation",
                snapshot.model["name"]
                    .as_str()
                    .unwrap_or("Model")
                    .chars()
                    .take(100)
                    .collect::<String>()
            ),
            &json!({"model_profile_id":id,"benchmark_id":bid,"system_scores":scores,"overrides":{},"reason":""}),
            &json!({"method":"benchmark","benchmark_id":bid,"model_profile_id":id,"model_version":snapshot.model["version"],"provider_revision":snapshot.revision,"requests":measured.requests}),
            "benchmark",
        )?
    } else {
        Value::Null
    };
    emit(
        &tx,
        user,
        "benchmark.finished",
        json!({"benchmark_id":bid,"model_id":id,"status":state,"requests":measured.requests,"input_tokens":measured.input,"output_tokens":measured.output}),
    )?;
    tx.commit()?;
    Ok(
        json!({"id":bid,"model_id":id,"model_version":snapshot.model["version"],"provider_revision":snapshot.revision,"status":state,"config":config,"result":result,"actor":user.id,"created_at":created,"draft":draft}),
    )
}

fn candidates(c: &Connection, body: &Value) -> Result<Vec<Value>> {
    let allowed = strings(body, "allowed_provider_ids", 100)?;
    let mut out = Vec::new();
    for model in configs(c, ConfigTable::Model)? {
        if model["enabled"] == false || model["supports_tools"] != true {
            continue;
        }
        let provider=c.query_row("SELECT p.revision_id,r.config FROM providers p JOIN provider_revisions r ON r.id=p.revision_id WHERE p.id=?1 AND p.enabled=1",[model["provider_id"].as_str().unwrap_or("")],|r|Ok((r.get::<_,String>(0)?,decode(r.get(1)?)?))).optional()?;
        let Some((revision, connection)) = provider else {
            continue;
        };
        if body["allow_external"] != true && connection["external"] != false
            || !allowed.is_empty() && !allowed.iter().any(|p| model["provider_id"] == *p)
        {
            continue;
        }
        let profile=c.query_row("SELECT a.id,a.published_version FROM design_assets a JOIN design_revisions r ON r.asset_id=a.id AND r.version=a.published_version WHERE a.kind='capability' AND r.status='published' AND json_extract(r.config,'$.model_profile_id')=?1 ORDER BY r.published_at DESC,a.id DESC LIMIT 1",[model["id"].as_str().unwrap()],|r|Ok((r.get::<_,String>(0)?,r.get::<_,u64>(1)?))).optional()?.map(|(id,v)|design::get(c,&id,Some(v))).transpose()?;
        let mut scores = json!({});
        let mut reference = Value::Null;
        if let Some(p) = profile {
            reference = json!({"id":p["id"],"version":p["version"],"digest":p["digest"]});
            if design::validate(c, "capability", &p["config"]).is_ok() {
                scores = p["config"]
                    .get("system_scores")
                    .cloned()
                    .unwrap_or(json!({}));
                for (k, v) in object(&p["config"]["overrides"])? {
                    scores[k] = v.clone();
                }
            }
        }
        out.push(json!({"id":model["id"],"name":model["name"],"provider_id":model["provider_id"],"model_version":model["version"],"provider_revision":revision,"scores":scores,"profile_ref":reference}));
    }
    out.sort_by(|a, b| {
        (a["name"].as_str(), a["id"].as_str()).cmp(&(b["name"].as_str(), b["id"].as_str()))
    });
    Ok(out)
}

fn recommend(api: &ProductApi, user: &User, body: &Value) -> Result<Value> {
    fields(
        body,
        &[
            "kind",
            "request",
            "model_profile_id",
            "budget",
            "acknowledge",
            "allow_external",
            "allowed_provider_ids",
        ],
    )?;
    let body = defaults(
        body,
        json!({"budget":"medium","acknowledge":false,"allow_external":false,"allowed_provider_ids":[],"model_profile_id":null}),
    )?;
    let kind = string(&body, "kind", 1, 20)?;
    if !["routing", "team"].contains(&kind) {
        return Err(bad("Recommendations require a routing or team kind"));
    }
    let prompt = string(&body, "request", 3, 12000)?;
    let budget = string(&body, "budget", 1, 10)?;
    if !["low", "medium", "high"].contains(&budget) {
        return Err(bad("Invalid recommendation budget"));
    }
    bool_or(&body, "allow_external", false)?;
    bool_or(&body, "acknowledge", false)?;
    if !body["model_profile_id"].is_null() {
        string(&body, "model_profile_id", 1, 32)?;
    }
    let mut c = api.store.connect()?;
    let choices = candidates(&c, &body)?;
    if choices.is_empty() {
        return Err(bad(
            "No enabled tool-capable model satisfies the provider and external-access rules",
        ));
    }
    let mut assignments = Map::new();
    let mut reasons = Vec::new();
    for (role, weights) in [
        ("planner", vec![("reasoning", 0.7), ("tool", 0.3)]),
        ("developer", vec![("coding", 0.7), ("tool", 0.3)]),
        (
            "tester",
            vec![("coding", 0.4), ("reasoning", 0.3), ("tool", 0.3)],
        ),
        ("reviewer", vec![("reasoning", 0.5), ("coding", 0.5)]),
    ] {
        let score = |candidate: &Value| {
            let mut sum = 0.;
            let mut weight = 0.;
            for (key, w) in &weights {
                if let Some(v) = candidate["scores"][key].as_f64() {
                    sum += v * w;
                    weight += w;
                }
            }
            if weight == 0. {
                return -1.;
            }
            let quality = sum / weight;
            match candidate["scores"]["cost"].as_f64() {
                Some(cost) if budget != "high" => quality * 0.7 + cost * 0.3,
                _ => quality,
            }
        };
        let mut choice = &choices[0];
        for candidate in &choices[1..] {
            if score(candidate) > score(choice) {
                choice = candidate;
            }
        }
        assignments.insert(role.into(), choice["id"].clone());
        reasons.push(json!({"role":role,"model_id":choice["id"],"reason":if score(choice)<0.{"No published measurements; stable catalog order is an unreviewed suggestion"}else{"Weighted ranking of published capability evidence"},"capability_ref":choice["profile_ref"]}));
    }
    let rules = json!({"allowed_provider_ids":body["allowed_provider_ids"],"allow_external":body["allow_external"],"require_tools":true,"max_model_calls":match budget{"low"=>30,"high"=>300,_=>100}});
    let mut config = if kind == "routing" {
        json!({"task_type":prompt.chars().take(120).collect::<String>(),"assignments":assignments,"budget":budget,"max_parallel":3,"rules":rules})
    } else {
        let members = [("planner","Atlas","Architecture","Analyze requirements and define acceptance evidence."),("developer","Nova","Development","Implement and provide verifiable code evidence."),("tester","Echo","Quality","Run regression checks and record failures."),("reviewer","Sage","Review","Independently review implementation, risks, and acceptance evidence.")].iter().enumerate().map(|(i,(role,name,department,instructions))|json!({"key":role,"name":name,"role":role,"model_profile_id":assignments[*role],"fallback_model_ids":[],"instructions":instructions,"department":department,"skills":[],"communication":{"scope":"run","recipients":[]},"reports_to":if i==0{Value::Null}else{json!("planner")},"tool_permissions":role_tools(role),"office_position":{"x":-7+i as i32*4,"z":-4,"floor":1,"zone":department}})).collect::<Vec<_>>();
        json!({"members":members})
    };
    let suggestions=skills::request(&c,user,"GET",&["skills"],&BTreeMap::new(),&json!({}),&[])?.body.as_array().unwrap().iter().filter(|s|s["enabled"]==true).take(20).map(|s|json!({"skill_id":s["id"],"name":s["name"],"version":s["latest_version"],"status":s["status"]})).collect::<Vec<_>>();
    let mut generation = json!({"method":"rules","reason":"Local rules using published evidence and explicit provider constraints","reasons":reasons,"skill_suggestions":suggestions,"requests":0});
    let snapshot = if let Some(id) = body["model_profile_id"].as_str() {
        if body["acknowledge"] != true {
            return Err(bad("Explicit acknowledgment is required to send requirements and model catalog to a paid design assistant"));
        }
        let snapshot = Snapshot::load(&c, id)?;
        if snapshot.connection["external"] != false && body["allow_external"] != true {
            return Err(bad(
                "External design assistance requires explicit external access",
            ));
        }
        let schema = proposal_schema(kind, &choices);
        let tools = if snapshot.model["supports_tools"] == true {
            vec![
                json!({"name":"propose_design","description":"Submit one editable, unapproved design proposal. This grants no permissions and executes nothing.","parameters":schema}),
            ]
        } else {
            Vec::new()
        };
        let reply=snapshot.call(api,user,&[json!({"role":"system","content":"Assist a human organization designer. Return one editable configuration for review; never publish or execute anything."}),json!({"role":"user","content":json!({"request":prompt,"kind":kind,"editable_template":config,"candidate_models":choices,"proposal_schema":schema,"instructions":"Call propose_design exactly once, or return only configuration JSON when tool calls are unavailable. Use candidate model IDs and existing roles. Do not add permissions, skills, runtime overrides, or unmeasured capability claims."}).to_string()})],&tools)?;
        let mut proposal = parse_proposal(&reply)?;
        if kind == "routing" {
            proposal["rules"] = rules.clone();
        }
        restrict_proposal(kind, &proposal, &choices).and_then(|_| design::validate(&c,kind,&proposal)).map_err(|_| ProductError { status:502, detail:"Design assistant returned an invalid configuration or exceeded the authorized model and permission scope; no draft was created".into() })?;
        config = proposal;
        generation["method"] = json!("ai");
        generation["reason"] = json!("Unapproved proposal generated by the selected model");
        generation["requests"] = json!(1);
        generation["rules_reasons"] = generation["reasons"].clone();
        let selected: Vec<_> = if kind == "routing" {
            object(&config["assignments"])?
                .iter()
                .map(|(role, id)| (role.as_str(), id))
                .collect()
        } else {
            config["members"]
                .as_array()
                .unwrap()
                .iter()
                .map(|member| {
                    (
                        member["role"].as_str().unwrap_or(""),
                        &member["model_profile_id"],
                    )
                })
                .collect()
        };
        generation["reasons"] = json!(selected.iter().map(|(role,id)|json!({"role":role,"model_id":id,"reason":"Model assignment proposed by AI; review it against the design and measured capability evidence"})).collect::<Vec<_>>());
        generation["model_profile_id"] = body["model_profile_id"].clone();
        generation["model_version"] = snapshot.model["version"].clone();
        generation["provider_revision"] = json!(snapshot.revision);
        generation["input_tokens"] = json!(reply.input);
        generation["output_tokens"] = json!(reply.output);
        Some(snapshot)
    } else {
        None
    };
    let tx = c.transaction_with_behavior(TransactionBehavior::Immediate)?;
    authorize_transaction(&tx, user)?;
    if let Some(snapshot) = &snapshot {
        snapshot.current(&tx, user)?;
    }
    if candidates(&tx, &body)? != choices {
        return Err(conflict());
    }
    let draft = design::generated(
        &tx,
        user,
        kind,
        &format!(
            "{} {} proposal",
            prompt.chars().take(70).collect::<String>(),
            kind
        ),
        &config,
        &generation,
        if snapshot.is_some() { "ai" } else { "manual" },
    )?;
    emit(
        &tx,
        user,
        "design.recommended",
        json!({"asset_id":draft["id"],"generation":generation}),
    )?;
    tx.commit()?;
    Ok(draft)
}

fn proposal_schema(kind: &str, choices: &[Value]) -> Value {
    let model =
        json!({"type":"string","enum":choices.iter().map(|c|c["id"].clone()).collect::<Vec<_>>()});
    if kind == "routing" {
        json!({"type":"object","properties":{"task_type":{"type":"string"},"assignments":{"type":"object","properties":{"planner":model,"developer":model,"tester":model,"reviewer":model},"additionalProperties":false},"budget":{"type":"string","enum":["low","medium","high"]},"max_parallel":{"type":"integer","minimum":1,"maximum":32},"description":{"type":"string"}},"required":["task_type","assignments"],"additionalProperties":false})
    } else {
        json!({"type":"object","properties":{"members":{"type":"array","minItems":1,"maxItems":30,"items":{"type":"object","properties":{"key":{"type":"string"},"name":{"type":"string"},"role":{"type":"string","enum":["planner","developer","tester","reviewer"]},"model_profile_id":model,"instructions":{"type":"string"},"department":{"type":"string"},"reports_to":{"type":"string"}},"required":["key","name","role","model_profile_id","instructions"],"additionalProperties":false}},"description":{"type":"string"}},"required":["members"],"additionalProperties":false})
    }
}
fn parse_proposal(reply: &Reply) -> Result<Value> {
    let value = if !reply.calls.is_empty() {
        if reply.calls.len() != 1 || reply.calls[0]["name"] != "propose_design" {
            return Err(upstream());
        }
        reply.calls[0]["arguments"].clone()
    } else {
        let raw = reply.text.trim();
        let raw = raw
            .strip_prefix("```json\n")
            .or_else(|| raw.strip_prefix("```\n"))
            .and_then(|s| s.strip_suffix("\n```"))
            .unwrap_or(raw);
        serde_json::from_str(raw).map_err(|_| upstream())?
    };
    object(&value).map_err(|_| upstream())?;
    Ok(value)
}
fn restrict_proposal(kind: &str, proposal: &Value, choices: &[Value]) -> Result<()> {
    let permitted = |id: &Value| choices.iter().any(|c| c["id"] == *id);
    if kind == "routing" {
        fields(
            proposal,
            &[
                "task_type",
                "assignments",
                "budget",
                "max_parallel",
                "description",
                "rules",
            ],
        )?;
        if object(&proposal["assignments"])?
            .values()
            .any(|id| !permitted(id))
        {
            return Err(bad(
                "Design assistant selected a model outside the authorized catalog",
            ));
        }
    } else {
        fields(proposal, &["members", "description"])?;
        for member in proposal["members"].as_array().ok_or_else(upstream)? {
            fields(
                member,
                &[
                    "key",
                    "name",
                    "role",
                    "model_profile_id",
                    "instructions",
                    "department",
                    "reports_to",
                    "office_position",
                ],
            )?;
            if let Some(position) = member.get("office_position").filter(|v| !v.is_null()) {
                fields(position, &["x", "z", "floor", "zone"])?;
                number(position, "x", -12., 12.)?;
                number(position, "z", -8., 8.)?;
                if position["floor"]
                    .as_u64()
                    .is_none_or(|n| !(1..=10).contains(&n))
                {
                    return Err(bad("Invalid proposed office floor"));
                }
                if position.get("zone").is_some() {
                    string(position, "zone", 0, 80)?;
                }
            }
            if !permitted(&member["model_profile_id"]) {
                return Err(bad(
                    "Design assistant selected a model outside the authorized catalog",
                ));
            }
        }
    }
    Ok(())
}
