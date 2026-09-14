//! Explicit provider requests run outside product transactions. Persisted
//! revisions and request reservations fence results before applying them.
use super::*;
mod model_ops;

pub(super) fn benchmark_suite() -> Value {
    model_ops::suite()
}
use reqwest::{
    blocking::{Client, Response},
    header::{HeaderMap, HeaderName, HeaderValue},
};
use std::{
    io::Read,
    net::{IpAddr, ToSocketAddrs},
    time::Duration,
};

fn upstream() -> ProductError {
    ProductError {
        status: 502,
        detail: "Provider request failed or returned an invalid response".into(),
    }
}

impl ProductApi {
    pub(super) fn network_request(
        &self,
        user: &User,
        method: &str,
        path: &str,
        query: &BTreeMap<String, String>,
        body: &[u8],
    ) -> Option<Result<ProductReply>> {
        handles(method, path).then(|| request(self, user, method, path, query, body))
    }
}

pub(super) fn handles(method: &str, path: &str) -> bool {
    method == "POST"
        && (matches!(path, "/api/setup/models" | "/api/setup/connect")
            || path == "/api/design/recommend"
            || path.starts_with("/api/model-hub/models/") && path.ends_with("/benchmark")
            || path.ends_with("/catalog/sync")
            || path.starts_with("/api/model-hub/providers/") && path.ends_with("/discover")
            || path.starts_with("/api/skill-store/sources/") && path.ends_with("/sync")
            || path.starts_with("/api/models/")
                && (path.ends_with("/test") || path.ends_with("/tool-test")))
}

pub(super) fn request(
    api: &ProductApi,
    user: &User,
    _method: &str,
    path: &str,
    query: &BTreeMap<String, String>,
    raw: &[u8],
) -> Result<ProductReply> {
    let user = authorize(api, user)?;
    let body: Value = if raw.is_empty() {
        json!({})
    } else {
        serde_json::from_slice(raw).map_err(|_| bad("Invalid JSON body"))?
    };
    let parts: Vec<_> = path.trim_matches('/').split('/').collect();
    if path == "/api/design/recommend"
        || path.starts_with("/api/model-hub/models/") && path.ends_with("/benchmark")
    {
        return model_ops::request(api, &user, path, query, &body);
    }
    let value = match parts.as_slice() {
        ["api", "setup", "models"] => {
            let (connection, key) = input_connection(&body)?;
            let rows = discover(&connection, &key)?;
            json!({"models":rows,"has_more":false,"note":""})
        }
        ["api", "setup", "connect"] => setup_connect(api, &user, &body, query)?,
        ["api", "providers", id, "catalog", "sync"]
        | ["api", "model-hub", "providers", id, "discover"] => sync_catalog(api, &user, id)?,
        ["api", "models", id, kind @ ("test" | "tool-test")] => {
            model_test(api, &user, id, *kind == "tool-test")?
        }
        ["api", "skill-store", "sources", id, "sync"] => sync_skills(api, &user, id)?,
        _ => return Err(missing()),
    };
    Ok(ProductReply::json(200, value))
}

fn sync_skills(api: &ProductApi, user: &User, id: &str) -> Result<Value> {
    let mut db = api.store.connect()?;
    let source = super::skill_store::source(&db, id)?;
    if source["enabled"] != true || source["kind"] != "github" {
        return Err(bad("An enabled GitHub source is required"));
    }
    let repository = url::Url::parse(source["repository"].as_str().ok_or_else(upstream)?)
        .map_err(|_| upstream())?;
    if repository.scheme() != "https"
        || repository.host_str() != Some("github.com")
        || !repository.username().is_empty()
        || repository.password().is_some()
        || repository.query().is_some()
        || repository.fragment().is_some()
    {
        return Err(bad("Invalid GitHub source"));
    }
    let repository = repository.path().trim_matches('/').trim_end_matches(".git");
    if repository.split('/').count() != 2
        || !repository
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"/_.-".contains(&b))
    {
        return Err(bad("Invalid GitHub source"));
    }
    let reference = source["ref"].as_str().ok_or_else(upstream)?;
    let client = Client::builder()
        .timeout(Duration::from_secs(30))
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .user_agent("Agent-Forge-Rust")
        .build()
        .map_err(|_| upstream())?;
    let mut commit_url = url::Url::parse(&format!(
        "https://api.github.com/repos/{repository}/commits/"
    ))
    .map_err(|_| upstream())?;
    commit_url
        .path_segments_mut()
        .map_err(|_| upstream())?
        .push(reference);
    let commit = response(
        client
            .get(commit_url)
            .header("accept", "application/vnd.github+json")
            .send()
            .map_err(|_| upstream())?,
    )?;
    let sha = commit["sha"]
        .as_str()
        .filter(|s| s.len() == 40 && s.bytes().all(|c| c.is_ascii_hexdigit()))
        .ok_or_else(upstream)?;
    let response = client
        .get(format!(
            "https://codeload.github.com/{repository}/zip/{sha}"
        ))
        .send()
        .map_err(|_| upstream())?;
    if !response.status().is_success() {
        return Err(upstream());
    }
    let mut bytes = Vec::new();
    response
        .take(32_000_001)
        .read_to_end(&mut bytes)
        .map_err(|_| upstream())?;
    if bytes.len() > 32_000_000 {
        return Err(upstream());
    }
    let files = super::skill_store::archive_files(&bytes)?;
    let origin = json!({"repository":source["repository"],"ref":reference,"commit":sha,"verification":"github_commit","archive_sha256":format!("{:x}",Sha256::digest(&bytes))});
    authorize(api, user)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let current = super::skill_store::source(&tx, id)?;
    authorize_transaction(&tx, user)?;
    if current["version"] != source["version"] || current["enabled"] != true {
        return Err(conflict());
    }
    let result = super::skill_store::apply_archive(
        &tx,
        user,
        id,
        source["version"].as_u64().ok_or_else(upstream)?,
        &files,
        &origin,
    )?;
    tx.commit()?;
    Ok(result)
}

fn authorize(api: &ProductApi, user: &User) -> Result<User> {
    api.store
        .user(&user.id)?
        .filter(|u| u.enabled && u.role == "admin")
        .ok_or_else(|| ProductError {
            status: 403,
            detail: "Administrator required".into(),
        })
}

fn authorize_transaction(db: &Connection, user: &User) -> Result<()> {
    let allowed: bool = db.query_row(
        "SELECT EXISTS(SELECT 1 FROM users WHERE id=?1 AND enabled=1 AND role='admin')",
        [&user.id],
        |r| r.get(0),
    )?;
    if !allowed {
        return Err(ProductError {
            status: 403,
            detail: "Administrator required".into(),
        });
    }
    Ok(())
}

fn input_connection(body: &Value) -> Result<(Value, String)> {
    object(body)?;
    let protocol = body["protocol"].as_str().unwrap_or("openai_chat");
    if !matches!(
        protocol,
        "openai_chat" | "openai_responses" | "anthropic_messages" | "google_gemini" | "custom_http"
    ) {
        return Err(bad("Unknown provider protocol"));
    }
    let mut base = string(body, "base_url", 1, 1500)?
        .trim_end_matches('/')
        .to_owned();
    if protocol != "custom_http" {
        for suffix in ["/chat/completions", "/messages", "/responses", "/models"] {
            if base.ends_with(suffix) {
                base.truncate(base.len() - suffix.len());
                break;
            }
        }
    }
    let external = bool_or(body, "external", true)?;
    validate_target(&base, external)?;
    let key = body["api_key"].as_str().unwrap_or("");
    if key.len() > 4096 {
        return Err(bad("Invalid provider credential"));
    }
    let protocol_config = body.get("protocol_config").cloned().unwrap_or(json!({}));
    if protocol == "custom_http" {
        protocols::validate(&protocol_config)?;
    }
    Ok((
        json!({"protocol":protocol,"base_url":base,"external":external,"protocol_config":protocol_config}),
        key.into(),
    ))
}

