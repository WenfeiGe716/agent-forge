//! Native desktop authority is received only over the parent-owned stdin pipe.

use crate::{Backend, BackendError, ProductStore};
use axum::{
    body::Body,
    extract::{Request, State},
    http::{header, HeaderMap, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    fs,
    path::{Component, Path, PathBuf},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use subtle::ConstantTimeEq;
use uuid::Uuid;

fn hash(value: &str) -> String {
    format!("{:x}", Sha256::digest(value.as_bytes()))
}
fn denial(status: StatusCode, detail: &str) -> Response {
    (status, Json(json!({"detail": detail}))).into_response()
}

#[derive(Clone)]
pub struct DirectoryGrant {
    pub path: PathBuf,
    pub identity: (u64, u64),
    expires: Instant,
}

pub struct DirectoryTickets {
    protected: Vec<PathBuf>,
    grants: Mutex<HashMap<String, DirectoryGrant>>,
}

impl DirectoryTickets {
    pub fn new(protected: Vec<PathBuf>) -> Self {
        Self {
            protected,
            grants: Mutex::new(HashMap::new()),
        }
    }

    pub fn issue(&self, value: &str) -> Result<Value, BackendError> {
        let raw = Path::new(value);
        if value.len() > 4096
            || value.contains(['\0', '\\'])
            || !raw.is_absolute()
            || raw.components().any(|c| matches!(c, Component::ParentDir))
        {
            return Err(BackendError::InvalidData);
        }
        let selected = fs::symlink_metadata(raw).map_err(|_| BackendError::InvalidData)?;
        if !selected.is_dir() || selected.file_type().is_symlink() {
            return Err(BackendError::InvalidData);
        }
        let source = raw.canonicalize().map_err(|_| BackendError::InvalidData)?;
        let current = fs::metadata(&source).map_err(|_| BackendError::InvalidData)?;
        if identity(&selected) != identity(&current)
            || source.parent().is_none()
            || self
                .protected
                .iter()
                .any(|p| source.starts_with(p) || p.starts_with(&source))
            || std::env::var_os("HOME")
                .map(PathBuf::from)
                .and_then(|p| p.canonicalize().ok())
                .is_some_and(|home| home.starts_with(&source))
        {
            return Err(BackendError::InvalidData);
        }
        let token = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
        let now = Instant::now();
        let mut grants = self.grants.lock().map_err(|_| BackendError::Unavailable)?;
        grants.retain(|_, g| g.expires > now);
        if grants.len() >= 32 {
            return Err(BackendError::Conflict);
        }
        grants.insert(
            hash(&token),
            DirectoryGrant {
                path: source.clone(),
                identity: identity(&current),
                expires: now + Duration::from_secs(120),
            },
        );
        Ok(
            json!({"ticket": token, "path": source, "name": source.file_name().and_then(|n| n.to_str()).unwrap_or("")}),
        )
    }

    pub fn consume(&self, token: &str) -> Result<DirectoryGrant, BackendError> {
        let grant = self
            .grants
            .lock()
            .map_err(|_| BackendError::Unavailable)?
            .remove(&hash(token))
            .ok_or(BackendError::Conflict)?;
        if grant.expires <= Instant::now() {
            return Err(BackendError::Conflict);
        }
        let current = fs::symlink_metadata(&grant.path).map_err(|_| BackendError::Conflict)?;
        if !current.is_dir()
            || current.file_type().is_symlink()
            || identity(&current) != grant.identity
        {
            return Err(BackendError::Conflict);
        }
        Ok(grant)
    }
}

#[cfg(unix)]
fn identity(metadata: &fs::Metadata) -> (u64, u64) {
    use std::os::unix::fs::MetadataExt;
    (metadata.dev(), metadata.ino())
}

#[cfg(not(unix))]
fn identity(_: &fs::Metadata) -> (u64, u64) {
    (0, 0)
}

pub struct DesktopConfig {
    pub desktop_token: String,
    pub control_token: String,
    pub origin: String,
    pub product: Arc<ProductStore>,
    pub backend: Arc<dyn Backend>,
    pub tickets: Arc<DirectoryTickets>,
}

/// Wrap after assembling all API and static routes, so even health and assets
/// require the per-process renderer token.
pub fn desktop_router(app: Router, config: DesktopConfig) -> Router {
    let state = Arc::new(config);
    let native = Router::new()
        .route("/api/desktop/session", post(session))
        .route("/api/desktop/status", get(status))
        .route(
            "/api/desktop/authorize-directory",
            post(authorize_directory),
        )
        .with_state(state.clone());
    app.merge(native)
        .layer(middleware::from_fn_with_state(state, guard))
}

fn matches(headers: &HeaderMap, name: &str, value: &str) -> bool {
    let mut values = headers.get_all(name).iter();
    let candidate = values.next();
    values.next().is_none()
        && !value.is_empty()
        && candidate.is_some_and(|v| bool::from(v.as_bytes().ct_eq(value.as_bytes())))
}

fn control(config: &DesktopConfig, headers: &HeaderMap) -> Result<(), &'static str> {
    if headers.contains_key(header::ORIGIN)
        || !matches(headers, "x-forge-desktop-control", &config.control_token)
        || !matches(headers, "x-forge-desktop", &config.desktop_token)
    {
        return Err("需要原生桌面控制权限");
    }
    Ok(())
}

async fn guard(State(config): State<Arc<DesktopConfig>>, request: Request, next: Next) -> Response {
    if !matches(request.headers(), "x-forge-desktop", &config.desktop_token) {
        return denial(StatusCode::FORBIDDEN, "桌面请求未授权");
    }
    if request
        .headers()
        .get(header::ORIGIN)
        .is_some_and(|v| v.as_bytes() != config.origin.as_bytes())
    {
        return denial(StatusCode::FORBIDDEN, "桌面请求来源未授权");
    }
    let health =
        request.uri().path() == "/api/health" && request.method() == axum::http::Method::GET;
    let mut response = next.run(request).await;
    if health && response.status().is_success() {
        *response.body_mut() = Body::from(json!({"ok": true, "version": "0.17.0", "desktop_instance": hash(&config.control_token), "backend": "rust"}).to_string());
        response.headers_mut().remove(header::CONTENT_LENGTH);
    }
    response
}

async fn session(State(config): State<Arc<DesktopConfig>>, headers: HeaderMap) -> Response {
    if let Err(error) = control(&config, &headers) {
        return denial(StatusCode::FORBIDDEN, error);
    }
    if !matches(&headers, "x-forge-request", "1") {
        return denial(StatusCode::FORBIDDEN, "缺少同源请求标识");
    }
    let result = tokio::task::spawn_blocking(move || -> Result<_, BackendError> {
        let db = config.product.connect()?;
        let user_id: String = db.query_row("SELECT id FROM users WHERE enabled=1 AND role='admin' ORDER BY username,id LIMIT 1", [], |r| r.get(0)).map_err(|_| BackendError::Forbidden)?;
        let user = config.product.user(&user_id)?.ok_or(BackendError::Forbidden)?;
        let token = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
        config.product.create_session(&user.id, &token, Duration::from_secs(43_200))?;
        Ok((user, token))
    }).await;
    match result {
        Ok(Ok((user, token))) => (
            [(
                header::SET_COOKIE,
                format!("forge_session={token}; HttpOnly; SameSite=strict; Path=/; Max-Age=43200"),
            )],
            Json(user),
        )
            .into_response(),
        _ => denial(StatusCode::FORBIDDEN, "没有可用的桌面管理员账号"),
    }
}

async fn status(State(config): State<Arc<DesktopConfig>>, headers: HeaderMap) -> Response {
    if let Err(error) = control(&config, &headers) {
        return denial(StatusCode::FORBIDDEN, error);
    }
    let result = tokio::task::spawn_blocking(move || -> Result<Value, BackendError> {
        let db = config.product.connect()?;
        let user_id: String = db.query_row("SELECT id FROM users WHERE enabled=1 AND role='admin' ORDER BY username,id LIMIT 1", [], |r| r.get(0)).map_err(|_| BackendError::Forbidden)?;
        let user = config.product.user(&user_id)?.ok_or(BackendError::Forbidden)?;
        let mut counts: HashMap<String, usize> = HashMap::new();
        for state in config.backend.run_statuses(&user)?.into_values() {
            if !matches!(state.as_str(), "completed" | "cancelled" | "failed") { *counts.entry(state).or_default() += 1; }
        }
        Ok(json!({"active_runs": counts.values().sum::<usize>(), "nonterminal_runs": counts, "dispatcher": "rust", "version": "0.17.0"}))
    }).await;
    match result {
        Ok(Ok(value)) => Json(value).into_response(),
        _ => denial(StatusCode::SERVICE_UNAVAILABLE, "无法确认运行状态"),
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DirectoryInput {
    path: String,
}

async fn authorize_directory(
    State(config): State<Arc<DesktopConfig>>,
    headers: HeaderMap,
    input: Result<Json<DirectoryInput>, axum::extract::rejection::JsonRejection>,
) -> Response {
    if let Err(error) = control(&config, &headers) {
        return denial(StatusCode::FORBIDDEN, error);
    }
    if !matches(&headers, "x-forge-request", "1") {
        return denial(StatusCode::FORBIDDEN, "缺少同源请求标识");
    }
    let Ok(Json(input)) = input else {
        return denial(StatusCode::UNPROCESSABLE_ENTITY, "目录参数无效");
    };
    match config.tickets.issue(&input.path) {
        Ok(value) => Json(value).into_response(),
        Err(_) => denial(
            StatusCode::UNPROCESSABLE_ENTITY,
            "请选择具体项目目录，不能导入主目录、平台数据或符号链接",
        ),
    }
}

/// The existing UI is served from a fixed build directory. API misses retain
/// their JSON errors; static paths cannot escape through URL or symlink aliases.
pub fn with_frontend(app: Router, root: PathBuf) -> Router {
    app.layer(middleware::from_fn(move |request: Request, next: Next| {
        let root = root.clone();
        async move {
            if request.uri().path().starts_with("/api/") {
                return next.run(request).await;
            }
            if !matches!(
                *request.method(),
                axum::http::Method::GET | axum::http::Method::HEAD
            ) {
                return denial(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed");
            }
            let relative = request.uri().path().trim_start_matches('/');
            let relative = if relative.is_empty() {
                "index.html"
            } else {
                relative
            };
            if relative.contains(['%', '\\'])
                || Path::new(relative)
                    .components()
                    .any(|c| !matches!(c, Component::Normal(_)))
            {
                return denial(StatusCode::NOT_FOUND, "Not Found");
            }
            let path = root.join(relative);
            let Ok(resolved) = path.canonicalize() else {
                return denial(StatusCode::NOT_FOUND, "Not Found");
            };
            let Ok(canonical_root) = root.canonicalize() else {
                return denial(StatusCode::NOT_FOUND, "Not Found");
            };
            if !resolved.starts_with(&canonical_root) {
                return denial(StatusCode::NOT_FOUND, "Not Found");
            }
            let Ok(data) = fs::read(&resolved) else {
                return denial(StatusCode::NOT_FOUND, "Not Found");
            };
            let mime = match resolved.extension().and_then(|e| e.to_str()) {
                Some("html") => "text/html; charset=utf-8",
                Some("js") => "application/javascript",
                Some("css") => "text/css",
                Some("png") => "image/png",
                Some("svg") => "image/svg+xml",
                _ => "application/octet-stream",
            };
            (
                [
                    (header::CONTENT_TYPE, mime),
                    (header::CACHE_CONTROL, "no-store"),
                    (header::X_CONTENT_TYPE_OPTIONS, "nosniff"),
                ],
                if request.method() == axum::http::Method::HEAD {
                    Vec::new()
                } else {
                    data
                },
            )
                .into_response()
        }
    }))
}
