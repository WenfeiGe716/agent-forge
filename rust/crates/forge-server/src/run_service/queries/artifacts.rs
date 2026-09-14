use super::*;

// Serialized artifacts reserve ZIP framing space beyond the 100 MiB source budget.
pub(crate) const MAX_BYTES: u64 = 128 * 1024 * 1024;

pub(crate) fn read(root: &Path, name: &str) -> Result<Vec<u8>> {
    let relative = Path::new(name);
    if relative.components().count() != 1
        || !matches!(relative.components().next(), Some(Component::Normal(_)))
    {
        return Err(BackendError::NotFound);
    }
    #[cfg(unix)]
    let file = {
        use std::os::fd::{AsRawFd, FromRawFd};
        let directory = crate::product_api::workspace::open_directory(root)
            .map_err(|_| BackendError::NotFound)?;
        let name = std::ffi::CString::new(name).map_err(|_| BackendError::NotFound)?;
        let fd = unsafe {
            libc::openat(
                directory.as_raw_fd(),
                name.as_ptr(),
                libc::O_RDONLY | libc::O_CLOEXEC | libc::O_NOFOLLOW | libc::O_NONBLOCK,
            )
        };
        if fd < 0 {
            return Err(BackendError::NotFound);
        }
        unsafe { fs::File::from_raw_fd(fd) }
    };
    #[cfg(not(unix))]
    let file = {
        let path = root.join(relative);
        let meta = path
            .symlink_metadata()
            .map_err(|_| BackendError::NotFound)?;
        if !meta.is_file() || meta.file_type().is_symlink() {
            return Err(BackendError::NotFound);
        }
        fs::File::open(path).map_err(io)?
    };
    let metadata = file.metadata().map_err(io)?;
    if !metadata.is_file() {
        return Err(BackendError::NotFound);
    }
    if metadata.len() > MAX_BYTES {
        return Err(BackendError::InvalidData);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if metadata.nlink() != 1 {
            return Err(BackendError::NotFound);
        }
    }
    let mut bytes = Vec::new();
    file.take(MAX_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(io)?;
    if bytes.len() as u64 != metadata.len() {
        return Err(BackendError::Conflict);
    }
    Ok(bytes)
}