fn validate_target(base: &str, external: bool) -> Result<url::Url> {
    let url = url::Url::parse(base).map_err(|_| bad("Invalid provider URL"))?;
    if !matches!(url.scheme(), "http" | "https")
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || external && url.scheme() != "https"
    {
        return Err(bad("Provider URL must have no credentials, query, or fragment; external providers require HTTPS"));
    }
    Ok(url)
}

fn endpoint(value: &str) -> Result<&str> {
    if !value.starts_with('/')
        || value.starts_with("//")
        || value.len() > 1000
        || value.contains(['?', '#', '\\', ':', '%'])
        || value.split('/').any(|p| matches!(p, "." | ".."))
        || value.chars().any(|c| c.is_control() || c.is_whitespace())
    {
        Err(bad("Invalid provider endpoint"))
    } else {
        Ok(value)
    }
}

fn client(connection: &Value, key: &str) -> Result<Client> {
    let external = connection["external"]
        .as_bool()
        .ok_or_else(|| bad("Invalid provider connection"))?;
    let base = validate_target(
        connection["base_url"].as_str().ok_or_else(upstream)?,
        external,
    )?;
    let mut builder = Client::builder()
        .timeout(Duration::from_secs(30))
        .connect_timeout(Duration::from_secs(10))
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy();
    if !external {
        let host = base.host_str().ok_or_else(upstream)?;
        let addrs = (host, base.port_or_known_default().ok_or_else(upstream)?)
            .to_socket_addrs()
            .map_err(|_| upstream())?
            .collect::<Vec<_>>();
        if addrs.is_empty()
            || addrs.iter().any(|a| match a.ip() {
                IpAddr::V4(ip) => {
                    !((ip.is_private() || ip.is_loopback())
                        && !ip.is_link_local()
                        && !ip.is_multicast()
                        && !ip.is_unspecified())
                }
                IpAddr::V6(ip) => !(ip.is_loopback() || ip.is_unique_local()),
            })
        {
            return Err(bad(
                "Internal provider must resolve only to private or loopback addresses",
            ));
        }
        builder = builder.resolve_to_addrs(host, &addrs);
    }
    let mut headers = HeaderMap::new();
    headers.insert("accept", HeaderValue::from_static("application/json"));
    headers.insert("cache-control", HeaderValue::from_static("no-cache"));
    headers.insert("pragma", HeaderValue::from_static("no-cache"));
    let mut insert = |name: &str, value: &str| -> Result<()> {
        headers.insert(
            HeaderName::from_bytes(name.as_bytes()).map_err(|_| bad("Invalid provider header"))?,
            HeaderValue::from_str(value).map_err(|_| bad("Invalid provider header"))?,
        );
        Ok(())
    };
    // Apply the same validated static headers to every protocol. The helper
    // rejects credential-like and hop-by-hop names before they reach reqwest.
    for (name, value) in super::protocols::static_headers(&connection["protocol_config"])? {
        insert(&name, &value)?;
    }
    match connection["protocol"].as_str().ok_or_else(upstream)? {
        "anthropic_messages" => {
            insert("x-api-key", key)?;
            insert("anthropic-version", "2023-06-01")?;
        }
        "google_gemini" => insert("x-goog-api-key", key)?,
        "custom_http" => {
            let auth = connection["protocol_config"]
                .get("auth")
                .cloned()
                .unwrap_or(json!({"header":"Authorization","value":"Bearer {{api_key}}"}));
            if !auth.is_null() {
                let name = auth["header"].as_str().unwrap_or("Authorization");
                if matches!(
                    name.to_ascii_lowercase().as_str(),
                    "host"
                        | "content-length"
                        | "connection"
                        | "transfer-encoding"
                        | "cookie"
                        | "set-cookie"
                        | "proxy-authorization"
                ) {
                    return Err(bad("Protected provider authentication header"));
                }
                let value = auth["value"].as_str().unwrap_or("Bearer {{api_key}}");
                if value.matches("{{api_key}}").count() != 1 {
                    return Err(bad("Invalid provider authentication template"));
                }
                insert(name, &value.replace("{{api_key}}", key))?;
            }
        }
        _ => {
            if !key.is_empty() {
                insert("authorization", &format!("Bearer {key}"))?;
            }
        }
    }
    builder
        .default_headers(headers)
        .build()
        .map_err(|_| upstream())
}

fn response(response: Response) -> Result<Value> {
    if !response.status().is_success() {
        return Err(upstream());
    }
    let mut bytes = Vec::new();
    response
        .take(8_000_001)
        .read_to_end(&mut bytes)
        .map_err(|_| upstream())?;
    if bytes.len() > 8_000_000 {
        return Err(upstream());
    }
    let value: Value = serde_json::from_slice(&bytes).map_err(|_| upstream())?;
    if !value.is_object() || value.get("error").is_some_and(|v| !v.is_null()) {
        return Err(upstream());
    }
    Ok(value)
}

fn pointer<'a>(value: &'a Value, path: &Value) -> Result<&'a Value> {
    value
        .pointer(path.as_str().ok_or_else(upstream)?)
        .ok_or_else(upstream)
}

fn complete_custom_catalog(data: &Value, items: &Value) -> Result<()> {
    let pointer = items.as_str().ok_or_else(upstream)?;
    let mut envelopes = vec![data];
    let mut ancestor = String::new();
    let parts = pointer.split('/').skip(1).collect::<Vec<_>>();
    for part in parts.iter().take(parts.len().saturating_sub(1)) {
        ancestor.push('/');
        ancestor.push_str(part);
        envelopes.push(data.pointer(&ancestor).ok_or_else(upstream)?);
    }
    let mut visited = BTreeSet::new();
    while let Some(value) = envelopes.pop() {
        let Some(envelope) = value.as_object() else {
            continue;
        };
        if !visited.insert(std::ptr::from_ref(value) as usize) {
            continue;
        }
        if visited.len() > 64 {
            return Err(upstream());
        }
        for name in ["error", "errors"] {
            if envelope.get(name).is_some_and(|v| {
                !v.is_null() && v != false && v != "" && v != &json!([]) && v != &json!({})
            }) {
                return Err(upstream());
            }
        }
        for name in ["has_more", "hasMore", "has_next", "hasNext"] {
            if envelope.get(name).is_some_and(|v| v != false) {
                return Err(upstream());
            }
        }
        for name in [
            "nextPageToken",
            "next_page_token",
            "next_page",
            "nextPage",
            "next",
            "next_cursor",
            "nextCursor",
        ] {
            if envelope
                .get(name)
                .is_some_and(|v| !v.is_null() && v != false && v != "")
            {
                return Err(upstream());
            }
        }
        for name in [
            "pagination",
            "paging",
            "page_info",
            "pageInfo",
            "meta",
            "links",
        ] {
            if let Some(value) = envelope.get(name) {
                envelopes.push(value);
            }
        }
    }
    Ok(())
}

