//! Private, persistent NDJSON host for the Rust transaction kernel.
//! A desktop/API adapter owns authentication; this executable exposes no socket.
use forge_core::{Command, DomainError};
use forge_store::{Store, StoreError};
use serde::Deserialize;
use serde_json::{json, Value};
use std::io::{self, BufRead, Read, Write};
use std::path::PathBuf;

const MAX_FRAME_BYTES: usize = 1024 * 1024;
const MAX_PAGE_SIZE: u32 = 1000;

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
enum Request {
    Execute {
        command_id: String,
        command: Command,
    },
    Task {
        task_id: String,
    },
    Run {
        run_id: String,
    },
    RouteBinding {
        run_id: String,
    },
    ExecutionPolicy {
        run_id: String,
    },
    ProviderSnapshot {
        provider_id: String,
        provider_revision_id: String,
    },
    RunProviderSnapshot {
        run_id: String,
    },
    Approval {
        approval_id: String,
    },
    Approvals {
        run_id: String,
        limit: u32,
    },
    Receipt {
        command_id: String,
    },
    Events {
        after: i64,
        limit: u32,
    },
    Outbox {
        limit: u32,
    },
    AcknowledgeOutbox {
        event_id: i64,
    },
}

fn error(code: &str, message: &str) -> Value {
    json!({"schema_version": 1, "ok": false, "error": {"code": code, "message": message}})
}

fn store_error(cause: StoreError) -> Value {
    let code = match cause {
        StoreError::Domain(DomainError::VersionConflict { .. })
        | StoreError::ConcurrentMutation => "version_conflict",
        StoreError::Domain(DomainError::StaleClaim) => "stale_claim",
        StoreError::Domain(DomainError::LeaseExpired) => "lease_expired",
        StoreError::Domain(DomainError::LeaseActive) => "lease_active",
        StoreError::Domain(DomainError::InvalidState(_)) => "invalid_state",
        StoreError::Domain(_) => "invalid_command",
        StoreError::CommandConflict(_) => "command_conflict",
        StoreError::RuntimeEventSequenceConflict { .. } => "event_sequence_conflict",
        StoreError::NotFound { .. } => "not_found",
        StoreError::AlreadyExists { .. } => "already_exists",
        StoreError::IncompatibleDatabase => "incompatible_database",
        StoreError::Sql(_) | StoreError::Json(_) => "storage_error",
    };
    error(
        code,
        "operation rejected; query the command receipt before retrying an uncertain write",
    )
}

fn handle(store: &mut Store, request: Request) -> Value {
    let result = match request {
        Request::Execute {
            command_id,
            command,
        } => store
            .execute(&command_id, command)
            .and_then(|result| serde_json::to_value(result).map_err(Into::into)),
        Request::Task { task_id } => store
            .get_task(&task_id)
            .and_then(|result| serde_json::to_value(result).map_err(Into::into)),
        Request::Run { run_id } => store
            .get_run(&run_id)
            .and_then(|result| serde_json::to_value(result).map_err(Into::into)),
        Request::RouteBinding { run_id } => store
            .get_route_binding(&run_id)
            .and_then(|result| serde_json::to_value(result).map_err(Into::into)),
        Request::ExecutionPolicy { run_id } => store
            .get_execution_policy(&run_id)
            .and_then(|result| serde_json::to_value(result).map_err(Into::into)),
        Request::ProviderSnapshot {
            provider_id,
            provider_revision_id,
        } => store
            .get_provider_snapshot(&provider_id, &provider_revision_id)
            .and_then(|result| serde_json::to_value(result).map_err(Into::into)),
        Request::RunProviderSnapshot { run_id } => store
            .get_run_provider_snapshot(&run_id)
            .and_then(|result| serde_json::to_value(result).map_err(Into::into)),
        Request::Approval { approval_id } => store
            .get_approval(&approval_id)
            .and_then(|result| serde_json::to_value(result).map_err(Into::into)),
        Request::Approvals { run_id, limit } if (1..=MAX_PAGE_SIZE).contains(&limit) => store
            .list_approvals(&run_id, limit)
            .and_then(|result| serde_json::to_value(result).map_err(Into::into)),
        Request::Receipt { command_id } => store
            .get_receipt(&command_id)
            .and_then(|result| serde_json::to_value(result).map_err(Into::into)),
        Request::Events { after, limit } if after >= 0 && (1..=MAX_PAGE_SIZE).contains(&limit) => {
            store
                .events_after(after, limit)
                .and_then(|result| serde_json::to_value(result).map_err(Into::into))
        }
        Request::Outbox { limit } if (1..=MAX_PAGE_SIZE).contains(&limit) => store
            .pending_outbox(limit)
            .and_then(|result| serde_json::to_value(result).map_err(Into::into)),
        Request::AcknowledgeOutbox { event_id } if event_id > 0 => {
            store.acknowledge_outbox(event_id).map(Value::Bool)
        }
        _ => {
            return error(
                "invalid_query",
                "expected a nonnegative cursor, positive event ID and limit between 1 and 1000",
            )
        }
    };
    match result {
        Ok(value) => json!({"schema_version": 1, "ok": true, "result": value}),
        // Database diagnostics can include data or paths. Keep the transport
        // response bounded; typed store errors remain available to Rust hosts.
        Err(cause) => store_error(cause),
    }
}

fn serve(store: &mut Store, input: impl BufRead, mut output: impl Write) -> io::Result<()> {
    let mut input = input;
    loop {
        let mut frame = Vec::new();
        let bytes = input
            .by_ref()
            .take((MAX_FRAME_BYTES + 1) as u64)
            .read_until(b'\n', &mut frame)?;
        if bytes == 0 {
            return Ok(());
        }
        let close = bytes > MAX_FRAME_BYTES || !frame.ends_with(b"\n");
        let response = if close {
            error(
                "invalid_frame",
                "expected a newline-terminated JSON frame of at most 1 MiB",
            )
        } else {
            match serde_json::from_slice::<Request>(&frame) {
                Ok(request) => handle(store, request),
                Err(_) => error("invalid_request", "invalid command envelope"),
            }
        };
        serde_json::to_writer(&mut output, &response)?;
        output.write_all(b"\n")?;
        output.flush()?;
        if close {
            return Ok(());
        }
    }
}

fn main() {
    let mut args = std::env::args_os().skip(1);
    let path = match (args.next(), args.next(), args.next()) {
        (Some(flag), Some(path), None) if flag == "--db" => PathBuf::from(path),
        _ => {
            eprintln!("usage: forge-kernel --db /absolute/path/to/new-kernel.sqlite3");
            std::process::exit(2);
        }
    };
    if !path.is_absolute() {
        eprintln!("database path must be absolute");
        std::process::exit(2);
    }
    let mut store = match Store::open(path) {
        Ok(store) => store,
        Err(_) => {
            eprintln!(
                "cannot open Forge kernel database; check format, parent directory and access"
            );
            std::process::exit(1);
        }
    };
    if let Err(cause) = serve(&mut store, io::stdin().lock(), io::stdout().lock()) {
        if cause.kind() != io::ErrorKind::BrokenPipe {
            eprintln!("kernel transport closed with an I/O error");
        }
        std::process::exit(1);
    }
}
