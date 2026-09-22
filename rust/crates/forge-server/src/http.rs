use crate::{
    ApprovalDecisionInput, Backend, BackendError, ProductStore, RunControl, RunDecisionInput, User,
};
use axum::{
    body::Body,
    extract::{
        rejection::JsonRejection, DefaultBodyLimit, OriginalUri, Path, Query, Request, State,
    },
    http::{header, HeaderMap, HeaderValue, Method, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, VecDeque},
    convert::Infallible,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use uuid::Uuid;

const SESSION_SECONDS: u64 = 43_200;

#[derive(Clone, Debug)]
pub struct ServerConfig {
    pub allowed_origins: Vec<String>,
    pub secure_cookie: bool,
    pub session_ttl: Duration,
    pub poll_interval: Duration,
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            allowed_origins: Vec::new(),
            secure_cookie: false,
            session_ttl: Duration::from_secs(SESSION_SECONDS),
            // Keep the SSE feed close to Codex's native delta cadence. The
            // stream remains replayable from the event sequence, so a short
            // poll interval does not duplicate or lose events.
            poll_interval: Duration::from_millis(200),
        }
    }
}

struct Session {
    user_id: String,
    expires: Instant,
}

struct Host {
    backend: Arc<dyn Backend>,
    config: ServerConfig,
    sessions: Mutex<HashMap<String, Session>>,
    product_sessions: Option<Arc<ProductStore>>,
    attempts: Mutex<HashMap<String, VecDeque<Instant>>>,
}

/// Build a host without launching a listener. All cloned routers share sessions.
/// The backend is a trusted embedding boundary; typed write routes delegate to
/// it and never expose raw kernel commands or worker claims.
pub fn router(backend: Arc<dyn Backend>, config: ServerConfig) -> Router {
    build_router(backend, config, None)
}

/// Persistent sessions share the product catalog across host restarts. The
/// in-memory router remains available for isolated adapters and tests.
pub fn router_with_product_store(
    backend: Arc<dyn Backend>,
    config: ServerConfig,
    product: Arc<ProductStore>,
) -> Router {
    build_router(backend, config, Some(product))
}

fn build_router(
    backend: Arc<dyn Backend>,
    mut config: ServerConfig,
    product_sessions: Option<Arc<ProductStore>>,
) -> Router {
    config.poll_interval = config.poll_interval.max(Duration::from_millis(1));
    config.session_ttl = config.session_ttl.min(Duration::from_secs(SESSION_SECONDS));
    let host = Arc::new(Host {
        backend,
        config,
        sessions: Mutex::new(HashMap::new()),
        product_sessions,
        attempts: Mutex::new(HashMap::new()),
    });
    Router::new()
        .route("/api/health", get(health))
        .route("/api/capabilities", get(capabilities))
        .route("/api/auth/login", post(login))
        .route("/api/auth/logout", post(logout))
        .route("/api/auth/me", get(me))
        .route("/api/runs", get(runs).post(create_run))
        .route("/api/runs/preview-config", post(preview_run))
        .route("/api/runs/{run_id}", get(run))
        .route("/api/runs/{run_id}/events", get(events))
        .route("/api/runs/{run_id}/stream", get(stream))
        .route("/api/runs/{run_id}/runtime-approvals", get(approvals))
        .route(
            "/api/runs/{run_id}/runtime-approvals/{approval_id}/decision",
            post(decide_approval),
        )
        .route("/api/runs/{run_id}/control", post(control_run))
        .route("/api/runs/{run_id}/decision/{phase}", post(decide_run))
        .fallback(product_request)
        .method_not_allowed_fallback(|| async {
            ApiError::message(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed")
        })
        .layer(DefaultBodyLimit::max(20 * 1024 * 1024))
        .layer(middleware::from_fn_with_state(host.clone(), guard))
        .with_state(host)
}

struct ApiError(StatusCode, Value);

impl ApiError {
    fn message(status: StatusCode, message: &str) -> Self {
        Self(status, json!({"detail": message}))
    }

    fn validation(location: &str, name: &str) -> Self {
        Self(
            StatusCode::UNPROCESSABLE_ENTITY,
            json!({"detail": [{
                "type": "value_error", "loc": [location, name], "msg": "Invalid value"
            }]}),
        )
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(self.1)).into_response()
    }
}

