use super::*;
use forge_core::{Claim, TaskStatus};
use std::{
    fs,
    path::PathBuf,
    sync::{
        atomic::{AtomicI64, Ordering},
        Barrier,
    },
    thread,
};

struct TestDatabase(PathBuf);

impl TestDatabase {
    fn new() -> Self {
        let directory = std::env::temp_dir().join(format!("forge-store-test-{}", Uuid::new_v4()));
        fs::create_dir(&directory).unwrap();
        Self(directory)
    }

    fn path(&self) -> PathBuf {
        self.0.join("kernel.sqlite3")
    }
}

impl Drop for TestDatabase {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

struct TestClock(AtomicI64);

impl TestClock {
    fn new(now: i64) -> Arc<Self> {
        Arc::new(Self(AtomicI64::new(now)))
    }

    fn set(&self, now: i64) {
        self.0.store(now, Ordering::SeqCst);
    }
}

impl Clock for TestClock {
    fn now_ms(&self) -> i64 {
        self.0.load(Ordering::SeqCst)
    }
}

fn seed(store: &mut Store) {
    store
        .execute(
            "create-run",
            Command::CreateRun {
                run_id: "run".into(),
            },
        )
        .unwrap();
    store
        .execute(
            "create-task",
            Command::CreateTask {
                run_id: "run".into(),
                task_id: "task".into(),
            },
        )
        .unwrap();
}

fn claim_command(worker_id: &str, expected_version: i64) -> Command {
    Command::ClaimTask {
        task_id: "task".into(),
        worker_id: worker_id.into(),
        expected_version,
        lease_duration_ms: 100,
    }
}

fn claim(store: &mut Store) -> Claim {
    let receipt = store.execute("claim", claim_command("worker", 0)).unwrap();
    let CommandResult::Task(task) = receipt.result else {
        panic!("expected task");
    };
    task.claim.unwrap()
}

fn renew_command(claim: &Claim) -> Command {
    Command::RenewClaim {
        task_id: "task".into(),
        claim_token: claim.token.clone(),
        attempt_generation: claim.generation,
        lease_duration_ms: 100,
    }
}

#[test]
fn renewed_claim_and_exact_receipt_survive_reopen_without_extending_on_retry() {
    let database = TestDatabase::new();
    let clock = TestClock::new(1_000);
    let mut store = Store::open_with_clock(database.path(), clock.clone()).unwrap();
    seed(&mut store);
    let original_claim = claim(&mut store);
    clock.set(1_050);
    let command = renew_command(&original_claim);
    let receipt = store.execute("renew", command.clone()).unwrap();
    let persisted = store.get_task("task").unwrap().unwrap();
    assert_eq!(persisted.claim.as_ref().unwrap().expires_at_ms, 1_150);
    assert_eq!(persisted.version, 2);
    assert_eq!(persisted.attempt_generation, 1);
    let expiry: i64 = store
        .connection
        .query_row(
            "SELECT expires_at_ms FROM attempts WHERE task_id='task' AND generation=1",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(expiry, 1_150);
    drop(store);

    clock.set(1_125);
    let mut reopened = Store::open_with_clock(database.path(), clock.clone()).unwrap();
    assert_eq!(reopened.get_task("task").unwrap().unwrap(), persisted);
    assert_eq!(reopened.get_receipt("renew").unwrap().unwrap(), receipt);
    assert_eq!(reopened.execute("renew", command.clone()).unwrap(), receipt);
    assert_eq!(reopened.events_after(0, 100).unwrap().len(), 4);
    assert_eq!(reopened.pending_outbox(100).unwrap().len(), 4);
    assert_eq!(reopened.get_task("task").unwrap().unwrap(), persisted);

    // Even after expiry, replay reports what was committed; it grants no new lease.
    clock.set(1_150);
    assert_eq!(reopened.execute("renew", command.clone()).unwrap(), receipt);
    assert!(matches!(
        reopened.execute("renew-again", command),
        Err(StoreError::Domain(DomainError::LeaseExpired))
    ));
    assert!(matches!(
        reopened.execute(
            "late-submit",
            Command::SubmitTask {
                task_id: "task".into(),
                claim_token: original_claim.token,
                attempt_generation: original_claim.generation,
                result: Value::Null
            }
        ),
        Err(StoreError::Domain(DomainError::LeaseExpired))
    ));
    assert!(reopened.get_receipt("renew-again").unwrap().is_none());
}

#[test]
fn renewal_rejects_wrong_token_generation_duration_and_expired_lease_without_mutation() {
    let database = TestDatabase::new();
    let clock = TestClock::new(1_000);
    let mut store = Store::open_with_clock(database.path(), clock.clone()).unwrap();
    seed(&mut store);
    let original = claim(&mut store);
    let snapshot = store.get_task("task").unwrap();
    for (id, token, generation, duration) in [
        ("wrong-token", "wrong", 1, 100),
        ("wrong-generation", original.token.as_str(), 2, 100),
        ("bad-duration", original.token.as_str(), 1, 0),
        (
            "long-duration",
            original.token.as_str(),
            1,
            forge_core::MAX_LEASE_DURATION_MS + 1,
        ),
    ] {
        assert!(store
            .execute(
                id,
                Command::RenewClaim {
                    task_id: "task".into(),
                    claim_token: token.into(),
                    attempt_generation: generation,
                    lease_duration_ms: duration
                }
            )
            .is_err());
        assert!(store.get_receipt(id).unwrap().is_none());
        assert_eq!(store.get_task("task").unwrap(), snapshot);
    }
    clock.set(1_100);
    assert!(matches!(
        store.execute("expired", renew_command(&original)),
        Err(StoreError::Domain(DomainError::LeaseExpired))
    ));
    assert_eq!(store.events_after(0, 100).unwrap().len(), 3);
}

#[test]
fn reconciliation_is_explicit_evidenced_versioned_durable_and_fences_old_attempt() {
    let database = TestDatabase::new();
    let clock = TestClock::new(1_000);
    let mut store = Store::open_with_clock(database.path(), clock.clone()).unwrap();
    seed(&mut store);
    let old_claim = claim(&mut store);
    let reconciliation = Command::ReconcileTask {
        task_id: "task".into(),
        expected_version: 2,
        evidence_ref: "reconciliation/worker-stopped-and-worktree-reset".into(),
    };
    assert!(store.execute("premature", reconciliation.clone()).is_err());
    clock.set(1_100);
    store
        .execute(
            "unknown",
            Command::MarkOutcomeUnknown {
                task_id: "task".into(),
                expected_version: 1,
            },
        )
        .unwrap();
    assert!(store
        .execute("automatic-reclaim", claim_command("next-worker", 2))
        .is_err());
    assert!(matches!(
        store.execute(
            "empty-evidence",
            Command::ReconcileTask {
                task_id: "task".into(),
                expected_version: 2,
                evidence_ref: " \n ".into()
            }
        ),
        Err(StoreError::Domain(DomainError::EmptyIdentifier(
            "evidence_ref"
        )))
    ));
    assert!(matches!(
        store.execute(
            "stale-decision",
            Command::ReconcileTask {
                task_id: "task".into(),
                expected_version: 1,
                evidence_ref: "reconciliation/record".into()
            }
        ),
        Err(StoreError::Domain(DomainError::VersionConflict { .. }))
    ));
    let receipt = store.execute("reconcile", reconciliation.clone()).unwrap();
    drop(store);

    let mut reopened = Store::open_with_clock(database.path(), clock).unwrap();
    let task = reopened.get_task("task").unwrap().unwrap();
    assert_eq!(task.status, TaskStatus::Ready);
    assert_eq!(task.version, 3);
    assert_eq!(task.attempt_generation, 1);
    assert_eq!(reopened.get_receipt("reconcile").unwrap().unwrap(), receipt);
    let event = reopened
        .events_after(receipt.event_id - 1, 1)
        .unwrap()
        .remove(0);
    assert_eq!(event.kind, "task_reconciled");
    assert_eq!(
        event.payload["evidence_ref"],
        "reconciliation/worker-stopped-and-worktree-reset"
    );
    reopened
        .execute("new-claim", claim_command("next-worker", 3))
        .unwrap();
    assert_eq!(
        reopened.execute("reconcile", reconciliation).unwrap(),
        receipt
    );
    let current = reopened.get_task("task").unwrap().unwrap();
    assert_eq!(current.status, TaskStatus::Running);
    assert_eq!(current.attempt_generation, 2);
    assert_ne!(current.claim.as_ref().unwrap().token, old_claim.token);
    assert!(matches!(
        reopened.execute(
            "old-submit",
            Command::SubmitTask {
                task_id: "task".into(),
                claim_token: old_claim.token.clone(),
                attempt_generation: old_claim.generation,
                result: Value::Null
            }
        ),
        Err(StoreError::Domain(DomainError::StaleClaim))
    ));
    assert!(matches!(
        reopened.execute("old-renew", renew_command(&old_claim)),
        Err(StoreError::Domain(DomainError::StaleClaim))
    ));
}

#[test]
fn separate_connections_reject_a_stale_observed_version_on_the_same_thread() {
    let database = TestDatabase::new();
    let mut first = Store::open(database.path()).unwrap();
    seed(&mut first);
    let mut second = Store::open(database.path()).unwrap();
    let first_version = first.get_task("task").unwrap().unwrap().version;
    let second_version = second.get_task("task").unwrap().unwrap().version;
    first
        .execute("first", claim_command("first", first_version))
        .unwrap();
    assert!(matches!(
        second.execute("second", claim_command("second", second_version)),
        Err(StoreError::Domain(DomainError::VersionConflict {
            expected: 0,
            actual: 1
        }))
    ));
    assert_eq!(
        second
            .get_task("task")
            .unwrap()
            .unwrap()
            .claim
            .unwrap()
            .worker_id,
        "first"
    );
    assert!(second.get_receipt("second").unwrap().is_none());
}

#[test]
fn simultaneous_sqlite_writers_produce_exactly_one_claim_and_event() {
    let database = TestDatabase::new();
    let mut first = Store::open(database.path()).unwrap();
    seed(&mut first);
    let mut second = Store::open(database.path()).unwrap();
    let barrier = Arc::new(Barrier::new(2));
    let first_barrier = barrier.clone();
    let one = thread::spawn(move || {
        first_barrier.wait();
        first.execute("first", claim_command("first", 0))
    });
    let two = thread::spawn(move || {
        barrier.wait();
        second.execute("second", claim_command("second", 0))
    });
    let results = [one.join().unwrap(), two.join().unwrap()];
    assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
    assert!(results.iter().any(|result| matches!(
        result,
        Err(StoreError::Domain(DomainError::VersionConflict { .. }))
    )));
    let reopened = Store::open(database.path()).unwrap();
    assert_eq!(
        reopened
            .get_task("task")
            .unwrap()
            .unwrap()
            .attempt_generation,
        1
    );
    assert_eq!(reopened.events_after(0, 100).unwrap().len(), 3);
    assert_eq!(reopened.pending_outbox(100).unwrap().len(), 3);
}

#[test]
fn renewal_outbox_failure_rolls_back_attempt_expiry_and_state_together() {
    let database = TestDatabase::new();
    let clock = TestClock::new(1_000);
    let mut store = Store::open_with_clock(database.path(), clock.clone()).unwrap();
    seed(&mut store);
    let original_claim = claim(&mut store);
    let before = store.get_task("task").unwrap();
    store.connection.execute_batch("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'injected'); END;").unwrap();
    clock.set(1_050);
    assert!(store
        .execute("renew", renew_command(&original_claim))
        .is_err());
    assert_eq!(store.get_task("task").unwrap(), before);
    let expiry: i64 = store
        .connection
        .query_row(
            "SELECT expires_at_ms FROM attempts WHERE task_id='task' AND generation=1",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(expiry, 1_100);
    assert!(store.get_receipt("renew").unwrap().is_none());
    assert_eq!(store.events_after(0, 100).unwrap().len(), 3);
}

#[test]
fn new_database_has_stable_ownership_and_schema_on_reopen() {
    let database = TestDatabase::new();
    let store = Store::open(database.path()).unwrap();
    let application_id: i64 = store
        .connection
        .pragma_query_value(None, "application_id", |row| row.get(0))
        .unwrap();
    let version: i64 = store
        .connection
        .pragma_query_value(None, "user_version", |row| row.get(0))
        .unwrap();
    let schema: i64 = store
        .connection
        .pragma_query_value(None, "schema_version", |row| row.get(0))
        .unwrap();
    assert_eq!(application_id, APPLICATION_ID);
    assert_eq!(version, SCHEMA_VERSION);
    drop(store);
    let reopened = Store::open(database.path()).unwrap();
    assert_eq!(
        reopened
            .connection
            .pragma_query_value(None, "schema_version", |row| row.get::<_, i64>(0))
            .unwrap(),
        schema
    );
    assert_eq!(
        reopened
            .connection
            .pragma_query_value(None, "user_version", |row| row.get::<_, i64>(0))
            .unwrap(),
        version
    );
}

#[test]
fn python_owned_and_unsupported_databases_are_rejected_without_changes() {
    for (application_id, user_version) in [
        (0, 0),
        (0, 17),
        (APPLICATION_ID, SCHEMA_VERSION + 1),
        (12345, 1),
    ] {
        let database = TestDatabase::new();
        let connection = Connection::open(database.path()).unwrap();
        connection.execute_batch("CREATE TABLE python_runs(id TEXT PRIMARY KEY, payload TEXT); INSERT INTO python_runs VALUES ('existing', 'must stay unchanged');").unwrap();
        connection
            .pragma_update(None, "application_id", application_id)
            .unwrap();
        connection
            .pragma_update(None, "user_version", user_version)
            .unwrap();
        drop(connection);
        let before = fs::read(database.path()).unwrap();
        assert!(matches!(
            Store::open(database.path()),
            Err(StoreError::IncompatibleDatabase)
        ));
        assert_eq!(fs::read(database.path()).unwrap(), before);
        assert!(!database
            .path()
            .with_file_name("kernel.sqlite3-wal")
            .exists());
        let connection = Connection::open(database.path()).unwrap();
        assert_eq!(
            connection
                .pragma_query_value(None, "application_id", |row| row.get::<_, i64>(0))
                .unwrap(),
            application_id
        );
        assert_eq!(
            connection
                .pragma_query_value(None, "user_version", |row| row.get::<_, i64>(0))
                .unwrap(),
            user_version
        );
        let count: i64 = connection.query_row("SELECT count(*) FROM python_runs WHERE id='existing' AND payload='must stay unchanged'", [], |row| row.get(0)).unwrap();
        assert_eq!(count, 1);
    }
}
