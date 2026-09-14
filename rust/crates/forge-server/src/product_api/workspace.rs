use super::*;
use std::fs;
use std::process::Command;

pub(crate) fn snapshot(source: &Path, destination: &Path) -> Result<()> {
    fs::create_dir(destination).map_err(|_| bad("Cannot create run workspace"))?;
    let mut count = 0;
    let mut total = 0;
    #[cfg(unix)]
    copy_directory(
        &open_directory(source)?,
        destination,
        &mut count,
        &mut total,
        0,
    )?;
    #[cfg(not(unix))]
    copy_tree(source, destination, &mut count, &mut total)?;
    Ok(())
}

pub(super) fn import_authorized(
    data: &Path,
    id: &str,
    grant: &crate::desktop::DirectoryGrant,
) -> Result<String> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let meta = fs::symlink_metadata(&grant.path)
            .map_err(|_| bad("Authorized directory no longer exists"))?;
        if (meta.dev(), meta.ino()) != grant.identity
            || !meta.is_dir()
            || meta.file_type().is_symlink()
        {
            return Err(bad("Authorized directory changed"));
        }
    }
    let parent = grant
        .path
        .parent()
        .ok_or_else(|| bad("Invalid project directory"))?;
    let name = grant
        .path
        .file_name()
        .and_then(|x| x.to_str())
        .ok_or_else(|| bad("Invalid directory name"))?;
    import_inner(
        data,
        parent,
        id,
        &json!({"blank":false,"import_path":name}),
        Some(grant.identity),
    )
}

pub(super) fn import(data: &Path, imports: &Path, id: &str, config: &Value) -> Result<String> {
    import_inner(data, imports, id, config, None)
}

