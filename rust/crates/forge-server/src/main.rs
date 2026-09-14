use forge_server::{
    desktop::{desktop_router, with_frontend, DesktopConfig, DirectoryTickets},
    desktop_launch, router, router_with_product_store, Backend, MemoryBackend, ProductStore,
    PythonSqliteBackend, RustOwnedBackend, ServerConfig, User,
};
use std::{
    net::SocketAddr,
    path::{Path, PathBuf},
    sync::Arc,
};

#[derive(Default)]
struct Options {
    address: Option<SocketAddr>,
    python_db: Option<PathBuf>,
    rust_db: Option<PathBuf>,
    product_db: Option<PathBuf>,
    import_db: Option<PathBuf>,
    data_root: Option<PathBuf>,
    import_root: Option<PathBuf>,
    frontend: Option<PathBuf>,
    allow_network: bool,
    migrate_only: bool,
}

fn options(args: impl IntoIterator<Item = std::ffi::OsString>) -> Result<Options, &'static str> {
    let mut args = args.into_iter();
    let mut result = Options::default();
    while let Some(flag) = args.next() {
        if flag == "--allow-network" {
            result.allow_network = true;
            continue;
        }
        if flag == "--migrate-only" {
            result.migrate_only = true;
            continue;
        }
        let value = args
            .next()
            .filter(|v| !v.is_empty())
            .ok_or("option requires a value")?;
        match flag.to_str() {
            Some("--listen") if result.address.is_none()=>result.address=Some(value.to_str().and_then(|v|v.parse().ok()).ok_or("invalid listen address")?),
            Some("--python-db") if result.python_db.is_none()=>result.python_db=Some(value.into()),
            Some("--rust-db") if result.rust_db.is_none()=>result.rust_db=Some(value.into()),
            Some("--product-db") if result.product_db.is_none()=>result.product_db=Some(value.into()),
            Some("--import-db") if result.import_db.is_none()=>result.import_db=Some(value.into()),
            Some("--data-root") if result.data_root.is_none()=>result.data_root=Some(value.into()),
            Some("--import-root") if result.import_root.is_none()=>result.import_root=Some(value.into()),
            Some("--frontend") if result.frontend.is_none()=>result.frontend=Some(value.into()),
            _=>return Err("usage: forge-server [--listen ADDRESS] [--product-db PATH --rust-db PATH --data-root PATH --import-root PATH] [--import-db LEGACY_SQLITE] [--frontend PATH] [--allow-network]; --desktop reads native launch input"),
        }
    }
    if result.import_db.is_some() && result.product_db.is_none() {
        return Err("--import-db requires --product-db");
    }
    if result.migrate_only && result.product_db.as_ref().is_some_and(|p| p.exists()) {
        return Err(
            "migration target already exists; inspect the current catalog instead of replacing it",
        );
    }
    if result.python_db.is_some() && result.product_db.is_some() {
        return Err("select a Rust product store or a read-only legacy adapter");
    }
    if result.rust_db.is_some() && result.python_db.is_none() && result.product_db.is_none() {
        return Err("--rust-db requires product authorization");
    }
    Ok(result)
}

#[tokio::main]
async fn main() {
    #[cfg(unix)]
    unsafe {
        libc::umask(0o077);
    }
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    let result = if args == [std::ffi::OsString::from("--desktop")] {
        desktop().await
    } else {
        match options(args) {
            Ok(opts) => serve(opts, None).await,
            Err(e) => Err(e),
        }
    };
    if let Err(message) = result {
        eprintln!("{message}");
        std::process::exit(1);
    }
}

struct DesktopLaunch {
    token: String,
    control: String,
    root: PathBuf,
    parent: u32,
}