fn discover(connection: &Value, key: &str) -> Result<Vec<Value>> {
    let client = client(connection, key)?;
    let protocol = connection["protocol"].as_str().ok_or_else(upstream)?;
    let custom = &connection["protocol_config"]["discovery"];
    let path = if protocol == "custom_http" {
        endpoint(
            custom["endpoint"]
                .as_str()
                .ok_or_else(|| bad("Custom protocol has no discovery endpoint"))?,
        )?
    } else {
        "/models"
    };
    let url = format!(
        "{}{path}",
        connection["base_url"]
            .as_str()
            .ok_or_else(upstream)?
            .trim_end_matches('/')
    );
    let mut query = BTreeMap::<String, String>::new();
    if protocol == "google_gemini" {
        query.insert("pageSize".into(), "200".into());
    }
    if protocol == "anthropic_messages" {
        query.insert("limit".into(), "100".into());
    }
    let mut models = Vec::new();
    let mut seen = BTreeSet::new();
    let mut cursors = BTreeSet::new();
    for _ in 0..10 {
        let upstream_response = client
            .get(&url)
            .query(&query)
            .send()
            .map_err(|_| upstream())?;
        if protocol == "custom_http"
            && upstream_response
                .headers()
                .get_all("link")
                .iter()
                .filter_map(|v| v.to_str().ok())
                .any(|v| {
                    v.split(';')
                        .any(|part| part.trim().starts_with("rel=") && part.contains("next"))
                })
        {
            return Err(upstream());
        }
        let data = response(upstream_response)?;
        let empty = Vec::new();
        let items = if protocol == "custom_http" {
            pointer(&data, &custom["items"])?
        } else if protocol == "google_gemini" {
            &data["models"]
        } else {
            &data["data"]
        };
        let items = if protocol == "google_gemini" && items.is_null() {
            if data
                .as_object()
                .ok_or_else(upstream)?
                .keys()
                .any(|key| !matches!(key.as_str(), "models" | "nextPageToken"))
            {
                return Err(upstream());
            }
            &empty
        } else {
            items.as_array().ok_or_else(upstream)?
        };
        if items.len() > 2000 {
            return Err(upstream());
        }
        for item in items {
            let mid = if protocol == "custom_http" {
                pointer(item, &custom["id"])?
            } else if protocol == "google_gemini" {
                &item["name"]
            } else {
                &item["id"]
            };
            let mid = mid
                .as_str()
                .filter(|v| !v.trim().is_empty() && v.len() <= 200)
                .ok_or_else(upstream)?;
            let mid = if protocol == "google_gemini" {
                mid.strip_prefix("models/").unwrap_or(mid)
            } else {
                mid
            };
            let name = if protocol == "custom_http" && !custom["name"].is_null() {
                pointer(item, &custom["name"])?.as_str()
            } else if protocol == "google_gemini" {
                item["displayName"].as_str()
            } else {
                item["display_name"]
                    .as_str()
                    .or_else(|| item["name"].as_str())
            }
            .unwrap_or(mid);
            if name.len() > 200 {
                return Err(upstream());
            }
            if !seen.insert(mid.to_owned()) {
                continue;
            }
            let mut row = json!({"id":mid,"name":name,"label":name});
            let capabilities = if protocol == "google_gemini" {
                &item["supportedGenerationMethods"]
            } else {
                &item["capabilities"]
            };
            if let Some(caps) = capabilities.as_array().filter(|v| {
                v.len() <= 100 && v.iter().all(|v| v.as_str().is_some_and(|s| s.len() <= 200))
            }) {
                row["capabilities"] = json!(caps)
            }
            if let Some(context) = item["inputTokenLimit"].as_u64().filter(|v| *v > 0) {
                row["context_window"] = json!(context)
            }
            models.push(row);
            if models.len() > 2000 {
                return Err(upstream());
            }
        }
        if protocol == "custom_http" {
            complete_custom_catalog(&data, &custom["items"])?;
            return Ok(models);
        }
        let next = if protocol == "google_gemini" {
            match data.get("nextPageToken") {
                None | Some(Value::Null) => "",
                Some(Value::String(value)) => value,
                _ => return Err(upstream()),
            }
        } else {
            match data.get("has_more") {
                Some(Value::Bool(true)) => data["last_id"]
                    .as_str()
                    .filter(|v| !v.trim().is_empty())
                    .ok_or_else(upstream)?,
                None | Some(Value::Bool(false)) => "",
                _ => return Err(upstream()),
            }
        };
        if next.is_empty() {
            return Ok(models);
        }
        if next.len() > 2000 || !cursors.insert(next.to_owned()) {
            return Err(upstream());
        }
        query.insert(
            if protocol == "google_gemini" {
                "pageToken"
            } else if protocol == "anthropic_messages" {
                "after_id"
            } else {
                "after"
            }
            .into(),
            next.into(),
        );
    }
    Err(upstream())
}

fn sync_catalog(api: &ProductApi, user: &User, id: &str) -> Result<Value> {
    let mut db = api.store.connect()?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    authorize_transaction(&tx, user)?;
    let (rid,connection,encrypted):(String,Value,String)=tx.query_row("SELECT p.revision_id,r.config,r.encrypted_key FROM providers p JOIN provider_revisions r ON r.id=p.revision_id AND r.provider_id=p.id WHERE p.id=?1 AND p.enabled=1",[id],|r|Ok((r.get(0)?,decode(r.get(1)?)?,r.get(2)?))).optional()?.ok_or_else(missing)?;
    let key = api.store.decrypt_secret(&encrypted)?;
    let cache_id = format!("{id}:{rid}");
    let token = uid();
    let stamp = now(&tx)?;
    tx.execute("INSERT INTO provider_model_catalogs(id,provider_id,revision_id,entries,last_synced_at,last_attempt_at,error,sync_token) VALUES(?1,?2,?3,'[]','',?4,'',?5) ON CONFLICT(id) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,error='',sync_token=excluded.sync_token",params![cache_id,id,rid,stamp,token])?;
    tx.commit()?;
    let result = discover(&connection, &key);
    authorize(api, user)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    authorize_transaction(&tx, user)?;
    let current:Option<(String,String)>=tx.query_row("SELECT p.revision_id,c.sync_token FROM providers p JOIN provider_model_catalogs c ON c.id=?2 WHERE p.id=?1 AND p.enabled=1",params![id,cache_id],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if current != Some((rid.clone(), token)) {
        return Err(conflict());
    }
    let rows = match result {
        Ok(rows) => rows,
        Err(error) => {
            tx.execute(
                "UPDATE provider_model_catalogs SET error=?1,sync_token='' WHERE id=?2",
                params![error.detail, cache_id],
            )?;
            emit(
                &tx,
                user,
                "provider.catalog.failed",
                json!({"provider_id":id,"revision_id":rid,"error":error.detail}),
            )?;
            tx.commit()?;
            return Err(error);
        }
    };
    let before: Value = tx.query_row(
        "SELECT entries FROM provider_model_catalogs WHERE id=?1",
        [&cache_id],
        |r| decode(r.get(0)?),
    )?;
    let mut inventory: BTreeMap<String, Value> = before
        .as_array()
        .ok_or_else(upstream)?
        .iter()
        .filter_map(|r| r["id"].as_str().map(|id| (id.into(), r.clone())))
        .collect();
    let stamp = now(&tx)?;
    let mut changes = json!({"added":[],"updated":[],"removed":[],"restored":[],"unchanged":[]});
    for (mid, row) in &mut inventory {
        if !rows.iter().any(|r| r["id"] == *mid) {
            if row["available"] == true {
                changes["removed"].as_array_mut().unwrap().push(json!(mid));
            }
            row["available"] = json!(false);
            if row["removed_at"].as_str().is_none_or(str::is_empty) {
                row["removed_at"] = json!(stamp)
            }
        }
    }
    for mut row in rows.clone() {
        let mid = row["id"].as_str().unwrap().to_owned();
        let old = inventory.get(&mid);
        let kind = match old {
            None => "added",
            Some(v) if v["available"] == false => "restored",
            Some(v)
                if ["name", "capabilities", "context_window"]
                    .iter()
                    .any(|k| v[k] != row[k]) =>
            {
                "updated"
            }
            _ => "unchanged",
        };
        changes[kind].as_array_mut().unwrap().push(json!(mid));
        row["first_seen_at"] = old
            .map(|r| r["first_seen_at"].clone())
            .filter(|v| v.as_str().is_some_and(|s| !s.is_empty()))
            .unwrap_or(json!(stamp));
        row["last_seen_at"] = json!(stamp);
        row["available"] = json!(true);
        row["removed_at"] = json!("");
        inventory.insert(mid, row);
    }
    let counts = changes
        .as_object()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.clone(), json!(v.as_array().unwrap().len())))
        .collect::<Map<_, _>>();
    let receipt = json!({"status":"success","completed_at":stamp,"received_count":rows.len(),"changes":changes,"counts":counts});
    tx.execute("UPDATE provider_model_catalogs SET entries=?1,last_synced_at=?2,error='',sync_token='' WHERE id=?3",params![json!(inventory.into_values().collect::<Vec<_>>()).to_string(),stamp,cache_id])?;
    tx.execute("INSERT INTO provider_catalog_pull_receipts VALUES(?1,?2) ON CONFLICT(id) DO UPDATE SET data=excluded.data",params![cache_id,receipt.to_string()])?;
    emit(
        &tx,
        user,
        "provider.catalog.synced",
        json!({"provider_id":id,"revision_id":rid,"pull":receipt}),
    )?;
    let out = super::catalog::catalog(&tx, id)?;
    tx.commit()?;
    Ok(out)
}

