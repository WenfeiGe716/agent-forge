//! Disposable adapter for compatibility tests and embedding experiments.
//! It neither opens nor writes the Python or transaction-kernel databases.

use crate::{Backend, BackendError, Event, EventPage, ProjectAccess, RunView, User};
use scrypt::{
    password_hash::{PasswordHash, PasswordHasher, PasswordVerifier, SaltString},
    Params, Scrypt,
};
use serde_json::Value;
use std::{collections::BTreeMap, sync::RwLock};
use uuid::Uuid;

struct Account {
    user: User,
    password_hash: String,
}

#[derive(Default)]
struct Data {
    users: BTreeMap<String, Account>,
    projects: BTreeMap<String, ProjectAccess>,
    runs: BTreeMap<String, RunView>,
    events: BTreeMap<i64, Event>,
}

#[derive(Default)]
pub struct MemoryBackend {
    data: RwLock<Data>,
}

impl MemoryBackend {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn add_user(&self, user: User, password: &str) -> Result<(), BackendError> {
        if user.id.is_empty()
            || user.username.is_empty()
            || password.is_empty()
            || !matches!(user.role.as_str(), "admin" | "developer" | "viewer")
        {
            return Err(BackendError::InvalidData);
        }
        let salt = SaltString::encode_b64(Uuid::new_v4().as_bytes())
            .map_err(|_| BackendError::Unavailable)?;
        // Match the current Python host's scrypt work factor.
        let params = Params::new(14, 8, 1, 32).map_err(|_| BackendError::Unavailable)?;
        let password_hash = Scrypt
            .hash_password_customized(password.as_bytes(), None, None, params, &salt)
            .map_err(|_| BackendError::Unavailable)?
            .to_string();
        let mut data = self.data.write().map_err(|_| BackendError::Unavailable)?;
        if data.users.contains_key(&user.id)
            || data
                .users
                .values()
                .any(|a| a.user.username == user.username)
        {
            return Err(BackendError::InvalidData);
        }
        data.users.insert(
            user.id.clone(),
            Account {
                user,
                password_hash,
            },
        );
        Ok(())
    }

    pub fn set_user_enabled(&self, user_id: &str, enabled: bool) -> Result<(), BackendError> {
        let mut data = self.data.write().map_err(|_| BackendError::Unavailable)?;
        let account = data.users.get_mut(user_id).ok_or(BackendError::NotFound)?;
        account.user.enabled = enabled;
        Ok(())
    }

    pub fn upsert_project(&self, project: ProjectAccess) -> Result<(), BackendError> {
        if project.id.is_empty() || project.owner_id.is_empty() {
            return Err(BackendError::InvalidData);
        }
        self.data
            .write()
            .map_err(|_| BackendError::Unavailable)?
            .projects
            .insert(project.id.clone(), project);
        Ok(())
    }

    pub fn put_run(&self, run: RunView) -> Result<(), BackendError> {
        if run.id.is_empty()
            || run.summary.get("id").and_then(Value::as_str) != Some(&run.id)
            || run.detail.get("id").and_then(Value::as_str) != Some(&run.id)
            || run.summary.get("project_id").and_then(Value::as_str) != Some(&run.project_id)
            || run.detail.get("project_id").and_then(Value::as_str) != Some(&run.project_id)
        {
            return Err(BackendError::InvalidData);
        }
        let mut data = self.data.write().map_err(|_| BackendError::Unavailable)?;
        if !data.projects.contains_key(&run.project_id) {
            return Err(BackendError::NotFound);
        }
        data.runs.insert(run.id.clone(), run);
        Ok(())
    }

    pub fn append_event(&self, event: Event) -> Result<(), BackendError> {
        let mut data = self.data.write().map_err(|_| BackendError::Unavailable)?;
        // Keep the global cursor monotonic so a connected reader cannot miss
        // an event inserted behind its already-delivered cursor.
        if event.id <= data.events.last_key_value().map_or(0, |(id, _)| *id) {
            return Err(BackendError::InvalidData);
        }
        if !data.runs.contains_key(&event.run_id) {
            return Err(BackendError::NotFound);
        }
        data.events.insert(event.id, event);
        Ok(())
    }
}

fn access<'a>(data: &'a Data, user: &User, run_id: &str) -> Result<&'a RunView, BackendError> {
    let run = data.runs.get(run_id).ok_or(BackendError::NotFound)?;
    let project = data
        .projects
        .get(&run.project_id)
        .ok_or(BackendError::NotFound)?;
    if user.role == "admin" || user.id == project.owner_id || project.members.contains(&user.id) {
        Ok(run)
    } else {
        Err(BackendError::NotFound)
    }
}

impl Backend for MemoryBackend {
    fn check_credentials(
        &self,
        username: &str,
        password: &str,
    ) -> Result<Option<User>, BackendError> {
        let candidate = {
            let data = self.data.read().map_err(|_| BackendError::Unavailable)?;
            data.users
                .values()
                .find(|a| a.user.username == username)
                .map(|a| (a.user.clone(), a.password_hash.clone()))
        };
        let Some((user, encoded)) = candidate else {
            return Ok(None);
        };
        let hash = PasswordHash::new(&encoded).map_err(|_| BackendError::Unavailable)?;
        if user.enabled && Scrypt.verify_password(password.as_bytes(), &hash).is_ok() {
            Ok(Some(user))
        } else {
            Ok(None)
        }
    }

    fn user(&self, user_id: &str) -> Result<Option<User>, BackendError> {
        let data = self.data.read().map_err(|_| BackendError::Unavailable)?;
        Ok(data.users.get(user_id).map(|a| a.user.clone()))
    }

    fn runs(&self, user: &User, limit: usize) -> Result<Vec<Value>, BackendError> {
        let data = self.data.read().map_err(|_| BackendError::Unavailable)?;
        let mut runs: Vec<_> = data
            .runs
            .values()
            .filter(|r| access(&data, user, &r.id).is_ok())
            .collect();
        runs.sort_by(|a, b| {
            b.summary
                .get("created_at")
                .and_then(Value::as_str)
                .cmp(&a.summary.get("created_at").and_then(Value::as_str))
                .then_with(|| a.id.cmp(&b.id))
        });
        Ok(runs
            .into_iter()
            .take(limit)
            .map(|r| r.summary.clone())
            .collect())
    }

    fn run(&self, user: &User, run_id: &str) -> Result<Value, BackendError> {
        let data = self.data.read().map_err(|_| BackendError::Unavailable)?;
        let mut detail = access(&data, user, run_id)?.detail.clone();
        detail["event_cursor"] = data
            .events
            .values()
            .rev()
            .find(|event| event.run_id == run_id)
            .map_or(0, |event| event.id)
            .into();
        Ok(detail)
    }

    fn events(
        &self,
        user: &User,
        run_id: &str,
        after: i64,
        limit: usize,
    ) -> Result<EventPage, BackendError> {
        let data = self.data.read().map_err(|_| BackendError::Unavailable)?;
        access(&data, user, run_id)?;
        if after < 0 || !(1..=1000).contains(&limit) {
            return Err(BackendError::InvalidData);
        }
        use std::ops::Bound::{Excluded, Unbounded};
        let mut events: Vec<_> = data
            .events
            .range((Excluded(after), Unbounded))
            .map(|(_, event)| event)
            .filter(|event| event.run_id == run_id)
            .take(limit + 1)
            .cloned()
            .collect();
        let has_more = events.len() > limit;
        events.truncate(limit);
        Ok(EventPage { events, has_more })
    }
}
