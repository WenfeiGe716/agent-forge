//! Isolated member working copies and conservative three-way file integration.
//! The host calls these functions only while the affected member is stopped.
//! Canonical integration/prepare calls are serialized; members may execute in
//! their returned directories concurrently. Canonical Git HEAD is untouched so
//! the final Run artifact still contains every change relative to its baseline.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::{Read, Write},
    path::{Component, Path, PathBuf},
    sync::Mutex,
};

static INTEGRATION: Mutex<()> = Mutex::new(());
const MAX_FILES: usize = 30_000;
const MAX_BYTES: u64 = 100 * 1024 * 1024;

#[derive(Debug, thiserror::Error)]
pub(super) enum WorkspaceError {
    #[error("workspace files conflict: {0:?}")]
    Conflict(Vec<String>),
    #[error("workspace path or file is unsafe")]
    UnsafePath,
    #[error("workspace changed while preparing integration")]
    Changed,
    #[error("workspace exceeds the supported file or byte limit")]
    Limit,
    #[error("workspace storage is unavailable")]
    Io(#[from] std::io::Error),
    #[error("workspace metadata is invalid")]
    State,
    #[error("cannot initialize member Git repository")]
    Git,
}

type Result<T> = std::result::Result<T, WorkspaceError>;

#[derive(Debug, Clone, Serialize)]
pub(super) struct IntegrationReport {
    pub changed_files: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct Stamp {
    digest: String,
    executable: bool,
}

#[derive(Clone)]
struct File {
    stamp: Stamp,
    bytes: Vec<u8>,
}

type Files = BTreeMap<String, File>;

#[derive(Serialize, Deserialize)]
struct State {
    canonical: PathBuf,
    baseline: BTreeMap<String, Stamp>,
}

/// `storage` is host-owned and outside `canonical`; only its `workspace` child
/// is given to the member. Repeated calls preserve the path and pending member
/// edits, synchronizing non-conflicting canonical changes before activation.
pub(super) fn prepare(canonical: &Path, storage: &Path) -> Result<PathBuf> {
    let _lock = INTEGRATION.lock().map_err(|_| WorkspaceError::State)?;
    let (canonical, storage) = roots(canonical, storage)?;
    let current = snapshot(&canonical)?;
    let workspace = storage.join("workspace");
    let state_path = storage.join("baseline.json");
    if state_path.try_exists()? {
        let state = load(&state_path, &canonical)?;
        require_directory(&workspace)?;
        let member = snapshot(&workspace)?;
        let desired = merge(&state.baseline, &member, &current)?;
        apply(&workspace, &member, &desired)?;
        save(&state_path, &canonical, &current)?;
        return Ok(workspace);
    }
    // An incomplete initial copy is not silently adopted as a trusted baseline.
    if workspace.try_exists()? {
        return Err(WorkspaceError::State);
    }
    fs::create_dir(&workspace)?;
    apply(&workspace, &Files::new(), &current)?;
    initialize_git(&workspace)?;
    save(&state_path, &canonical, &current)?;
    Ok(workspace)
}

/// Merge a stopped member's changes into canonical. Conflicts are identified
/// for the entire change set before any files are changed. Different edits to
/// the same file conflict, even when a textual merge might be possible.
pub(super) fn integrate(canonical: &Path, storage: &Path) -> Result<IntegrationReport> {
    let _lock = INTEGRATION.lock().map_err(|_| WorkspaceError::State)?;
    let (canonical, storage) = roots(canonical, storage)?;
    let state_path = storage.join("baseline.json");
    let state = load(&state_path, &canonical)?;
    let workspace = storage.join("workspace");
    require_directory(&workspace)?;
    let member = snapshot(&workspace)?;
    let current = snapshot(&canonical)?;
    let desired = merge(&state.baseline, &member, &current)?;
    let changed_files = changed(&current, &desired);
    // Both workspaces are checked before applying, and apply checks once again
    // immediately before mutation. Scheduling keeps these two actors stopped.
    if stamps(&snapshot(&workspace)?) != stamps(&member) {
        return Err(WorkspaceError::Changed);
    }
    apply(&canonical, &current, &desired)?;
    apply(&workspace, &member, &desired)?;
    save(&state_path, &canonical, &desired)?;
    Ok(IntegrationReport { changed_files })
}

fn roots(canonical: &Path, storage: &Path) -> Result<(PathBuf, PathBuf)> {
    require_directory(canonical)?;
    let canonical = canonical.canonicalize()?;
    let absolute = if storage.is_absolute() {
        storage.to_owned()
    } else {
        std::env::current_dir()?.join(storage)
    };
    if absolute
        .components()
        .any(|component| matches!(component, Component::ParentDir | Component::CurDir))
    {
        return Err(WorkspaceError::UnsafePath);
    }
    let mut ancestor = absolute.as_path();
    let mut tail = Vec::new();
    while !ancestor.try_exists()? {
        tail.push(
            ancestor
                .file_name()
                .ok_or(WorkspaceError::UnsafePath)?
                .to_owned(),
        );
        ancestor = ancestor.parent().ok_or(WorkspaceError::UnsafePath)?;
    }
    require_directory(ancestor)?;
    let mut storage = ancestor.canonicalize()?;
    for component in tail.into_iter().rev() {
        storage.push(component);
    }
    if canonical.starts_with(&storage) || storage.starts_with(&canonical) {
        return Err(WorkspaceError::UnsafePath);
    }
    fs::create_dir_all(&storage)?;
    require_directory(&storage)?;
    Ok((canonical, storage))
}

fn require_directory(path: &Path) -> Result<()> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_dir() || meta.file_type().is_symlink() {
        return Err(WorkspaceError::UnsafePath);
    }
    Ok(())
}

fn regular_file(path: &Path) -> Result<fs::File> {
    let mut options = fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    let file = options.open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.file_type().is_symlink() {
        return Err(WorkspaceError::UnsafePath);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if metadata.nlink() != 1 {
            return Err(WorkspaceError::UnsafePath);
        }
    }
    Ok(file)
}

fn relative(path: &Path) -> Result<String> {
    if path.as_os_str().is_empty()
        || path.components().any(|c| match c {
            Component::Normal(name) => name.to_string_lossy().eq_ignore_ascii_case(".git"),
            _ => true,
        })
    {
        return Err(WorkspaceError::UnsafePath);
    }
    path.to_str()
        .map(str::to_owned)
        .ok_or(WorkspaceError::UnsafePath)
}

fn snapshot(root: &Path) -> Result<Files> {
    require_directory(root)?;
    let mut files = Files::new();
    let mut pending = vec![root.to_owned()];
    let mut total = 0u64;
    let mut entries = 0usize;
    while let Some(directory) = pending.pop() {
        require_directory(&directory)?;
        for entry in fs::read_dir(&directory)? {
            let entry = entry?;
            // Match project import/export rules. Runtime caches differ by cwd
            // and must not create spurious cross-member merge conflicts.
            if crate::product_api::workspace::excluded(&entry.file_name().to_string_lossy()) {
                continue;
            }
            entries += 1;
            if entries > MAX_FILES {
                return Err(WorkspaceError::Limit);
            }
            let path = entry.path();
            let rel = path
                .strip_prefix(root)
                .map_err(|_| WorkspaceError::UnsafePath)?;
            let name = relative(rel)?;
            if rel.components().count() > 64 {
                return Err(WorkspaceError::Limit);
            }
            let meta = fs::symlink_metadata(&path)?;
            if meta.file_type().is_symlink() {
                return Err(WorkspaceError::UnsafePath);
            }
            if meta.is_dir() {
                pending.push(path);
                continue;
            }
            let input = regular_file(&path)?;
            let metadata = input.metadata()?;
            total = total
                .checked_add(metadata.len())
                .ok_or(WorkspaceError::Limit)?;
            if total > MAX_BYTES {
                return Err(WorkspaceError::Limit);
            }
            let mut bytes = Vec::new();
            input.take(metadata.len() + 1).read_to_end(&mut bytes)?;
            if bytes.len() as u64 != metadata.len() {
                return Err(WorkspaceError::Changed);
            }
            #[cfg(unix)]
            let executable = {
                use std::os::unix::fs::PermissionsExt;
                metadata.permissions().mode() & 0o111 != 0
            };
            #[cfg(not(unix))]
            let executable = false;
            files.insert(
                name,
                File {
                    stamp: Stamp {
                        digest: format!("{:x}", Sha256::digest(&bytes)),
                        executable,
                    },
                    bytes,
                },
            );
        }
    }
    Ok(files)
}

fn stamps(files: &Files) -> BTreeMap<String, Stamp> {
    files
        .iter()
        .map(|(path, file)| (path.clone(), file.stamp.clone()))
        .collect()
}

fn changed(before: &Files, after: &Files) -> Vec<String> {
    before
        .keys()
        .chain(after.keys())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .filter(|key| {
            before.get(*key).map(|file| &file.stamp) != after.get(*key).map(|file| &file.stamp)
        })
        .cloned()
        .collect()
}

fn merge(base: &BTreeMap<String, Stamp>, member: &Files, current: &Files) -> Result<Files> {
    let mut desired = current.clone();
    let mut conflicts = BTreeSet::new();
    for key in base.keys().chain(member.keys()).collect::<BTreeSet<_>>() {
        let original = base.get(key);
        let updated = member.get(key).map(|file| &file.stamp);
        if updated == original {
            continue;
        }
        let present = current.get(key).map(|file| &file.stamp);
        if present != original && present != updated {
            conflicts.insert(key.clone());
            continue;
        }
        match member.get(key) {
            Some(file) => {
                desired.insert(key.clone(), file.clone());
            }
            None => {
                desired.remove(key);
            }
        }
    }
    // A file/directory collision is a conflict even when the leaf paths differ.
    for key in desired.keys() {
        for parent in Path::new(key)
            .ancestors()
            .skip(1)
            .take_while(|path| !path.as_os_str().is_empty())
        {
            let parent = relative(parent)?;
            if desired.contains_key(&parent) {
                conflicts.insert(key.clone());
                conflicts.insert(parent);
            }
        }
    }
    if !conflicts.is_empty() {
        return Err(WorkspaceError::Conflict(conflicts.into_iter().collect()));
    }
    Ok(desired)
}

fn apply(root: &Path, before: &Files, after: &Files) -> Result<()> {
    if stamps(&snapshot(root)?) != stamps(before) {
        return Err(WorkspaceError::Changed);
    }
    let keys = changed(before, after);
    // Remove obsolete leaves before writing, allowing a file to become a
    // directory (or the reverse) when the three-way merge authorized it.
    for key in keys.iter().filter(|key| !after.contains_key(*key)) {
        let path = root.join(relative(Path::new(key))?);
        fs::remove_file(&path)?;
        let mut parent = path.parent();
        while let Some(directory) = parent.filter(|directory| *directory != root) {
            match fs::remove_dir(directory) {
                Ok(()) => parent = directory.parent(),
                Err(error) if error.kind() == std::io::ErrorKind::DirectoryNotEmpty => break,
                Err(error) => return Err(error.into()),
            }
        }
    }
    for key in keys.iter().filter(|key| after.contains_key(*key)) {
        let path = root.join(relative(Path::new(key))?);
        if path.is_dir() {
            remove_empty_directories(&path)?;
        }
        let parent = path.parent().ok_or(WorkspaceError::UnsafePath)?;
        fs::create_dir_all(parent)?;
        require_directory(parent)?;
        atomic_write(&path, &after[key].bytes, after[key].stamp.executable)?;
    }
    Ok(())
}

fn remove_empty_directories(path: &Path) -> Result<()> {
    require_directory(path)?;
    for entry in fs::read_dir(path)? {
        let entry = entry?;
        require_directory(&entry.path())?;
        remove_empty_directories(&entry.path())?;
    }
    fs::remove_dir(path)?;
    Ok(())
}

fn atomic_write(path: &Path, bytes: &[u8], executable: bool) -> Result<()> {
    let parent = path.parent().ok_or(WorkspaceError::UnsafePath)?;
    require_directory(parent)?;
    let temporary = parent.join(format!(".forge-write-{}", uuid::Uuid::new_v4()));
    let result = (|| {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        file.write_all(bytes)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            file.set_permissions(fs::Permissions::from_mode(if executable {
                0o755
            } else {
                0o644
            }))?;
        }
        #[cfg(not(unix))]
        let _ = executable;
        file.sync_all()?;
        fs::rename(&temporary, path)?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(temporary);
    }
    result
}

fn load(path: &Path, canonical: &Path) -> Result<State> {
    let mut bytes = Vec::new();
    regular_file(path)?
        .take(8 * 1024 * 1024 + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() > 8 * 1024 * 1024 {
        return Err(WorkspaceError::Limit);
    }
    let state: State = serde_json::from_slice(&bytes).map_err(|_| WorkspaceError::State)?;
    if state.canonical != canonical || state.baseline.len() > MAX_FILES {
        return Err(WorkspaceError::State);
    }
    for path in state.baseline.keys() {
        relative(Path::new(path))?;
    }
    Ok(state)
}

fn save(path: &Path, canonical: &Path, files: &Files) -> Result<()> {
    let state = State {
        canonical: canonical.to_owned(),
        baseline: stamps(files),
    };
    let bytes = serde_json::to_vec(&state).map_err(|_| WorkspaceError::State)?;
    atomic_write(path, &bytes, false)
}

fn initialize_git(workspace: &Path) -> Result<()> {
    for args in [
        vec!["init", "--quiet"],
        vec!["add", "--all"],
        vec![
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "Forge member baseline",
        ],
    ] {
        let mut command = std::process::Command::new("git");
        for (key, _) in
            std::env::vars_os().filter(|(key, _)| key.to_string_lossy().starts_with("GIT_"))
        {
            command.env_remove(key);
        }
        let output = command
            .args([
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=Forge",
                "-c",
                "user.email=forge@localhost",
            ])
            .arg("-C")
            .arg(workspace)
            .args(args)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .output()?;
        if !output.status.success() {
            return Err(WorkspaceError::Git);
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fixture(PathBuf);

    impl Fixture {
        fn new() -> Self {
            let root = std::env::temp_dir()
                .join(format!("forge-member-workspaces-{}", uuid::Uuid::new_v4()));
            fs::create_dir_all(root.join("canonical")).unwrap();
            fs::write(root.join("canonical/shared.txt"), "original\n").unwrap();
            initialize_git(&root.join("canonical")).unwrap();
            Self(root)
        }

        fn canonical(&self) -> PathBuf {
            self.0.join("canonical")
        }
        fn storage(&self, member: &str) -> PathBuf {
            self.0.join("members").join(member)
        }
        fn member(&self, member: &str) -> PathBuf {
            prepare(&self.canonical(), &self.storage(member)).unwrap()
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn parallel_members_are_isolated_and_disjoint_results_integrate_without_moving_canonical_head()
    {
        let fixture = Fixture::new();
        let canonical = fixture.canonical();
        let head = fs::read(canonical.join(".git/refs/heads/master"))
            .or_else(|_| fs::read(canonical.join(".git/refs/heads/main")))
            .unwrap();
        let a = fixture.member("a");
        let b = fixture.member("b");
        assert!(a.join(".git").is_dir() && b.join(".git").is_dir());
        assert_ne!(
            a.join(".git").canonicalize().unwrap(),
            canonical.join(".git").canonicalize().unwrap()
        );
        let jobs: Vec<_> = [(a.clone(), "a.txt"), (b.clone(), "b.txt")]
            .into_iter()
            .map(|(path, name)| {
                std::thread::spawn(move || {
                    fs::write(path.join(name), name).unwrap();
                    assert!(!path
                        .join(if name == "a.txt" { "b.txt" } else { "a.txt" })
                        .exists());
                })
            })
            .collect();
        for job in jobs {
            job.join().unwrap();
        }
        assert!(!canonical.join("a.txt").exists() && !canonical.join("b.txt").exists());
        let jobs: Vec<_> = [fixture.storage("a"), fixture.storage("b")]
            .into_iter()
            .map(|storage| {
                let canonical = canonical.clone();
                std::thread::spawn(move || integrate(&canonical, &storage).unwrap())
            })
            .collect();
        let changed: BTreeSet<_> = jobs
            .into_iter()
            .flat_map(|job| job.join().unwrap().changed_files)
            .collect();
        assert_eq!(changed, BTreeSet::from(["a.txt".into(), "b.txt".into()]));
        assert_eq!(
            fs::read_to_string(canonical.join("a.txt")).unwrap(),
            "a.txt"
        );
        assert_eq!(
            fs::read_to_string(canonical.join("b.txt")).unwrap(),
            "b.txt"
        );
        assert_eq!(
            head,
            fs::read(canonical.join(".git/refs/heads/master"))
                .or_else(|_| fs::read(canonical.join(".git/refs/heads/main")))
                .unwrap()
        );
        assert!(integrate(&canonical, &fixture.storage("a"))
            .unwrap()
            .changed_files
            .is_empty());
    }

    #[test]
    fn member_runtime_caches_do_not_conflict_or_enter_integrated_results() {
        let fixture = Fixture::new();
        let a = fixture.member("a");
        let b = fixture.member("b");
        for (workspace, contents) in [(&a, "cache from a"), (&b, "cache from b")] {
            fs::create_dir(workspace.join("__pycache__")).unwrap();
            fs::write(workspace.join("__pycache__/test.pyc"), contents).unwrap();
            fs::write(workspace.join(".env"), "private runtime setting").unwrap();
        }
        fs::write(a.join("a.txt"), "a").unwrap();
        fs::write(b.join("b.txt"), "b").unwrap();
        assert_eq!(
            integrate(&fixture.canonical(), &fixture.storage("a"))
                .unwrap()
                .changed_files,
            ["a.txt"]
        );
        assert_eq!(
            integrate(&fixture.canonical(), &fixture.storage("b"))
                .unwrap()
                .changed_files,
            ["b.txt"]
        );
        assert!(!fixture.canonical().join("__pycache__").exists());
        assert!(!fixture.canonical().join(".env").exists());
        assert_eq!(
            fs::read_to_string(a.join("__pycache__/test.pyc")).unwrap(),
            "cache from a"
        );
    }

    #[test]
    fn same_file_conflict_preserves_every_canonical_file_and_the_losing_members_result() {
        let fixture = Fixture::new();
        let a = fixture.member("a");
        let b = fixture.member("b");
        fs::write(a.join("shared.txt"), "member a\n").unwrap();
        fs::write(b.join("shared.txt"), "member b\n").unwrap();
        fs::write(b.join("unique.txt"), "must not partially integrate\n").unwrap();
        integrate(&fixture.canonical(), &fixture.storage("a")).unwrap();
        let before = stamps(&snapshot(&fixture.canonical()).unwrap());
        assert!(
            matches!(integrate(&fixture.canonical(), &fixture.storage("b")), Err(WorkspaceError::Conflict(paths)) if paths == ["shared.txt"])
        );
        assert_eq!(before, stamps(&snapshot(&fixture.canonical()).unwrap()));
        assert!(!fixture.canonical().join("unique.txt").exists());
        assert_eq!(
            fs::read_to_string(b.join("shared.txt")).unwrap(),
            "member b\n"
        );
        assert!(matches!(
            prepare(&fixture.canonical(), &fixture.storage("b")),
            Err(WorkspaceError::Conflict(_))
        ));
    }

    #[test]
    fn reactivation_reuses_directory_and_syncs_canonical_without_discarding_pending_member_edits() {
        let fixture = Fixture::new();
        let a = fixture.member("a");
        fs::write(a.join("a.txt"), "first\n").unwrap();
        integrate(&fixture.canonical(), &fixture.storage("a")).unwrap();
        fs::write(a.join("a.txt"), "pending member edit\n").unwrap();
        fs::write(fixture.canonical().join("leader.txt"), "leader review\n").unwrap();
        let resumed = fixture.member("a");
        assert_eq!(a, resumed);
        assert_eq!(
            fs::read_to_string(a.join("leader.txt")).unwrap(),
            "leader review\n"
        );
        assert_eq!(
            fs::read_to_string(a.join("a.txt")).unwrap(),
            "pending member edit\n"
        );
        assert_eq!(
            fs::read_to_string(fixture.canonical().join("a.txt")).unwrap(),
            "first\n"
        );
        let result = integrate(&fixture.canonical(), &fixture.storage("a")).unwrap();
        assert_eq!(result.changed_files, ["a.txt"]);
        assert_eq!(
            fs::read_to_string(fixture.canonical().join("a.txt")).unwrap(),
            "pending member edit\n"
        );
    }

    #[test]
    fn path_prefix_conflict_and_delete_modify_conflict_do_not_overwrite() {
        let fixture = Fixture::new();
        let a = fixture.member("a");
        let b = fixture.member("b");
        fs::write(a.join("path"), "file\n").unwrap();
        fs::create_dir(b.join("path")).unwrap();
        fs::write(b.join("path/child"), "directory child\n").unwrap();
        fs::remove_file(a.join("shared.txt")).unwrap();
        fs::write(b.join("shared.txt"), "modified by b\n").unwrap();
        integrate(&fixture.canonical(), &fixture.storage("a")).unwrap();
        assert!(
            matches!(integrate(&fixture.canonical(), &fixture.storage("b")), Err(WorkspaceError::Conflict(paths)) if paths == ["path", "path/child", "shared.txt"])
        );
        assert_eq!(
            fs::read_to_string(fixture.canonical().join("path")).unwrap(),
            "file\n"
        );
        assert!(!fixture.canonical().join("shared.txt").exists());
    }

    #[test]
    fn file_directory_replacements_and_identical_parallel_edits_are_supported() {
        let fixture = Fixture::new();
        let a = fixture.member("a");
        let b = fixture.member("b");
        for workspace in [&a, &b] {
            fs::remove_file(workspace.join("shared.txt")).unwrap();
            fs::create_dir(workspace.join("shared.txt")).unwrap();
            fs::write(workspace.join("shared.txt/child"), "same\n").unwrap();
        }
        integrate(&fixture.canonical(), &fixture.storage("a")).unwrap();
        assert!(integrate(&fixture.canonical(), &fixture.storage("b"))
            .unwrap()
            .changed_files
            .is_empty());
        fs::remove_file(a.join("shared.txt/child")).unwrap();
        fs::remove_dir(a.join("shared.txt")).unwrap();
        fs::write(a.join("shared.txt"), "a file again\n").unwrap();
        integrate(&fixture.canonical(), &fixture.storage("a")).unwrap();
        assert_eq!(
            fs::read_to_string(fixture.canonical().join("shared.txt")).unwrap(),
            "a file again\n"
        );
    }

    #[cfg(unix)]
    #[test]
    fn symlinks_hardlinks_and_redirected_storage_are_rejected_without_touching_external_files() {
        use std::os::unix::fs::symlink;
        let fixture = Fixture::new();
        let a = fixture.member("a");
        let external = fixture.0.join("outside.txt");
        fs::write(&external, "outside\n").unwrap();
        symlink(&external, a.join("escape")).unwrap();
        assert!(integrate(&fixture.canonical(), &fixture.storage("a")).is_err());
        fs::remove_file(a.join("escape")).unwrap();
        fs::hard_link(&external, a.join("escape")).unwrap();
        assert!(integrate(&fixture.canonical(), &fixture.storage("a")).is_err());
        fs::remove_file(a.join("escape")).unwrap();
        let fake = fixture.storage("redirect");
        symlink(fixture.canonical(), &fake).unwrap();
        assert!(prepare(&fixture.canonical(), &fake).is_err());
        assert!(matches!(
            prepare(
                &fixture.canonical(),
                &fixture.canonical().join("nested/new")
            ),
            Err(WorkspaceError::UnsafePath)
        ));
        assert!(!fixture.canonical().join("nested").exists());
        assert_eq!(fs::read_to_string(&external).unwrap(), "outside\n");
    }
}