#[derive(Default)]
struct Reply {
    text: String,
    calls: Vec<Value>,
    native: Value,
    input: u64,
    output: u64,
}

fn render(template: &Value, vars: &BTreeMap<&str, Value>) -> Result<Value> {
    match template {
        Value::String(value) if value.starts_with("{{") && value.ends_with("}}") => vars
            .get(&value[2..value.len() - 2])
            .cloned()
            .ok_or_else(|| bad("Unknown protocol template variable")),
        Value::String(value) if value.contains("{{") || value.contains("}}") => Err(bad(
            "Protocol template variables must occupy a complete value",
        )),
        Value::Array(values) => values
            .iter()
            .map(|v| render(v, vars))
            .collect::<Result<Vec<_>>>()
            .map(Value::Array),
        Value::Object(values) => values
            .iter()
            .map(|(k, v)| Ok((k.clone(), render(v, vars)?)))
            .collect::<Result<Map<_, _>>>()
            .map(Value::Object),
        _ => Ok(template.clone()),
    }
}

fn custom_body(
    config: &Value,
    mid: &str,
    params: &Value,
    messages: &[Value],
    tools: &[Value],
) -> Result<Value> {
    let defaults = json!({"system":{"role":"system","content":"{{content}}"},"user":{"role":"user","content":"{{content}}"},"assistant":{"role":"assistant","content":"{{content}}","tool_calls":"{{calls}}"},"tool":{"role":"tool","tool_call_id":"{{call_id}}","content":"{{content}}"}});
    let templates = config.get("message_templates").unwrap_or(&defaults);
    let default_call = json!({"id":"{{id}}","type":"function","function":{"name":"{{name}}","arguments":"{{arguments_json}}"}});
    let mut mapped_messages = Vec::new();
    let mut system = Vec::new();
    for message in messages {
        let role = message["role"].as_str().ok_or_else(upstream)?;
        if role == "system" && config["separate_system"] == true {
            system.push(message["content"].as_str().unwrap_or("").to_owned());
            continue;
        }
        let calls = message["calls"]
            .as_array()
            .cloned()
            .unwrap_or_default()
            .iter()
            .map(|call| {
                render(
                    config.get("call_template").unwrap_or(&default_call),
                    &BTreeMap::from([
                        ("id", call["id"].clone()),
                        ("name", call["name"].clone()),
                        ("arguments", call["arguments"].clone()),
                        ("arguments_json", json!(call["arguments"].to_string())),
                    ]),
                )
            })
            .collect::<Result<Vec<_>>>()?;
        mapped_messages.push(render(
            &templates[role],
            &BTreeMap::from([
                ("role", json!(role)),
                ("content", message["content"].clone()),
                ("call_id", message["call_id"].clone()),
                ("calls", json!(calls)),
            ]),
        )?);
    }
    let default_tool = json!({"type":"function","function":"{{tool}}"});
    let mapped_tools = tools
        .iter()
        .map(|tool| {
            render(
                config.get("tool_template").unwrap_or(&default_tool),
                &BTreeMap::from([
                    ("tool", tool.clone()),
                    ("name", tool["name"].clone()),
                    ("description", tool["description"].clone()),
                    ("parameters", tool["parameters"].clone()),
                ]),
            )
        })
        .collect::<Result<Vec<_>>>()?;
    let mut body = render(
        &config["body"],
        &BTreeMap::from([
            ("model", json!(mid)),
            ("parameters", params.clone()),
            ("messages", json!(mapped_messages)),
            ("tools", json!(mapped_tools)),
            ("system", json!(system.join("\n\n"))),
        ]),
    )?;
    object(&body)?;
    if config["parameter_placement"] == "root" {
        for (key, value) in object(params)? {
            if body.get(key).is_some() {
                return Err(bad("Custom parameter overrides a protected body field"));
            }
            body[key] = value.clone();
        }
    }
    Ok(body)
}