async fn desktop() -> Result<(), &'static str> {
    let input = desktop_launch::read_input()?;
    if input.action == "prepare" {
        match desktop_launch::prepare(&input) {
            Ok(value) => {
                println!("{value}");
                return Ok(());
            }
            Err(error) => {
                println!("{}", serde_json::json!({"ok":false,"error":error}));
                return Err("desktop preparation failed");
            }
        }
    }
    if input.action != "serve" {
        return Err("unknown desktop launch action");
    }
    let (root, secrets) = desktop_launch::serve_data(&input)?;
    for key in ["ENCRYPTION_KEY", "ADMIN_PASSWORD", "SANDBOX_TOKEN"] {
        std::env::set_var(key, secrets[key].as_str().ok_or("missing desktop secret")?);
    }
    let executable = std::env::current_exe().map_err(|_| "cannot locate executable")?;
    let resources = executable
        .parent()
        .ok_or("cannot locate bundled resources")?;
    let frontend = if resources.join("frontend/index.html").is_file() {
        resources.join("frontend")
    } else {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../frontend/dist-desktop")
    };
    if !frontend.join("index.html").is_file() {
        return Err("desktop frontend resources are missing; build the application first");
    }
    for (key, name) in [
        ("FORGE_CODEX_BINARY", "codex"),
        ("FORGE_OPENCODEX_BINARY", "opencodex"),
        ("FORGE_BUN_BINARY", "bun"),
    ] {
        let binary = resources.join(name);
        if binary.is_file() {
            std::env::set_var(key, binary);
        }
    }
    if resources
        .join("gateway/runtime/opencodex-gateway.ts")
        .is_file()
    {
        std::env::set_var(
            "FORGE_OPENCODEX_ENTRY",
            resources.join("gateway/runtime/opencodex-gateway.ts"),
        );
        std::env::set_var(
            "FORGE_GATEWAY_SOURCE_ROOT",
            resources.join("gateway/source"),
        );
    }
    let legacy = root.join("forge.db");
    let options = Options {
        address: Some(SocketAddr::from(([127, 0, 0, 1], input.port.unwrap()))),
        product_db: Some(root.join("product.sqlite3")),
        rust_db: Some(root.join("runs.sqlite3")),
        import_db: legacy.is_file().then_some(legacy),
        data_root: Some(root.join("data")),
        import_root: Some(root.join("imports")),
        frontend: Some(frontend),
        ..Options::default()
    };
    #[cfg(unix)]
    let parent = unsafe { libc::getppid() } as u32;
    #[cfg(not(unix))]
    let parent = 0;
    serve(
        options,
        Some(DesktopLaunch {
            token: input.desktop_token.unwrap(),
            control: input.control_token.unwrap(),
            root,
            parent,
        }),
    )
    .await
}