impl From<BackendError> for ApiError {
    fn from(error: BackendError) -> Self {
        match error {
            BackendError::NotFound => Self::message(StatusCode::NOT_FOUND, "运行不存在或无权访问"),
            BackendError::Forbidden => Self::message(StatusCode::FORBIDDEN, "只读账号不可修改"),
            BackendError::Conflict => {
                Self::message(StatusCode::CONFLICT, "操作对象已变化或当前状态不支持该操作")
            }
            BackendError::Unsupported => {
                Self::message(StatusCode::CONFLICT, "当前后端尚不支持该操作")
            }
            BackendError::UnsupportedConfiguration(detail) => {
                Self::message(StatusCode::CONFLICT, detail)
            }
            BackendError::InvalidData => Self::validation("body", "request"),
            // Internal database/adapter diagnostics must never cross HTTP.
            _ => Self::message(StatusCode::SERVICE_UNAVAILABLE, "服务暂时不可用"),
        }
    }
}

async fn guard(State(host): State<Arc<Host>>, request: Request, next: Next) -> Response {
    let mut denial = None;
    if !matches!(
        *request.method(),
        Method::GET | Method::HEAD | Method::OPTIONS
    ) {
        if request
            .headers()
            .get("x-forge-request")
            .and_then(|v| v.to_str().ok())
            != Some("1")
        {
            denial = Some(ApiError::message(StatusCode::FORBIDDEN, "缺少同源请求标识"));
        } else if let Some(origin) = request.headers().get(header::ORIGIN) {
            if !origin.to_str().ok().is_some_and(|origin| {
                host.config
                    .allowed_origins
                    .iter()
                    .any(|allowed| allowed == origin)
            }) {
                denial = Some(ApiError::message(StatusCode::FORBIDDEN, "请求来源未授权"));
            }
        }
    }
    let mut response = match denial {
        Some(error) => error.into_response(),
        None => next.run(request).await,
    };
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response.headers_mut().insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    response.headers_mut().insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("same-origin"),
    );
    response
}

fn cookie(headers: &HeaderMap) -> String {
    headers
        .get_all(header::COOKIE)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|value| value.split(';'))
        .find_map(|part| {
            let (name, value) = part.trim().split_once('=')?;
            (name == "forge_session").then(|| value.to_owned())
        })
        .unwrap_or_default()
}

fn token_hash(token: &str) -> String {
    format!("{:x}", Sha256::digest(token.as_bytes()))
}

fn current_user(host: &Host, token: &str) -> Result<User, ApiError> {
    if let Some(product) = &host.product_sessions {
        let user = product
            .session_user(token)?
            .ok_or_else(|| ApiError::message(StatusCode::UNAUTHORIZED, "请先登录"))?;
        return host
            .backend
            .user(&user.id)?
            .filter(|user| user.enabled)
            .ok_or_else(|| ApiError::message(StatusCode::UNAUTHORIZED, "账号已停用"));
    }
    let user_id = {
        let mut sessions = host
            .sessions
            .lock()
            .map_err(|_| BackendError::Unavailable)?;
        let key = token_hash(token);
        match sessions.get(&key) {
            Some(session) if session.expires > Instant::now() => session.user_id.clone(),
            _ => {
                sessions.remove(&key);
                return Err(ApiError::message(StatusCode::UNAUTHORIZED, "请先登录"));
            }
        }
    };
    host.backend
        .user(&user_id)?
        .filter(|user| user.enabled)
        .ok_or_else(|| ApiError::message(StatusCode::UNAUTHORIZED, "账号已停用"))
}