fn complete(
    connection: &Value,
    model: &Value,
    key: &str,
    messages: &[Value],
    tools: &[Value],
) -> Result<Reply> {
    let protocol = connection["protocol"].as_str().ok_or_else(upstream)?;
    let mid = model["model_id"].as_str().ok_or_else(upstream)?;
    let params = model.get("parameters").cloned().unwrap_or(json!({}));
    object(&params)?;
    let declared = model
        .get("allowed_parameters")
        .cloned()
        .unwrap_or_else(|| json!(params.as_object().unwrap().keys().collect::<Vec<_>>()));
    validate_connection_parameters(connection, &declared, &params)?;
    if params.as_object().unwrap().keys().any(|k| {
        matches!(
            k.as_str(),
            "model" | "messages" | "input" | "tools" | "stream" | "headers" | "api_key"
        )
    }) {
        return Err(bad("Reserved model parameter"));
    }
    let client = client(connection, key)?;
    let mut body = params;
    let endpoint_path;
    let mut method = reqwest::Method::POST;
    match protocol {
        "openai_chat" => {
            endpoint_path = "/chat/completions".to_owned();
            body["model"] = json!(mid);
            body["stream"] = json!(false);
            body["messages"]=json!(messages.iter().map(|m|match m["role"].as_str(){Some("assistant") if m["native"].is_object()=>m["native"].clone(),Some("assistant")=>json!({"role":"assistant","content":m["content"],"tool_calls":m["calls"].as_array().unwrap_or(&Vec::new()).iter().map(|c|json!({"id":c["id"],"type":"function","function":{"name":c["name"],"arguments":c["arguments"].to_string()}})).collect::<Vec<_>>()}),Some("tool")=>json!({"role":"tool","tool_call_id":m["call_id"],"content":m["content"]}),_=>m.clone()}).collect::<Vec<_>>());
            if !tools.is_empty() {
                body["tools"] = json!(tools
                    .iter()
                    .map(|t| json!({"type":"function","function":t}))
                    .collect::<Vec<_>>())
            }
        }
        "openai_responses" => {
            endpoint_path = "/responses".to_owned();
            body["model"] = json!(mid);
            body["stream"] = json!(false);
            let mut input = Vec::new();
            for m in messages {
                match m["role"].as_str(){Some("tool")=>input.push(json!({"type":"function_call_output","call_id":m["call_id"],"output":m["content"]})),Some("assistant")=>{if let Some(native)=m["native"].as_array(){input.extend(native.clone())}else if m["content"]!=""{input.push(json!({"role":"assistant","content":m["content"]}))}},_=>input.push(m.clone())}
            }
            body["input"] = json!(input);
            if !tools.is_empty() {
                body["tools"] = json!(tools
                    .iter()
                    .map(|t| {
                        let mut t = t.clone();
                        t["type"] = json!("function");
                        t
                    })
                    .collect::<Vec<_>>())
            }
        }
        "anthropic_messages" => {
            endpoint_path = "/messages".to_owned();
            body["model"] = json!(mid);
            if body.get("max_tokens").is_none() {
                body["max_tokens"] = json!(4096)
            }
            let mut input: Vec<Value> = Vec::new();
            for m in messages {
                let (role, blocks) = match m["role"].as_str() {
                    Some("tool") => (
                        "user",
                        json!([{"type":"tool_result","tool_use_id":m["call_id"],"content":m["content"]}]),
                    ),
                    Some("assistant") => ("assistant", m["native"].clone()),
                    _ => ("user", json!([{"type":"text","text":m["content"]}])),
                };
                if let Some(last) = input.last_mut().filter(|v| v["role"] == role) {
                    last["content"]
                        .as_array_mut()
                        .unwrap()
                        .extend(blocks.as_array().ok_or_else(upstream)?.clone())
                } else {
                    input.push(json!({"role":role,"content":blocks}))
                }
            }
            body["messages"] = json!(input);
            if !tools.is_empty() {
                body["tools"]=json!(tools.iter().map(|t|json!({"name":t["name"],"description":t["description"],"input_schema":t["parameters"]})).collect::<Vec<_>>())
            }
        }
        "google_gemini" => {
            if mid.contains(['?', '#', '/', '%']) {
                return Err(bad("Invalid Gemini model identifier"));
            }
            endpoint_path = format!("/models/{mid}:generateContent");
            let mut contents = Vec::new();
            for m in messages {
                let (role, parts) = match m["role"].as_str() {
                    Some("assistant") => ("model", m["native"].clone()),
                    Some("tool") => (
                        "user",
                        json!([{"functionResponse":{"name":"forge_connection_check","response":serde_json::from_str::<Value>(m["content"].as_str().ok_or_else(upstream)?).map_err(|_|upstream())?}}]),
                    ),
                    _ => ("user", json!([{"text":m["content"]}])),
                };
                contents.push(json!({"role":role,"parts":parts}))
            }
            body = json!({"contents":contents,"generationConfig":body});
            if !tools.is_empty() {
                body["tools"] = json!([{"functionDeclarations":tools}])
            }
        }
        "custom_http" => {
            let config = &connection["protocol_config"];
            endpoint_path = endpoint(
                config["endpoint"]
                    .as_str()
                    .ok_or_else(|| bad("Custom protocol endpoint is missing"))?,
            )?
            .to_owned();
            method = match config["method"].as_str().unwrap_or("POST") {
                "POST" => reqwest::Method::POST,
                "PUT" => reqwest::Method::PUT,
                "PATCH" => reqwest::Method::PATCH,
                _ => return Err(bad("Invalid custom protocol method")),
            };
            body = custom_body(config, mid, &body, messages, tools)?;
        }
        _ => return Err(bad("Unknown provider protocol")),
    }
    let raw = response(
        client
            .request(
                method,
                format!(
                    "{}{}",
                    connection["base_url"]
                        .as_str()
                        .ok_or_else(upstream)?
                        .trim_end_matches('/'),
                    endpoint_path
                ),
            )
            .json(&body)
            .send()
            .map_err(|_| upstream())?,
    )?;
    let mut reply = Reply::default();
    match protocol {
        "openai_chat" => {
            let message = &raw["choices"][0]["message"];
            reply.native = message.clone();
            reply.text = message["content"].as_str().unwrap_or("").into();
            reply.input = raw["usage"]["prompt_tokens"].as_u64().unwrap_or(0);
            reply.output = raw["usage"]["completion_tokens"].as_u64().unwrap_or(0);
            if let Some(calls) = message["tool_calls"].as_array() {
                for call in calls {
                    reply.calls.push(json!({"id":call["id"],"name":call["function"]["name"],"arguments":serde_json::from_str::<Value>(call["function"]["arguments"].as_str().ok_or_else(upstream)?).map_err(|_|upstream())?}))
                }
            }
        }
        "openai_responses" => {
            reply.native = raw["output"].clone();
            for item in raw["output"].as_array().ok_or_else(upstream)? {
                if item["type"] == "function_call" {
                    reply.calls.push(json!({"id":item["call_id"],"name":item["name"],"arguments":serde_json::from_str::<Value>(item["arguments"].as_str().ok_or_else(upstream)?).map_err(|_|upstream())?}))
                }
                if let Some(content) = item["content"].as_array() {
                    for c in content {
                        if let Some(text) = c["text"].as_str() {
                            reply.text.push_str(text)
                        }
                    }
                }
            }
            reply.input = raw["usage"]["input_tokens"].as_u64().unwrap_or(0);
            reply.output = raw["usage"]["output_tokens"].as_u64().unwrap_or(0);
        }
        "anthropic_messages" => {
            reply.native = raw["content"].clone();
            for block in raw["content"].as_array().ok_or_else(upstream)? {
                if block["type"] == "text" {
                    reply
                        .text
                        .push_str(block["text"].as_str().ok_or_else(upstream)?)
                }
                if block["type"] == "tool_use" {
                    reply.calls.push(
                        json!({"id":block["id"],"name":block["name"],"arguments":block["input"]}),
                    )
                }
            }
            reply.input = raw["usage"]["input_tokens"].as_u64().unwrap_or(0);
            reply.output = raw["usage"]["output_tokens"].as_u64().unwrap_or(0);
        }
        "google_gemini" => {
            reply.native = raw["candidates"][0]["content"]["parts"].clone();
            for part in reply.native.as_array().ok_or_else(upstream)? {
                if let Some(text) = part["text"].as_str() {
                    reply.text.push_str(text)
                }
                if part["functionCall"].is_object() {
                    reply.calls.push(json!({"id":part["functionCall"].get("id").cloned().unwrap_or(json!(uid())),"name":part["functionCall"]["name"],"arguments":part["functionCall"]["args"]}))
                }
            }
            reply.input = raw["usageMetadata"]["promptTokenCount"]
                .as_u64()
                .unwrap_or(0);
            reply.output = raw["usageMetadata"]["candidatesTokenCount"]
                .as_u64()
                .unwrap_or(0);
        }
        "custom_http" => {
            let map = &connection["protocol_config"]["response"];
            reply.text = pointer(&raw, &map["text"])?
                .as_str()
                .ok_or_else(upstream)?
                .to_owned();
            if !map["calls"].is_null() {
                let calls = pointer(&raw, &map["calls"])?;
                if !calls.is_null() {
                    for call in calls.as_array().ok_or_else(upstream)? {
                        let fields = &map["call_fields"];
                        let arguments = pointer(call, &fields["arguments"])?;
                        let arguments = if let Some(text) = arguments.as_str() {
                            serde_json::from_str::<Value>(text).map_err(|_| upstream())?
                        } else {
                            arguments.clone()
                        };
                        reply.calls.push(json!({"id":pointer(call,&fields["id"] )?,"name":pointer(call,&fields["name"] )?,"arguments":arguments}));
                    }
                }
            }
            if !map["input_tokens"].is_null() {
                reply.input = pointer(&raw, &map["input_tokens"])?.as_u64().unwrap_or(0)
            }
            if !map["output_tokens"].is_null() {
                reply.output = pointer(&raw, &map["output_tokens"])?.as_u64().unwrap_or(0)
            }
        }
        _ => return Err(upstream()),
    }
    if reply.text.is_empty() && reply.calls.is_empty() {
        return Err(upstream());
    }
    Ok(reply)
}