fn import_inner(
    data: &Path,
    imports: &Path,
    id: &str,
    config: &Value,
    expected_identity: Option<(u64, u64)>,
) -> Result<String> {
    let projects = data.join("projects");
    fs::create_dir_all(&projects).map_err(|_| bad("Cannot create project storage"))?;
    let destination = projects.join(id);
    if destination.exists() {
        return Err(conflict());
    }
    fs::create_dir(&destination).map_err(|_| bad("Cannot create project snapshot"))?;
    let result = (|| {
        if config["blank"] != true {
            let relative = Path::new(config["import_path"].as_str().unwrap_or(""));
            if relative.as_os_str().is_empty()
                || relative.is_absolute()
                || relative
                    .components()
                    .any(|x| !matches!(x, std::path::Component::Normal(_)))
            {
                return Err(bad(
                    "Project source must be relative to the import directory",
                ));
            }
            let imports = imports
                .canonicalize()
                .map_err(|_| bad("Import directory unavailable"))?;
            let source = imports.join(relative);
            let metadata =
                fs::symlink_metadata(&source).map_err(|_| bad("Project source unavailable"))?;
            if !metadata.is_dir()
                || metadata.file_type().is_symlink()
                || !source
                    .canonicalize()
                    .map_err(|_| bad("Invalid source"))?
                    .starts_with(&imports)
            {
                return Err(bad("Unsafe project source"));
            }
            let mut count = 0usize;
            let mut total = 0u64;
            #[cfg(unix)]
            {
                use std::os::unix::fs::MetadataExt;
                let opened = open_directory(&source)?;
                let meta = opened
                    .metadata()
                    .map_err(|_| bad("Cannot inspect import directory"))?;
                if expected_identity.is_some_and(|id| id != (meta.dev(), meta.ino())) {
                    return Err(bad("Authorized directory changed"));
                }
                copy_directory(&opened, &destination, &mut count, &mut total, 0)?;
            }
            #[cfg(not(unix))]
            let _ = expected_identity;
            #[cfg(not(unix))]
            copy_tree(&source, &destination, &mut count, &mut total)?;
        }
        git(&destination, &["init", "--quiet"])?;
        git(&destination, &["add", "--all"])?;
        git(
            &destination,
            &[
                "-c",
                "user.name=Forge",
                "-c",
                "user.email=forge@localhost",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--allow-empty",
                "-m",
                "Imported project baseline",
                "--quiet",
            ],
        )?;
        git(&destination, &["rev-parse", "HEAD"])
    })();
    if result.is_err() {
        let _ = fs::remove_dir_all(&destination);
    }
    result
}
fn git(root: &Path, args: &[&str]) -> Result<String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(args)
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .output()
        .map_err(|_| bad("Git is required to create the project snapshot"))?;
    if !output.status.success() {
        return Err(bad("Git could not create the project snapshot"));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().into())
}
pub(crate) fn excluded(name: &str) -> bool {
    let name = name.to_ascii_lowercase();
    [
        ".git",
        ".hg",
        ".svn",
        ".ssh",
        ".aws",
        ".azure",
        ".gcloud",
        ".codex",
        ".docker",
        ".kube",
        ".local",
        ".config",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".idea",
        ".next",
        "coverage",
        ".npmrc",
        ".pypirc",
        ".netrc",
    ]
    .contains(&name.as_str())
        || name == ".env"
        || name.starts_with(".env.")
        || name.starts_with("id_rsa")
        || name.starts_with("id_ed25519")
        || name.contains("credentials")
        || [".pem", ".key", ".p12", ".pfx"]
            .iter()
            .any(|s| name.ends_with(s))
}
#[cfg(not(unix))]
fn copy_tree(source: &Path, dest: &Path, count: &mut usize, total: &mut u64) -> Result<()> {
    for item in fs::read_dir(source).map_err(|_| bad("Cannot enumerate source"))? {
        let entry = item.map_err(|_| bad("Cannot read source entry"))?;
        let name = entry.file_name();
        if excluded(&name.to_string_lossy()) {
            continue;
        }
        let meta = entry
            .path()
            .symlink_metadata()
            .map_err(|_| bad("Cannot inspect source entry"))?;
        if meta.file_type().is_symlink() {
            continue;
        }
        let target = dest.join(&name);
        if meta.is_dir() {
            fs::create_dir(&target).map_err(|_| bad("Cannot create snapshot directory"))?;
            copy_tree(&entry.path(), &target, count, total)?;
        } else if meta.is_file() {
            #[cfg(unix)]
            {
                use std::os::unix::fs::MetadataExt;
                if meta.nlink() != 1 {
                    return Err(bad("Hard links are not accepted in project imports"));
                }
            }
            *count += 1;
            *total += meta.len();
            if *count > 30000 || *total > 100 * 1024 * 1024 {
                return Err(bad(
                    "Project snapshot exceeds the 100 MB or 30000 file limit",
                ));
            }
            let mut options = fs::OpenOptions::new();
            options.read(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
            }
            let mut input = options
                .open(entry.path())
                .map_err(|_| bad("Cannot safely open project file"))?;
            let opened = input
                .metadata()
                .map_err(|_| bad("Cannot inspect open project file"))?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::MetadataExt;
                if opened.dev() != meta.dev()
                    || opened.ino() != meta.ino()
                    || opened.nlink() != 1
                    || !opened.is_file()
                {
                    return Err(bad("Source changed during import"));
                }
            }
            let mut output = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(target)
                .map_err(|_| bad("Cannot create snapshot file"))?;
            std::io::copy(
                &mut std::io::Read::take(&mut input, meta.len() + 1),
                &mut output,
            )
            .map_err(|_| bad("Cannot copy snapshot file"))?;
            if output
                .metadata()
                .map_err(|_| bad("Cannot inspect snapshot file"))?
                .len()
                != meta.len()
            {
                return Err(bad("Source file changed during import"));
            }
        } else {
            return Err(bad("Special files cannot be imported"));
        }
    }
    Ok(())
}

#[cfg(unix)]
pub(crate) fn open_directory(path: &Path) -> Result<fs::File> {
    use std::os::{
        fd::{AsRawFd, FromRawFd},
        unix::ffi::OsStrExt,
    };
    let mut directory = fs::File::open("/").map_err(|_| bad("Cannot open filesystem root"))?;
    for component in path.components() {
        let std::path::Component::Normal(name) = component else {
            if component == std::path::Component::RootDir {
                continue;
            }
            return Err(bad("Import directory must be an absolute normalized path"));
        };
        let name =
            std::ffi::CString::new(name.as_bytes()).map_err(|_| bad("Invalid directory name"))?;
        // Each component is opened relative to the pinned parent, without following links.
        let fd = unsafe {
            libc::openat(
                directory.as_raw_fd(),
                name.as_ptr(),
                libc::O_RDONLY | libc::O_CLOEXEC | libc::O_NOFOLLOW | libc::O_DIRECTORY,
            )
        };
        if fd < 0 {
            return Err(bad("Cannot safely open import directory"));
        }
        directory = unsafe { fs::File::from_raw_fd(fd) };
    }
    Ok(directory)
}