async fn serve(options: Options, native: Option<DesktopLaunch>) -> Result<(), &'static str> {
    let tickets = native
        .as_ref()
        .map(|n| Arc::new(DirectoryTickets::new(vec![n.root.clone()])));
    let address = options
        .address
        .unwrap_or_else(|| "127.0.0.1:8787".parse().unwrap());
    if !address.ip().is_loopback() && !options.allow_network {
        return Err("non-loopback listeners require --allow-network");
    }
    let product = if let Some(path) = options.product_db {
        Some(Arc::new(
            ProductStore::open(
                path,
                options.import_db.as_deref(),
                std::env::var("ADMIN_PASSWORD").ok().as_deref(),
            )
            .map_err(|_| {
                "cannot open Rust product database; legacy sources are never overwritten"
            })?,
        ))
    } else {
        None
    };
    if options.migrate_only {
        if product.is_none() || options.import_db.is_none() {
            return Err("--migrate-only requires --product-db and --import-db");
        }
        println!(
            "{}",
            serde_json::json!({"ok":true,"backend":"rust","source_preserved":true})
        );
        return Ok(());
    }
    let (backend, label): (Arc<dyn Backend>, &str) = if let Some(product) = &product {
        let rust_db = options
            .rust_db
            .unwrap_or_else(|| product.path().with_file_name("runs.sqlite3"));
        let data = options.data_root.unwrap_or_else(|| {
            product
                .path()
                .parent()
                .unwrap_or(Path::new("."))
                .join("data")
        });
        let imports = options.import_root.unwrap_or_else(|| {
            product
                .path()
                .parent()
                .unwrap_or(Path::new("."))
                .join("imports")
        });
        desktop_launch::private_dir(&data)?;
        if !imports.exists() {
            desktop_launch::private_dir(&imports)?;
        }
        std::env::set_var("IMPORT_ROOT", imports);
        let codex = std::env::var_os("FORGE_CODEX_BINARY")
            .map(PathBuf::from)
            .or_else(|| {
                let p = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                    .join("../../../.local/runtimes/codex/bin/codex");
                p.is_file().then_some(p)
            });
        let mut backend = RustOwnedBackend::from_product(product.clone(), rust_db, data, codex)
            .map_err(|_| "cannot open Rust Run service")?;
        if let Some(tickets) = &tickets {
            backend = backend.with_directories(tickets.clone());
        }
        (Arc::new(backend), "Rust product and run stores")
    } else if let Some(path) = options.python_db {
        if let Some(rust) = options.rust_db {
            (
                Arc::new(
                    RustOwnedBackend::open(path, rust)
                        .map_err(|_| "cannot open Rust run adapter")?,
                ),
                "Rust runs with read-only legacy assets",
            )
        } else {
            (
                Arc::new(
                    PythonSqliteBackend::open(path)
                        .map_err(|_| "cannot open compatible Python product database read-only")?,
                ),
                "read-only Python database; Rust-local sessions",
            )
        }
    } else {
        let password = std::env::var("FORGE_SERVER_ADMIN_PASSWORD")
            .ok()
            .filter(|v| !v.is_empty())
            .ok_or("set FORGE_SERVER_ADMIN_PASSWORD or select --product-db")?;
        let memory = MemoryBackend::new();
        memory
            .add_user(
                User {
                    id: "admin".into(),
                    username: "admin".into(),
                    role: "admin".into(),
                    enabled: true,
                },
                &password,
            )
            .map_err(|_| "cannot initialize memory backend")?;
        (Arc::new(memory), "disposable memory backend")
    };
    let listener = tokio::net::TcpListener::bind(address)
        .await
        .map_err(|_| "cannot bind listener")?;
    let address = listener
        .local_addr()
        .map_err(|_| "cannot inspect listener")?;
    let mut origins: Vec<String> = std::env::var("ALLOWED_ORIGINS")
        .unwrap_or_default()
        .split(',')
        .filter(|v| !v.is_empty())
        .map(str::to_owned)
        .collect();
    origins.push(format!("http://{address}"));
    let config = ServerConfig {
        allowed_origins: origins,
        secure_cookie: std::env::var("SECURE_COOKIE").is_ok_and(|v| v == "true"),
        ..ServerConfig::default()
    };
    let mut app = if let Some(product) = &product {
        router_with_product_store(backend.clone(), config, product.clone())
    } else {
        router(backend.clone(), config)
    };
    if let Some(frontend) = options.frontend {
        app = with_frontend(app, frontend);
    }
    let parent = native.as_ref().map(|n| n.parent);
    if let Some(native) = native {
        let product = product.ok_or("desktop requires a Rust product catalog")?;
        let tickets = tickets.ok_or("desktop directory authorization unavailable")?;
        app = desktop_router(
            app,
            DesktopConfig {
                desktop_token: native.token,
                control_token: native.control,
                origin: format!("http://{address}"),
                product,
                backend,
                tickets,
            },
        );
    }
    eprintln!("forge-server listening on http://{address} ({label})");
    let (stop, stopped) = tokio::sync::oneshot::channel::<()>();
    use std::future::IntoFuture;
    let mut server = Box::pin(
        axum::serve(listener, app)
            .with_graceful_shutdown(async {
                let _ = stopped.await;
            })
            .into_future(),
    );
    tokio::select! {
        result = &mut server => result.map_err(|_| "HTTP host failed"),
        () = shutdown(parent) => {
            let _ = stop.send(());
            // An open SSE subscriber cannot retain an orphan desktop host.
            match tokio::time::timeout(std::time::Duration::from_secs(5), server).await {
                Ok(result) => result.map_err(|_| "HTTP host failed"),
                Err(_) => Ok(()),
            }
        }
    }
}

async fn shutdown(parent: Option<u32>) {
    let parent_exit = async move {
        loop {
            tokio::time::sleep(std::time::Duration::from_secs(1)).await;
            #[cfg(unix)]
            if parent.is_some_and(|p| unsafe { libc::getppid() } as u32 != p) {
                return;
            }
        }
    };
    #[cfg(unix)]
    {
        let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("install SIGTERM handler");
        tokio::select! {_=tokio::signal::ctrl_c()=>{},_=term.recv()=>{},_=parent_exit=>{}}
    }
    #[cfg(not(unix))]
    {
        tokio::select! {_=tokio::signal::ctrl_c()=>{},_=parent_exit=>{}}
    }
}