fn probe(connection: &Value, model: &Value, key: &str) -> Result<Value> {
    let nonce = uid();
    let tool = json!({"name":"forge_connection_check","description":"Read-only connection check. Return the given nonce; no files or commands.","parameters":{"type":"object","properties":{"nonce":{"type":"string"}},"required":["nonce"],"additionalProperties":false}});
    let mut messages = vec![
        json!({"role":"user","content":format!("Connection test only, no project data. Call forge_connection_check exactly once with nonce {nonce}. Do not answer in text yet.")}),
    ];
    let first = complete(connection, model, key, &messages, &[tool])?;
    if first.calls.len() != 1
        || first.calls[0]["name"] != "forge_connection_check"
        || first.calls[0]["arguments"] != json!({"nonce":nonce})
        || first.calls[0]["id"].as_str().is_none_or(str::is_empty)
    {
        return Err(upstream());
    }
    messages.push(
        json!({"role":"assistant","content":first.text,"calls":first.calls,"native":first.native}),
    );
    messages.push(json!({"role":"tool","call_id":first.calls[0]["id"],"content":json!({"ok":true,"nonce":nonce}).to_string()}));
    messages.push(json!({"role":"user","content":"The test tool succeeded. Reply only OK; do not call another tool."}));
    let second = complete(connection, model, key, &messages, &[])?;
    if !second.calls.is_empty()
        || second
            .text
            .trim()
            .trim_end_matches(['.', '!'])
            .to_uppercase()
            != "OK"
    {
        return Err(upstream());
    }
    Ok(
        json!({"requests":2,"tool_roundtrip":true,"input_tokens":first.input+second.input,"output_tokens":first.output+second.output}),
    )
}

fn model_test(api: &ProductApi, user: &User, id: &str, tools: bool) -> Result<Value> {
    let mut db = api.store.connect()?;
    let model = get_config(&db, ConfigTable::Model, id)?;
    let pid = model["provider_id"].as_str().ok_or_else(upstream)?;
    let(rid,connection,encrypted):(String,Value,String)=db.query_row("SELECT p.revision_id,r.config,r.encrypted_key FROM providers p JOIN provider_revisions r ON r.id=p.revision_id WHERE p.id=?1 AND p.enabled=1",[pid],|r|Ok((r.get(0)?,decode(r.get(1)?)?,r.get(2)?))).optional()?.ok_or_else(missing)?;
    let secret = api.store.decrypt_secret(&encrypted)?;
    let mut result = if tools {
        probe(&connection, &model, &secret)?
    } else {
        let reply = complete(
            &connection,
            &model,
            &secret,
            &[json!({"role":"user","content":"Connectivity test only. Reply briefly with OK."})],
            &[],
        )?;
        json!({"text":reply.text.chars().take(300).collect::<String>(),"input_tokens":reply.input,"output_tokens":reply.output})
    };
    authorize(api, user)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let current = get_config(&tx, ConfigTable::Model, id)?;
    authorize_transaction(&tx, user)?;
    let current_revision: String = tx
        .query_row(
            "SELECT revision_id FROM providers WHERE id=?1 AND enabled=1",
            [pid],
            |r| r.get(0),
        )
        .optional()?
        .ok_or_else(conflict)?;
    if current_revision != rid || current != model {
        return Err(conflict());
    }
    if tools {
        result["checked_at"] = json!(now(&tx)?);
        let mut updated = model.clone();
        updated["supports_tools"] = json!(true);
        let mut saved = save_config(
            &tx,
            user,
            ConfigTable::Model,
            id,
            updated,
            model["version"].as_u64(),
        )?;
        put_config(
            &tx,
            &format!("probe:{id}"),
            &json!({"revision_id":rid,"model_version":saved["version"],"probe":result}),
        )?;
        saved["probe"] = result;
        result = saved;
    }
    emit(
        &tx,
        user,
        if tools {
            "model.tools_tested"
        } else {
            "model.tested"
        },
        json!({"model_id":id,"revision_id":rid}),
    )?;
    tx.commit()?;
    Ok(result)
}

fn setup_connect(
    api: &ProductApi,
    user: &User,
    body: &Value,
    query: &BTreeMap<String, String>,
) -> Result<Value> {
    let key = query
        .get("__idempotency_key")
        .filter(|v| {
            !v.is_empty()
                && v.len() <= 100
                && v.chars()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_'))
        })
        .ok_or_else(|| bad("A valid Idempotency-Key header is required"))?;
    if body["acknowledge_probe"] != true {
        return Err(bad(
            "The connection check requires acknowledgement of two model requests",
        ));
    }
    let name = body
        .get("name")
        .map(|_| string(body, "name", 1, 100))
        .transpose()?;
    let (connection, secret) = input_connection(body)?;
    let mid = string(body, "model_id", 1, 200)?;
    let parameters =
        body.get("parameters")
            .cloned()
            .unwrap_or(match connection["protocol"].as_str() {
                Some("anthropic_messages") => json!({"max_tokens":4096}),
                Some("openai_responses") => json!({"max_output_tokens":4096}),
                Some("google_gemini") => json!({"maxOutputTokens":4096}),
                _ => json!({}),
            });
    object(&parameters)?;
    validate_connection_parameters(
        &connection,
        &json!(parameters.as_object().unwrap().keys().collect::<Vec<_>>()),
        &parameters,
    )?;
    let operation = format!("setup:{}", digest(&json!([user.id, key])));
    let fingerprint = digest(body);
    let mut db = api.store.connect()?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let existing: Option<(String, Value)> = tx
        .query_row(
            "SELECT state,result FROM steps WHERE id=?1",
            [&operation],
            |r| Ok((r.get(0)?, decode(r.get(1)?)?)),
        )
        .optional()?;
    authorize_transaction(&tx, user)?;
    if let Some((state, result)) = existing {
        if result["fingerprint"] != fingerprint {
            return Err(conflict());
        }
        return if state == "completed" {
            Ok(result["response"].clone())
        } else {
            Err(conflict())
        };
    }
    tx.execute(
        "INSERT INTO steps VALUES(?1,'started',?2)",
        params![operation, json!({"fingerprint":fingerprint}).to_string()],
    )?;
    tx.commit()?;
    let result = probe(
        &connection,
        &json!({"model_id":mid,"parameters":parameters}),
        &secret,
    );
    let mut probe = match result {
        Ok(probe) => probe,
        Err(error) => {
            db.execute(
                "UPDATE steps SET state='failed',result=?1 WHERE id=?2",
                params![
                    json!({"fingerprint":fingerprint,"error":error.detail}).to_string(),
                    operation
                ],
            )?;
            return Err(error);
        }
    };
    authorize(api, user)?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let name = name.unwrap_or(mid);
    authorize_transaction(&tx, user)?;
    let pid = uid();
    let rid = uid();
    let mid_id = uid();
    probe["checked_at"] = json!(now(&tx)?);
    tx.execute(
        "INSERT INTO providers VALUES(?1,?2,?3,1)",
        params![pid, name, rid],
    )?;
    tx.execute(
        "INSERT INTO provider_revisions VALUES(?1,?2,?3,?4,?5)",
        params![
            rid,
            pid,
            connection.to_string(),
            api.store.encrypt_secret(&secret)?,
            now(&tx)?
        ],
    )?;
    let model = json!({"name":mid,"model_id":mid,"provider_id":pid,"enabled":true,"supports_tools":true,"allowed_parameters":parameters.as_object().unwrap().keys().collect::<Vec<_>>(),"parameters":parameters});
    save_config(&tx, user, ConfigTable::Model, &mid_id, model, None)?;
    let (bindings, skill_report) = if body["install_builtin_skills"] == true {
        install_builtins(&tx, user)?
    } else {
        (BTreeMap::new(), Vec::new())
    };
    let mut changed = Vec::new();
    if body["bind_unassigned_agents"] == true || !bindings.is_empty() {
        for mut agent in configs(&tx, ConfigTable::Agent)? {
            if agent["enabled"] != false {
                let aid = agent["id"].as_str().unwrap().to_owned();
                let version = agent["version"].as_u64();
                let mut dirty = false;
                if body["bind_unassigned_agents"] == true
                    && agent["model_profile_id"].as_str().is_none_or(str::is_empty)
                {
                    agent["model_profile_id"] = json!(mid_id);
                    dirty = true;
                }
                if agent["skills"].as_array().is_none_or(Vec::is_empty) {
                    if let Some(role) = agent["role"].as_str() {
                        if let Some(skills) = bindings.get(role) {
                            agent["skills"] = json!(skills);
                            dirty = true;
                        }
                    }
                }
                if !dirty {
                    continue;
                }
                save_config(&tx, user, ConfigTable::Agent, &aid, agent, version)?;
                changed.push(aid);
            }
        }
    }
    let result = json!({"provider_id":pid,"revision_id":rid,"model_id":mid_id,"model_version":1,"probe":probe,"agents_updated":changed,"skills":skill_report,"bind_unassigned_agents":body["bind_unassigned_agents"]==true});
    put_config(&tx, &format!("probe:{mid_id}"), &result)?;
    if config(&tx, "onboarding", Value::Null)?.is_null() {
        put_config(
            &tx,
            "onboarding",
            &json!({"completed_at":now(&tx)?,"actor":user.id,"initial_model_id":mid_id}),
        )?;
    }
    emit(&tx, user, "setup.connected", result.clone())?;
    tx.execute(
        "UPDATE steps SET state='completed',result=?1 WHERE id=?2",
        params![
            json!({"fingerprint":fingerprint,"response":result}).to_string(),
            operation
        ],
    )?;
    tx.commit()?;
    Ok(result)
}