#[cfg(unix)]
fn directory_names(directory: &fs::File) -> Result<Vec<std::ffi::OsString>> {
    use std::os::{fd::IntoRawFd, unix::ffi::OsStrExt};
    struct Listing(*mut libc::DIR);
    impl Drop for Listing {
        fn drop(&mut self) {
            unsafe {
                libc::closedir(self.0);
            }
        }
    }
    let fd = directory
        .try_clone()
        .map_err(|_| bad("Cannot enumerate source directory"))?
        .into_raw_fd();
    // fdopendir owns the duplicate descriptor on success; Listing closes it on every exit.
    let dir = unsafe { libc::fdopendir(fd) };
    if dir.is_null() {
        unsafe {
            libc::close(fd);
        }
        return Err(bad("Cannot enumerate source directory"));
    }
    let listing = Listing(dir);
    let mut names = Vec::new();
    loop {
        #[cfg(target_os = "macos")]
        unsafe {
            *libc::__error() = 0;
        }
        #[cfg(target_os = "linux")]
        unsafe {
            *libc::__errno_location() = 0;
        }
        let entry = unsafe { libc::readdir(listing.0) };
        if entry.is_null() {
            if std::io::Error::last_os_error()
                .raw_os_error()
                .is_some_and(|code| code != 0)
            {
                return Err(bad("Source directory changed during enumeration"));
            }
            break;
        }
        let name = unsafe { std::ffi::CStr::from_ptr((*entry).d_name.as_ptr()) }.to_bytes();
        if name == b"." || name == b".." {
            continue;
        }
        names.push(std::ffi::OsStr::from_bytes(name).to_owned());
        if names.len() > 30000 {
            return Err(bad("Source directory exceeds entry limit"));
        }
    }
    Ok(names)
}

#[cfg(unix)]
fn copy_directory(
    source: &fs::File,
    dest: &Path,
    count: &mut usize,
    total: &mut u64,
    depth: usize,
) -> Result<()> {
    use std::os::{
        fd::{AsRawFd, FromRawFd},
        unix::{
            ffi::OsStrExt,
            fs::{MetadataExt, PermissionsExt},
        },
    };
    if depth > 64 {
        return Err(bad("Project directory nesting exceeds 64 levels"));
    }
    for name in directory_names(source)? {
        if excluded(&name.to_string_lossy()) {
            continue;
        }
        let c_name =
            std::ffi::CString::new(name.as_bytes()).map_err(|_| bad("Invalid source name"))?;
        let fd = unsafe {
            libc::openat(
                source.as_raw_fd(),
                c_name.as_ptr(),
                libc::O_RDONLY | libc::O_CLOEXEC | libc::O_NOFOLLOW | libc::O_NONBLOCK,
            )
        };
        if fd < 0 {
            if std::io::Error::last_os_error().raw_os_error() == Some(libc::ELOOP) {
                continue;
            }
            return Err(bad("Cannot safely open source entry"));
        }
        let mut input = unsafe { fs::File::from_raw_fd(fd) };
        let meta = input
            .metadata()
            .map_err(|_| bad("Cannot inspect source entry"))?;
        *count += 1;
        if *count > 30000 {
            return Err(bad("Project snapshot exceeds 30000 entries"));
        }
        let target = dest.join(name);
        if meta.is_dir() {
            fs::create_dir(&target).map_err(|_| bad("Cannot create snapshot directory"))?;
            copy_directory(&input, &target, count, total, depth + 1)?;
        } else if meta.is_file() {
            if meta.nlink() != 1 {
                return Err(bad("Hard links are not accepted in project imports"));
            }
            *total += meta.len();
            if *total > 100 * 1024 * 1024 {
                return Err(bad("Project snapshot exceeds 100 MB"));
            }
            let mut output = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&target)
                .map_err(|_| bad("Cannot create snapshot file"))?;
            let copied = std::io::copy(
                &mut std::io::Read::take(&mut input, meta.len() + 1),
                &mut output,
            )
            .map_err(|_| bad("Cannot copy snapshot file"))?;
            if copied != meta.len() {
                return Err(bad("Source file changed during import"));
            }
            output
                .set_permissions(fs::Permissions::from_mode(meta.mode() & 0o777))
                .map_err(|_| bad("Cannot preserve snapshot file permissions"))?;
        } else {
            return Err(bad("Special files cannot be imported"));
        }
    }
    Ok(())
}