async fn blocking<T: Send + 'static>(
    operation: impl FnOnce() -> Result<T, ApiError> + Send + 'static,
) -> Result<T, ApiError> {
    tokio::task::spawn_blocking(operation)
        .await
        .map_err(|_| BackendError::Unavailable)?
}

async fn health() -> Json<Value> {
    Json(json!({"ok": true, "version": "0.14.0"}))
}

async fn create_run(
    State(host): State<Arc<Host>>,
    headers: HeaderMap,
    input: Result<Json<Value>, JsonRejection>,
) -> Result<Response, ApiError> {
    let Json(input) = input.map_err(|_| ApiError::validation("body", "run"))?;
    let token = cookie(&headers);
    let key = headers
        .get("idempotency-key")
        .and_then(|v| v.to_str().ok())
        .map(str::to_owned);
    if key.as_ref().is_some_and(|s| s.is_empty() || s.len() > 200) {
        return Err(ApiError::validation("header", "idempotency-key"));
    }
    blocking(move || {
        let user = current_user(&host, &token)?;
        Ok((
            StatusCode::ACCEPTED,
            Json(host.backend.create_run(&user, input, key)?),
        )
            .into_response())
    })
    .await
}

async fn preview_run(
    State(host): State<Arc<Host>>,
    headers: HeaderMap,
    input: Result<Json<Value>, JsonRejection>,
) -> Result<Json<Value>, ApiError> {
    let Json(input) = input.map_err(|_| ApiError::validation("body", "run"))?;
    let token = cookie(&headers);
    blocking(move || {
        let user = current_user(&host, &token)?;
        Ok(Json(host.backend.preview_run(&user, input)?))
    })
    .await
}