type SkillBindings = BTreeMap<String, Vec<Value>>;
fn install_builtins(c: &Connection, user: &User) -> Result<(SkillBindings, Vec<Value>)> {
    const MANIFEST: &str =
        include_str!("../../../../../backend/forge/builtin_skills/manifest.json");
    let manifest: Value =
        serde_json::from_str(MANIFEST).map_err(|_| bad("Invalid bundled skill manifest"))?;
    let files = [
        json!({"SKILL.md":include_str!("../../../../../backend/forge/builtin_skills/requirements-clarification/SKILL.md"),"references/checklist.md":include_str!("../../../../../backend/forge/builtin_skills/requirements-clarification/references/checklist.md")}),
        json!({"SKILL.md":include_str!("../../../../../backend/forge/builtin_skills/evidence-code-review/SKILL.md"),"references/checklist.md":include_str!("../../../../../backend/forge/builtin_skills/evidence-code-review/references/checklist.md")}),
        json!({"SKILL.md":include_str!("../../../../../backend/forge/builtin_skills/regression-test-design/SKILL.md"),"references/checklist.md":include_str!("../../../../../backend/forge/builtin_skills/regression-test-design/references/checklist.md")}),
    ];
    let mut bindings = SkillBindings::new();
    let mut reports = Vec::new();
    for (package, files) in manifest["packages"]
        .as_array()
        .ok_or_else(upstream)?
        .iter()
        .zip(files)
    {
        for (path, checksum) in object(&package["files"])? {
            let text = files[path].as_str().ok_or_else(upstream)?;
            if json!(format!("{:x}", Sha256::digest(text.as_bytes()))) != *checksum {
                return Err(bad(
                    "Bundled skill checksum does not match release manifest",
                ));
            }
        }
        let name = package["name"].as_str().ok_or_else(upstream)?;
        let existing: Option<String> = c
            .query_row("SELECT id FROM skills WHERE name=?1", [name], |r| r.get(0))
            .optional()?;
        let skill = if let Some(id) = existing {
            let existing = super::skills::get(c, &id, None, true)?;
            if existing["enabled"] != true
                || existing["status"] != "approved"
                || existing["digest"] != digest(&files)
            {
                reports.push(json!({"name":name,"status":"existing_preserved"}));
                continue;
            }
            existing
        } else {
            super::skills::save(c, user, None, None, &files)?
        };
        let id = skill["id"].as_str().ok_or_else(upstream)?;
        let version = skill["version"].as_u64().ok_or_else(upstream)?;
        c.execute("UPDATE skill_revisions SET status='approved',approved_by='builtin:0.8.0' WHERE skill_id=?1 AND version=?2",params![id,version])?;
        for role in package["roles"].as_array().ok_or_else(upstream)? {
            bindings
                .entry(role.as_str().ok_or_else(upstream)?.into())
                .or_default()
                .push(json!({"skill_id":id,"version":version}));
        }
        reports.push(json!({"name":name,"status":"ready","skill_id":id,"version":version}));
        emit(
            c,
            user,
            "skill.builtin.installed",
            json!({"skill_id":id,"version":version,"source":"bundled-v0.8.0"}),
        )?;
    }
    Ok((bindings, reports))
}

fn validate_connection_parameters(
    connection: &Value,
    declared: &Value,
    parameters: &Value,
) -> Result<()> {
    if connection["protocol"] != "custom_http" {
        return validate_parameters(
            connection["protocol"].as_str().ok_or_else(upstream)?,
            declared,
            parameters,
        );
    }
    validate_custom_parameters(&connection["protocol_config"], declared, parameters)
}

