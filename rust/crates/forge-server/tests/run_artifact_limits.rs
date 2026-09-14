use forge_server::{Backend, ProductStore, RustOwnedBackend};
use std::{collections::BTreeMap, fs, io::Write, path::PathBuf, sync::Arc};

struct Temp(PathBuf);
impl Drop for Temp {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[tokio::test]
async fn source_archive_above_twenty_mib_downloads_with_bounded_safe_file_access() {
    let root = Temp(std::env::temp_dir().join(format!(
        "forge-large-artifact-{}",
        uuid::Uuid::new_v4().simple()
    )));
    fs::create_dir_all(&root.0).unwrap();
    let product = Arc::new(
        ProductStore::open(
            root.0.join("product.sqlite"),
            None,
            Some("large-artifact-test-password"),
        )
        .unwrap(),
    );
    let user = product
        .check_credentials("admin", "large-artifact-test-password")
        .unwrap()
        .unwrap();
    let backend = RustOwnedBackend::from_product(
        product.clone(),
        root.0.join("kernel.sqlite"),
        root.0.join("data"),
        None,
    )
    .unwrap();
    let c = product.connect().unwrap();
    c.execute(
        "INSERT INTO projects VALUES('project',?1,'{}','','2026-01-01')",
        [&user.id],
    )
    .unwrap();
    c.execute("INSERT INTO runs VALUES('run','project',?1,'Run','Request','completed','{}',NULL,'','','{}',0,0,0,'','2026-01-01','2026-01-01')",[&user.id]).unwrap();
    let artifacts = root.0.join("data/runs/run/artifacts");
    fs::create_dir_all(&artifacts).unwrap();
    let file = fs::File::create(artifacts.join("source.zip")).unwrap();
    let mut zip = zip::ZipWriter::new(file);
    let block = vec![b'a'; 1024 * 1024];
    for index in 0..21 {
        zip.start_file(
            format!("file-{index}.bin"),
            zip::write::SimpleFileOptions::default()
                .compression_method(zip::CompressionMethod::Stored),
        )
        .unwrap();
        zip.write_all(&block).unwrap();
    }
    zip.finish().unwrap();
    let response = backend
        .product_request(
            &user,
            "GET",
            "/api/runs/run/artifacts/source.zip",
            &BTreeMap::new(),
            &[],
        )
        .unwrap();
    let download = response.download.unwrap();
    assert!(download.bytes.len() > 20 * 1024 * 1024);
    assert_eq!(download.content_type, "application/zip");
    let archive = zip::ZipArchive::new(std::io::Cursor::new(download.bytes)).unwrap();
    assert_eq!(archive.len(), 21);
    let sparse = fs::File::create(artifacts.join("oversize.zip")).unwrap();
    sparse.set_len(128 * 1024 * 1024 + 1).unwrap();
    assert_eq!(
        backend
            .product_request(
                &user,
                "GET",
                "/api/runs/run/artifacts/oversize.zip",
                &BTreeMap::new(),
                &[]
            )
            .unwrap_err()
            .status,
        422
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::symlink;
        fs::write(root.0.join("private.txt"), "private").unwrap();
        symlink(root.0.join("private.txt"), artifacts.join("link.txt")).unwrap();
        assert_eq!(
            backend
                .product_request(
                    &user,
                    "GET",
                    "/api/runs/run/artifacts/link.txt",
                    &BTreeMap::new(),
                    &[]
                )
                .unwrap_err()
                .status,
            404
        );
        fs::hard_link(root.0.join("private.txt"), artifacts.join("hard.txt")).unwrap();
        assert_eq!(
            backend
                .product_request(
                    &user,
                    "GET",
                    "/api/runs/run/artifacts/hard.txt",
                    &BTreeMap::new(),
                    &[]
                )
                .unwrap_err()
                .status,
            404
        );
    }
    let viewer = product
        .check_credentials("admin", "large-artifact-test-password")
        .unwrap()
        .unwrap();
    c.execute("UPDATE users SET enabled=0 WHERE id=?1", [&user.id])
        .unwrap();
    let error = backend
        .product_request(
            &viewer,
            "GET",
            "/api/runs/run/artifacts/source.zip",
            &BTreeMap::new(),
            &[],
        )
        .unwrap_err();
    assert!(matches!(error.status, 401 | 403 | 404));
}