async fn product_request(
    State(host): State<Arc<Host>>,
    OriginalUri(uri): OriginalUri,
    method: Method,
    headers: HeaderMap,
    body: axum::body::Bytes,
) -> Result<Response, ApiError> {
    let token = cookie(&headers);
    let mut query: std::collections::BTreeMap<String, String> =
        url::form_urlencoded::parse(uri.query().unwrap_or("").as_bytes())
            .map(|(k, v)| (k.into_owned(), v.into_owned()))
            .collect();
    query.remove("__idempotency_key");
    if let Some(key) = headers.get("idempotency-key").and_then(|v| v.to_str().ok()) {
        if key.is_empty() || key.len() > 200 {
            return Err(ApiError::validation("header", "idempotency-key"));
        }
        query.insert("__idempotency_key".into(), key.into());
    }
    blocking(move || {
        let user = current_user(&host, &token)?;
        let reply = host
            .backend
            .product_request(&user, method.as_str(), uri.path(), &query, &body)
            .map_err(|e| {
                ApiError::message(
                    StatusCode::from_u16(e.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
                    &e.detail,
                )
            })?;
        let status =
            StatusCode::from_u16(reply.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
        if let Some(download) = reply.download {
            let filename: String = download
                .filename
                .chars()
                .filter(|c| c.is_ascii_alphanumeric() || "._-".contains(*c))
                .collect();
            let disposition =
                HeaderValue::from_str(&format!("attachment; filename=\"{filename}\""))
                    .map_err(|_| BackendError::InvalidData)?;
            Ok((
                status,
                [
                    (
                        header::CONTENT_TYPE,
                        HeaderValue::from_static(download.content_type),
                    ),
                    (header::CONTENT_DISPOSITION, disposition),
                ],
                download.bytes,
            )
                .into_response())
        } else {
            Ok((status, Json(reply.body)).into_response())
        }
    })
    .await
}

/// Describes the deliberately small surface exposed by this compatibility
/// host.  Desktop launchers can use this before selecting a backend and avoid
/// sending write requests to a read-only migration adapter.  Keep this
/// response additive and machine-readable; it is not an authorization check.
async fn capabilities(State(host): State<Arc<Host>>) -> Json<Value> {
    Json(host.backend.capabilities())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Login {
    username: String,
    password: String,
}

async fn login(
    State(host): State<Arc<Host>>,
    input: Result<Json<Login>, JsonRejection>,
) -> Result<Response, ApiError> {
    let Json(input) = input.map_err(|_| ApiError::validation("body", "login"))?;
    if input.username.is_empty() || input.username.len() > 256 || input.password.len() > 4096 {
        return Err(ApiError::validation("body", "login"));
    }
    blocking(move || {
        {
            let now = Instant::now();
            let mut attempts = host
                .attempts
                .lock()
                .map_err(|_| BackendError::Unavailable)?;
            attempts.retain(|_, queue| {
                queue.retain(|instant| now.duration_since(*instant) < Duration::from_secs(300));
                !queue.is_empty()
            });
            // Bound attacker-controlled usernames as well as per-account work.
            if attempts.len() >= 4096 && !attempts.contains_key(&input.username) {
                return Err(ApiError::message(
                    StatusCode::TOO_MANY_REQUESTS,
                    "尝试次数过多，请稍后再试",
                ));
            }
            let queue = attempts.entry(input.username.clone()).or_default();
            if queue.len() >= 10 {
                return Err(ApiError::message(
                    StatusCode::TOO_MANY_REQUESTS,
                    "尝试次数过多，请稍后再试",
                ));
            }
            queue.push_back(now);
        }
        let user = host
            .backend
            .check_credentials(&input.username, &input.password)?
            .filter(|user| user.enabled)
            .ok_or_else(|| ApiError::message(StatusCode::UNAUTHORIZED, "用户名或密码错误"))?;
        host.attempts
            .lock()
            .map_err(|_| BackendError::Unavailable)?
            .remove(&input.username);
        let token = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
        if let Some(product) = &host.product_sessions {
            product.create_session(&user.id, &token, host.config.session_ttl)?;
        } else {
            let now = Instant::now();
            let mut sessions = host
                .sessions
                .lock()
                .map_err(|_| BackendError::Unavailable)?;
            sessions.retain(|_, session| session.expires > now);
            if sessions.len() >= 10_000 {
                return Err(BackendError::Unavailable.into());
            }
            sessions.insert(
                token_hash(&token),
                Session {
                    user_id: user.id.clone(),
                    expires: now + host.config.session_ttl,
                },
            );
        }
        let secure = if host.config.secure_cookie {
            "; Secure"
        } else {
            ""
        };
        let ttl = host.config.session_ttl.as_secs();
        let cookie = format!(
            "forge_session={token}; HttpOnly; SameSite=strict; Path=/; Max-Age={ttl}{secure}"
        );
        let mut response = Json(user).into_response();
        response.headers_mut().insert(
            header::SET_COOKIE,
            HeaderValue::from_str(&cookie).map_err(|_| BackendError::Unavailable)?,
        );
        Ok(response)
    })
    .await
}

async fn logout(State(host): State<Arc<Host>>, headers: HeaderMap) -> Result<Response, ApiError> {
    let token = cookie(&headers);
    blocking(move || {
        current_user(&host, &token)?;
        if let Some(product) = &host.product_sessions {
            product.revoke_session(&token)?;
        } else {
            host.sessions
                .lock()
                .map_err(|_| BackendError::Unavailable)?
                .remove(&token_hash(&token));
        }
        let mut response = Json(json!({"ok": true})).into_response();
        response.headers_mut().insert(
            header::SET_COOKIE,
            HeaderValue::from_static(
                "forge_session=; Path=/; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT",
            ),
        );
        Ok(response)
    })
    .await
}

async fn me(State(host): State<Arc<Host>>, headers: HeaderMap) -> Result<Json<User>, ApiError> {
    let token = cookie(&headers);
    blocking(move || current_user(&host, &token).map(Json)).await
}

type Parameters = Result<Query<HashMap<String, String>>, axum::extract::rejection::QueryRejection>;

fn parameters(query: Parameters) -> Result<HashMap<String, String>, ApiError> {
    query
        .map(|Query(params)| params)
        .map_err(|_| ApiError::validation("query", "query"))
}

fn number(
    params: &HashMap<String, String>,
    name: &str,
    default: i64,
    min: i64,
    max: i64,
) -> Result<i64, ApiError> {
    let value = match params.get(name) {
        Some(value) => value
            .parse::<i64>()
            .map_err(|_| ApiError::validation("query", name))?,
        None => default,
    };
    if !(min..=max).contains(&value) {
        return Err(ApiError::validation("query", name));
    }
    Ok(value)
}

async fn runs(
    State(host): State<Arc<Host>>,
    headers: HeaderMap,
    query: Parameters,
) -> Result<Json<Vec<Value>>, ApiError> {
    let limit = number(&parameters(query)?, "limit", 100, 1, 500)? as usize;
    let token = cookie(&headers);
    blocking(move || {
        let user = current_user(&host, &token)?;
        Ok(Json(host.backend.runs(&user, limit)?))
    })
    .await
}

async fn run(
    State(host): State<Arc<Host>>,
    headers: HeaderMap,
    Path(run_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let token = cookie(&headers);
    blocking(move || {
        let user = current_user(&host, &token)?;
        Ok(Json(host.backend.run(&user, &run_id)?))
    })
    .await
}

async fn events(
    State(host): State<Arc<Host>>,
    headers: HeaderMap,
    Path(run_id): Path<String>,
    query: Parameters,
) -> Result<Json<crate::EventPage>, ApiError> {
    let params = parameters(query)?;
    let after = number(&params, "after", 0, 0, i64::MAX)?;
    let limit = number(&params, "limit", 200, 1, 1000)? as usize;
    let token = cookie(&headers);
    blocking(move || {
        let user = current_user(&host, &token)?;
        Ok(Json(host.backend.events(&user, &run_id, after, limit)?))
    })
    .await
}

async fn approvals(
    State(host): State<Arc<Host>>,
    headers: HeaderMap,
    Path(run_id): Path<String>,
    query: Parameters,
) -> Result<Json<Value>, ApiError> {
    let limit = number(&parameters(query)?, "limit", 100, 1, 500)? as usize;
    let token = cookie(&headers);
    blocking(move || {
        let user = current_user(&host, &token)?;
        Ok(Json(host.backend.approvals(&user, &run_id, limit)?))
    })
    .await
}

async fn decide_approval(
    State(host): State<Arc<Host>>,
    headers: HeaderMap,
    Path((run_id, approval_id)): Path<(String, String)>,
    input: Result<Json<ApprovalDecisionInput>, JsonRejection>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let Json(input) = input.map_err(|_| ApiError::validation("body", "decision"))?;
    if input.digest.len() != 64 || input.comment.chars().count() > 4000 {
        return Err(ApiError::validation("body", "decision"));
    }
    let token = cookie(&headers);
    blocking(move || {
        let user = current_user(&host, &token)?;
        let value = host
            .backend
            .decide_approval(&user, &run_id, &approval_id, input)?;
        Ok((StatusCode::ACCEPTED, Json(value)))
    })
    .await
}

async fn control_run(
    State(host): State<Arc<Host>>,
    headers: HeaderMap,
    Path(run_id): Path<String>,
    input: Result<Json<RunControl>, JsonRejection>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let Json(input) = input.map_err(|_| ApiError::validation("body", "control"))?;
    if input.reason.chars().count() > 4000
        || input
            .prompt
            .as_ref()
            .is_some_and(|p| p.is_empty() || p.chars().count() > 100_000)
        || input
            .task_key
            .as_ref()
            .is_some_and(|p| p.is_empty() || p.chars().count() > 64)
        || input
            .agent_id
            .as_ref()
            .is_some_and(|p| p.is_empty() || p.chars().count() > 32)
    {
        return Err(ApiError::validation("body", "control"));
    }
    let token = cookie(&headers);
    blocking(move || {
        let user = current_user(&host, &token)?;
        let value = host.backend.control_run(&user, &run_id, input)?;
        Ok((StatusCode::ACCEPTED, Json(value)))
    })
    .await
}

async fn decide_run(
    State(host): State<Arc<Host>>,
    headers: HeaderMap,
    Path((run_id, phase)): Path<(String, String)>,
    input: Result<Json<RunDecisionInput>, JsonRejection>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let Json(input) = input.map_err(|_| ApiError::validation("body", "decision"))?;
    if input.digest.len() != 64 || input.note.chars().count() > 4000 {
        return Err(ApiError::validation("body", "decision"));
    }
    let token = cookie(&headers);
    blocking(move || {
        let user = current_user(&host, &token)?;
        let value = host.backend.decide_run(&user, &run_id, &phase, input)?;
        Ok((StatusCode::ACCEPTED, Json(value)))
    })
    .await
}

async fn stream(
    State(host): State<Arc<Host>>,
    headers: HeaderMap,
    Path(run_id): Path<String>,
    query: Parameters,
) -> Result<Response, ApiError> {
    let after = number(&parameters(query)?, "after", 0, 0, i64::MAX)?;
    let last = headers
        .get("last-event-id")
        .and_then(|value| value.to_str().ok())
        .filter(|value| !value.is_empty() && value.bytes().all(|byte| byte.is_ascii_digit()))
        .and_then(|value| value.parse::<i64>().ok())
        .unwrap_or(0);
    let mut cursor = after.max(last);
    let token = cookie(&headers);
    let initial_host = host.clone();
    let initial_token = token.clone();
    let initial_run = run_id.clone();
    blocking(move || {
        let user = current_user(&initial_host, &initial_token)?;
        initial_host.backend.run(&user, &initial_run)?;
        Ok(())
    })
    .await?;
    // Subscribe before the first read: a commit racing that read remains a
    // pending wake-up. The durable cursor, not a bounded channel, owns replay.
    let mut notifications = host.backend.event_notifications();
    let body = async_stream::stream! {
        loop {
            let live_host = host.clone();
            let live_token = token.clone();
            let live_run = run_id.clone();
            let result = blocking(move || {
                let user = current_user(&live_host, &live_token)?;
                Ok(live_host.backend.events(&user, &live_run, cursor, 200)?)
            }).await;
            match result {
                Ok(page) => {
                    if page.events.is_empty() {
                        yield Ok::<_, Infallible>(": keepalive\n\n".to_owned());
                    }
                    let has_more = page.has_more;
                    for event in page.events {
                        cursor = event.id;
                        let data = serde_json::to_string(&event).expect("event contains JSON values");
                        yield Ok(format!("id: {}\ndata: {data}\n\n", event.id));
                    }
                    if has_more { continue; }
                }
                Err(error) => {
                    if matches!(error.0, StatusCode::UNAUTHORIZED | StatusCode::NOT_FOUND | StatusCode::FORBIDDEN) {
                        yield Ok("event: revoked\ndata: {}\n\n".to_owned());
                    } else {
                        yield Ok("event: error\ndata: {}\n\n".to_owned());
                    }
                    break;
                }
            }
            if let Some(receiver) = notifications.as_mut() {
                // Periodic ACL/state reconciliation remains for writes from
                // other processes and non-runtime commands; token deltas wake
                // immediately after their durable journal commit.
                tokio::select! {
                    result = receiver.changed() => { if result.is_err() { notifications = None; } },
                    _ = tokio::time::sleep(Duration::from_secs(1)) => {},
                }
            } else {
                tokio::time::sleep(host.config.poll_interval).await;
            }
        }
    };
    let mut response = Body::from_stream(body).into_response();
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/event-stream; charset=utf-8"),
    );
    response
        .headers_mut()
        .insert("x-accel-buffering", HeaderValue::from_static("no"));
    Ok(response)
}