pub(super) fn validate_custom_parameters(
    config: &Value,
    declared: &Value,
    parameters: &Value,
) -> Result<()> {
    let supported = config["supported_parameters"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    for name in declared
        .as_array()
        .ok_or_else(|| bad("Invalid declared parameters"))?
    {
        if !name.is_string() || !supported.contains(name) {
            return Err(bad("Undeclared custom model parameter"));
        }
    }
    for (name, value) in object(parameters)? {
        if !declared.as_array().unwrap().contains(&json!(name)) {
            return Err(bad("Model parameter is not allowed"));
        }
        let rule = &config["parameter_schema"][name];
        if rule.is_null() {
            continue;
        }
        let valid = match rule["type"].as_str() {
            Some("string") => value.is_string(),
            Some("number") => value.is_number(),
            Some("integer") => value.is_i64() || value.is_u64(),
            Some("boolean") => value.is_boolean(),
            Some("object") => value.is_object(),
            Some("array") => value.is_array(),
            _ => false,
        };
        if !valid
            || rule["enum"]
                .as_array()
                .is_some_and(|options| !options.contains(value))
        {
            return Err(bad("Invalid custom model parameter"));
        }
        if let Some(number) = value.as_f64() {
            if rule["minimum"].as_f64().is_some_and(|n| number < n)
                || rule["maximum"].as_f64().is_some_and(|n| number > n)
            {
                return Err(bad("Custom model parameter is out of range"));
            }
        }
    }
    if let Some(schema) = config["parameter_schema"].as_object() {
        for (name, rule) in schema {
            if rule["required"] == true && parameters.get(name).is_none() {
                return Err(bad("Required custom model parameter is missing"));
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        io::{BufRead, BufReader, Write},
        net::TcpListener,
        sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        },
        thread,
    };

    fn fixture(protocol: &str, total: usize) -> (String, Arc<AtomicUsize>, thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let calls = Arc::new(AtomicUsize::new(0));
        let count = calls.clone();
        let protocol = protocol.to_owned();
        let thread = thread::spawn(move || {
            for _ in 0..total {
                let (mut socket, _) = listener.accept().unwrap();
                socket
                    .set_read_timeout(Some(Duration::from_secs(5)))
                    .unwrap();
                let mut reader = BufReader::new(socket.try_clone().unwrap());
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                let mut length = 0;
                loop {
                    let mut header = String::new();
                    reader.read_line(&mut header).unwrap();
                    if header == "\r\n" {
                        break;
                    }
                    if let Some((name, value)) = header.split_once(':') {
                        if name.eq_ignore_ascii_case("content-length") {
                            length = value.trim().parse().unwrap();
                        }
                    }
                }
                let mut bytes = vec![0; length];
                reader.read_exact(&mut bytes).unwrap();
                let body: Value = if bytes.is_empty() {
                    json!({})
                } else {
                    serde_json::from_slice(&bytes).unwrap()
                };
                let index = count.fetch_add(1, Ordering::SeqCst);
                let data = if line.starts_with("GET") {
                    json!({"data":[{"id":"fixture-model","name":"Fixture model"}],"has_more":false})
                } else {
                    fn prompt(value: &Value) -> Option<&str> {
                        match value {
                            Value::String(s)
                                if s.starts_with("Connection test only, no project data.") =>
                            {
                                Some(s)
                            }
                            Value::Object(m) => m.values().find_map(prompt),
                            Value::Array(v) => v.iter().find_map(prompt),
                            _ => None,
                        }
                    }
                    let nonce = prompt(&body)
                        .unwrap()
                        .split("nonce ")
                        .nth(1)
                        .unwrap()
                        .split('.')
                        .next()
                        .unwrap();
                    let first = index % 2 == 0;
                    if !first {
                        let raw = body.to_string();
                        assert!(
                            raw.contains("tool_call_id")
                                || raw.contains("function_call_output")
                                || raw.contains("tool_result")
                                || raw.contains("functionResponse")
                        );
                    }
                    match protocol.as_str() {
                        "openai_chat" => {
                            json!({"choices":[{"message":if first{json!({"role":"assistant","content":"","reasoning_content":"preserved","tool_calls":[{"id":"call-1","type":"function","function":{"name":"forge_connection_check","arguments":json!({"nonce":nonce}).to_string()}}]})}else{assert_eq!(body["messages"][1]["reasoning_content"],"preserved");json!({"role":"assistant","content":"OK"})}}],"usage":{"prompt_tokens":1,"completion_tokens":2}})
                        }
                        "openai_responses" => {
                            json!({"output":if first{json!([{"type":"function_call","call_id":"call-1","name":"forge_connection_check","arguments":json!({"nonce":nonce}).to_string()}])}else{json!([{"type":"message","content":[{"type":"output_text","text":"OK"}]}])},"usage":{"input_tokens":1,"output_tokens":2}})
                        }
                        "anthropic_messages" => {
                            json!({"content":if first{json!([{"type":"tool_use","id":"call-1","name":"forge_connection_check","input":{"nonce":nonce}}])}else{json!([{"type":"text","text":"OK"}])},"usage":{"input_tokens":1,"output_tokens":2}})
                        }
                        "google_gemini" => {
                            json!({"candidates":[{"content":{"parts":if first{json!([{"functionCall":{"name":"forge_connection_check","args":{"nonce":nonce}}}])}else{json!([{"text":"OK"}])}}}],"usageMetadata":{"promptTokenCount":1,"candidatesTokenCount":2}})
                        }
                        _ => {
                            json!({"text":if first{""}else{"OK"},"calls":if first{json!([{"id":"call-1","name":"forge_connection_check","arguments":{"nonce":nonce}}])}else{json!([])},"input":1,"output":2})
                        }
                    }
                };
                let bytes = data.to_string();
                write!(socket,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",bytes.len(),bytes).unwrap();
            }
        });
        (url, calls, thread)
    }

    #[test]
    fn five_protocols_complete_real_http_tool_round_trips() {
        for protocol in [
            "openai_chat",
            "openai_responses",
            "anthropic_messages",
            "google_gemini",
            "custom_http",
        ] {
            let (url, calls, thread) = fixture(protocol, 2);
            let custom = json!({"endpoint":"/invoke","body":{"model":"{{model}}","messages":"{{messages}}","tools":"{{tools}}","parameters":"{{parameters}}"},"response":{"text":"/text","calls":"/calls","call_fields":{"id":"/id","name":"/name","arguments":"/arguments"},"input_tokens":"/input","output_tokens":"/output"}});
            let connection = json!({"protocol":protocol,"base_url":url,"external":false,"protocol_config":if protocol=="custom_http"{custom}else{json!({})}});
            let model = json!({"model_id":"fixture-model","parameters":if protocol=="anthropic_messages"{json!({"max_tokens":4096})}else if protocol=="openai_responses"{json!({"max_output_tokens":4096})}else if protocol=="google_gemini"{json!({"maxOutputTokens":4096})}else{json!({})}});
            let result = probe(&connection, &model, "test-secret").unwrap();
            assert_eq!(result["requests"], 2);
            assert_eq!(result["input_tokens"], 2);
            assert_eq!(result["output_tokens"], 4);
            thread.join().unwrap();
            assert_eq!(calls.load(Ordering::SeqCst), 2);
        }
    }

    #[test]
    fn setup_persists_once_and_failed_receipt_never_replays_model_requests() {
        let root = std::env::temp_dir().join(format!("forge-network-{}", uid()));
        std::fs::create_dir_all(&root).unwrap();
        let store = Arc::new(
            ProductStore::open(
                root.join("product.sqlite"),
                None,
                Some("network-test-password"),
            )
            .unwrap()
            .with_encryption_key(&fernet::Fernet::generate_key())
            .unwrap(),
        );
        let user = store
            .check_credentials("admin", "network-test-password")
            .unwrap()
            .unwrap();
        let api = ProductApi::new(store.clone(), root.join("data"), root.join("imports"));
        let (url, calls, thread) = fixture("openai_responses", 2);
        let body = json!({"name":"Fixture","protocol":"openai_responses","base_url":url,"external":false,"api_key":"fixture-provider-secret","model_id":"fixture-model","acknowledge_probe":true,"install_builtin_skills":true});
        let query = BTreeMap::from([("__idempotency_key".into(), "same-operation".into())]);
        let first = api
            .request(
                &user,
                "POST",
                "/api/setup/connect",
                &query,
                &serde_json::to_vec(&body).unwrap(),
            )
            .unwrap();
        thread.join().unwrap();
        let second = api
            .request(
                &user,
                "POST",
                "/api/setup/connect",
                &query,
                &serde_json::to_vec(&body).unwrap(),
            )
            .unwrap();
        assert_eq!(first.body, second.body);
        assert_eq!(calls.load(Ordering::SeqCst), 2);
        assert_eq!(first.body["skills"].as_array().unwrap().len(), 3);
        let db = store.connect().unwrap();
        assert_eq!(
            db.query_row("SELECT count(*) FROM providers", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            1
        );
        assert_eq!(
            db.query_row(
                "SELECT count(*) FROM skill_revisions WHERE status='approved'",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
            3
        );
        db.execute("UPDATE steps SET state='failed'", []).unwrap();
        assert_eq!(
            api.request(
                &user,
                "POST",
                "/api/setup/connect",
                &query,
                &serde_json::to_vec(&body).unwrap()
            )
            .unwrap_err()
            .status,
            409
        );
        assert_eq!(calls.load(Ordering::SeqCst), 2);
        drop(api);
        drop(store);
        drop(db);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn discovery_does_not_invoke_models_and_blocks_unsafe_targets() {
        let (url, calls, thread) = fixture("openai_chat", 1);
        let connection =
            json!({"protocol":"openai_chat","base_url":url,"external":false,"protocol_config":{}});
        let models = discover(&connection, "").unwrap();
        assert_eq!(models[0]["id"], "fixture-model");
        thread.join().unwrap();
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        for url in [
            "http://169.254.169.254",
            "http://8.8.8.8",
            "http://0.0.0.0",
            "http://224.0.0.1",
        ] {
            assert!(client(
                &json!({"protocol":"openai_chat","base_url":url,"external":false}),
                ""
            )
            .is_err());
        }
        assert!(
            input_connection(&json!({"base_url":"http://example.com","external":true})).is_err()
        );
    }

    #[test]
    fn custom_catalog_requires_complete_envelopes_and_ignores_model_metadata() {
        for metadata in [
            json!({"has_more":true}),
            json!({"has_more":"false"}),
            json!({"nextCursor":"cursor"}),
            json!({"errors":["partial failure"]}),
        ] {
            let data = json!({"payload":{"items":[{"id":"model"}],"pagination":metadata}});
            assert!(complete_custom_catalog(&data, &json!("/payload/items")).is_err());
        }
        complete_custom_catalog(&json!({"payload":{"items":[{"id":"model","next":"model-metadata","error":"model-metadata"}],"pagination":{"has_more":false}}}),&json!("/payload/items")).unwrap();
        protocols::validate(&json!({"endpoint":"/invoke","body":{"model":"{{model}}","messages":"{{messages}}"},"response":{"text":"/text"}})).unwrap();
    }
}
