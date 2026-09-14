use super::*;
use std::io::{Cursor, Read, Write};

const MAX: usize = 1_500_000;
pub(super) fn validate_bindings(c: &Connection, v: &Value) -> Result<()> {
    let items = v.as_array().ok_or_else(|| bad("skills must be an array"))?;
    if items.len() > 12 {
        return Err(bad("Too many bound skills"));
    }
    let mut seen = BTreeSet::new();
    for b in items {
        let id = string(b, "skill_id", 1, 32)?;
        if !seen.insert(id) {
            return Err(bad("Duplicate skill binding"));
        }
        if b["enabled"] == false {
            continue;
        }
        let version = b["version"]
            .as_u64()
            .ok_or_else(|| bad("Skill version must be a positive integer"))?;
        let enabled=c.query_row("SELECT s.enabled,r.status FROM skills s JOIN skill_revisions r ON r.skill_id=s.id WHERE s.id=?1 AND r.version=?2",params![id,version],|r|Ok((r.get::<_,bool>(0)?,r.get::<_,String>(1)?))).optional()?.ok_or_else(||bad("Skill version does not exist"))?;
        if !enabled.0 || enabled.1 != "approved" {
            return Err(bad("Skill must be enabled and its version approved"));
        }
    }
    Ok(())
}
pub(super) fn request(
    c: &Connection,
    u: &User,
    m: &str,
    p: &[&str],
    q: &BTreeMap<String, String>,
    b: &Value,
    raw: &[u8],
) -> Result<ProductReply> {
    let (status, v) = match (m, p) {
        ("GET", ["skills"]) => {
            let mut s = c.prepare("SELECT id,latest_version FROM skills ORDER BY name")?;
            let ids = s
                .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, u64>(1)?)))?
                .collect::<std::result::Result<Vec<_>, _>>()?;
            (
                200,
                json!(ids
                    .into_iter()
                    .map(|(id, v)| get(c, &id, Some(v), false))
                    .collect::<Result<Vec<_>>>()?),
            )
        }
        ("POST", ["skills"]) => {
            fields(b, &["files", "expected_version"])?;
            (201, save(c, u, None, None, &b["files"])?)
        }
        ("POST", ["skills", "import"]) => (201, save(c, u, None, None, &import_zip(raw)?)?),
        ("GET", ["skills", id]) => {
            let version = q
                .get("version")
                .map(|v| v.parse::<u64>().map_err(|_| bad("Invalid skill version")))
                .transpose()?;
            let mut v = get(c, id, version, true)?;
            let mut s=c.prepare("SELECT version,status,digest FROM skill_revisions WHERE skill_id=?1 ORDER BY version DESC")?;
            v["versions"]=json!(s.query_map([id],|r|Ok(json!({"version":r.get::<_,u64>(0)?,"status":r.get::<_,String>(1)?,"digest":r.get::<_,String>(2)?})))?.collect::<std::result::Result<Vec<_>,_>>()?);
            (200, v)
        }
        ("PUT", ["skills", id]) => {
            fields(b, &["files", "expected_version"])?;
            (
                200,
                save(c, u, Some(id), b["expected_version"].as_u64(), &b["files"])?,
            )
        }
        ("POST", ["skills", id, "review"]) => {
            fields(b, &["version", "digest", "action"])?;
            let version = b["version"]
                .as_u64()
                .ok_or_else(|| bad("Invalid skill version"))?;
            let old = get(c, id, Some(version), true)?;
            if b["digest"] != old["digest"]
                || digest(&old["files"]) != old["digest"].as_str().unwrap_or("")
            {
                return Err(conflict());
            }
            let approved = match b["action"].as_str() {
                Some("approve") => true,
                Some("revoke") => false,
                _ => return Err(bad("Skill action must be approve or revoke")),
            };
            c.execute("UPDATE skill_revisions SET status=?1,approved_by=?2 WHERE skill_id=?3 AND version=?4",params![if approved{"approved"}else{"revoked"},u.id,id,version])?;
            emit(
                c,
                u,
                "skill.reviewed",
                json!({"skill_id":id,"version":version,"approved":approved,"digest":b["digest"]}),
            )?;
            (200, get(c, id, Some(version), true)?)
        }
        ("POST", ["skills", id, "enabled"]) => {
            fields(b, &["enabled"])?;
            let enabled = bool_or(b, "enabled", true)?;
            if c.execute(
                "UPDATE skills SET enabled=?1 WHERE id=?2",
                params![enabled, id],
            )? != 1
            {
                return Err(missing());
            }
            emit(
                c,
                u,
                "skill.enabled",
                json!({"skill_id":id,"enabled":enabled}),
            )?;
            (200, json!({"ok":true}))
        }
        ("GET", ["skills", id, "export"]) => {
            let version = q
                .get("version")
                .map(|v| v.parse::<u64>().map_err(|_| bad("Invalid skill version")))
                .transpose()?;
            let v = get(c, id, version, true)?;
            let mut zip = zip::ZipWriter::new(Cursor::new(Vec::new()));
            let options = zip::write::SimpleFileOptions::default()
                .compression_method(zip::CompressionMethod::Deflated);
            for (name, text) in object(&v["files"])? {
                zip.start_file(
                    format!("{}/{name}", v["name"].as_str().unwrap_or("skill")),
                    options,
                )
                .map_err(|_| bad("Cannot export skill archive"))?;
                zip.write_all(text.as_str().unwrap_or("").as_bytes())
                    .map_err(|_| bad("Cannot export skill archive"))?;
            }
            let bytes = zip
                .finish()
                .map_err(|_| bad("Cannot export skill archive"))?
                .into_inner();
            return Ok(ProductReply {
                status: 200,
                body: Value::Null,
                download: Some(ProductDownload {
                    bytes,
                    filename: format!(
                        "{}-v{}.zip",
                        v["name"].as_str().unwrap_or("skill"),
                        v["version"]
                    ),
                    content_type: "application/zip",
                }),
            });
        }
        _ => return Err(missing()),
    };
    Ok(ProductReply::json(status, v))
}
pub(super) fn get(c: &Connection, id: &str, version: Option<u64>, full: bool) -> Result<Value> {
    c.query_row("SELECT s.name,s.enabled,s.latest_version,r.version,r.digest,r.status,r.manifest,r.files,r.created_at FROM skills s JOIN skill_revisions r ON r.skill_id=s.id AND r.version=COALESCE(?2,s.latest_version) WHERE s.id=?1",params![id,version],|r|{let mut manifest=decode(r.get(6)?)?;let description=manifest["description"].clone();manifest.as_object_mut().map(|v|v.remove("body"));let mut v=json!({"id":id,"name":r.get::<_,String>(0)?,"enabled":r.get::<_,bool>(1)?,"latest_version":r.get::<_,u64>(2)?,"version":r.get::<_,u64>(3)?,"digest":r.get::<_,String>(4)?,"status":r.get::<_,String>(5)?,"description":description,"manifest":manifest,"created_at":r.get::<_,String>(8)?});if full{v["files"]=decode(r.get(7)?)?}Ok(v)}).optional()?.ok_or_else(missing)
}
pub(super) fn save(
    c: &Connection,
    u: &User,
    id: Option<&str>,
    expected: Option<u64>,
    files: &Value,
) -> Result<Value> {
    let (clean, manifest) = validate_files(files)?;
    let name = manifest["name"].as_str().unwrap_or("");
    let id = id.map(str::to_owned).unwrap_or_else(uid);
    let version = if let Some(expected) = expected {
        let old = get(c, &id, None, false)?;
        if old["name"] != name || old["latest_version"] != expected {
            return Err(conflict());
        }
        if c.execute(
            "UPDATE skills SET latest_version=?1 WHERE id=?2 AND latest_version=?3",
            params![expected + 1, id, expected],
        )? != 1
        {
            return Err(conflict());
        }
        expected + 1
    } else {
        c.execute(
            "INSERT INTO skills(id,name,enabled,latest_version) VALUES(?1,?2,1,1)",
            params![id, name],
        )?;
        1
    };
    let sum = digest(&clean);
    c.execute("INSERT INTO skill_revisions(id,skill_id,version,digest,manifest,files,status,approved_by,created_at) VALUES(?1,?2,?3,?4,?5,?6,'draft','',?7)",params![format!("{id}:{version}"),id,version,sum,encode(&manifest),encode(&clean),now(c)?])?;
    emit(
        c,
        u,
        "skill.revised",
        json!({"skill_id":id,"version":version,"digest":sum}),
    )?;
    get(c, &id, Some(version), true)
}
pub(super) fn validate_files(files: &Value) -> Result<(Value, Value)> {
    let files = object(files)?;
    if files.is_empty() || files.len() > 80 || !files.contains_key("SKILL.md") {
        return Err(bad("Skill requires SKILL.md and at most 80 files"));
    }
    let mut clean = Map::new();
    let mut total = 0;
    for (name, content) in files {
        let text = content
            .as_str()
            .ok_or_else(|| bad("Skill resources must be UTF-8 text"))?;
        let ext = Path::new(name)
            .extension()
            .and_then(|x| x.to_str())
            .unwrap_or("")
            .to_ascii_lowercase();
        if name.len() > 200
            || name.contains(['\\', ':', '\0'])
            || name.split('/').any(|x| x.is_empty() || x.starts_with('.'))
            || ![
                "md", "txt", "json", "yaml", "yml", "py", "sh", "js", "ts", "csv", "toml", "xml",
                "html", "css",
            ]
            .contains(&ext.as_str())
        {
            return Err(bad("Unsafe skill resource path"));
        }
        total += text.len();
        if text.len() > 200000 || total > MAX || text.contains('\0') {
            return Err(bad("Skill resource size or text is invalid"));
        }
        clean.insert(name.clone(), json!(text.replace("\r\n", "\n")));
    }
    let text = clean["SKILL.md"].as_str().unwrap();
    let header = text
        .strip_prefix("---\n")
        .and_then(|s| s.split_once("\n---\n"))
        .ok_or_else(|| bad("SKILL.md requires YAML frontmatter"))?;
    if header.0.len() > 16000 || header.1.trim().is_empty() {
        return Err(bad("Invalid SKILL.md content"));
    }
    let metadata: serde_yaml::Value =
        serde_yaml::from_str(header.0).map_err(|_| bad("Invalid skill frontmatter"))?;
    let mut manifest = serde_json::to_value(metadata)
        .map_err(|_| bad("Skill frontmatter must contain JSON-compatible values"))?;
    let name = string(&manifest, "name", 1, 64)?;
    if name.split('-').any(|s| {
        s.is_empty()
            || !s
                .bytes()
                .all(|x| x.is_ascii_lowercase() || x.is_ascii_digit())
    }) {
        return Err(bad("Invalid Skill name"));
    }
    string(&manifest, "description", 1, 1024)?;
    manifest["body"] = json!(header.1.trim());
    manifest["files"]=json!(clean.iter().map(|(name,text)|json!({"path":name,"bytes":text.as_str().unwrap().len(),"sha256":format!("{:x}",Sha256::digest(text.as_str().unwrap().as_bytes()))})).collect::<Vec<_>>());
    Ok((Value::Object(clean), manifest))
}
fn import_zip(raw: &[u8]) -> Result<Value> {
    if raw.len() > MAX {
        return Err(bad("Skill ZIP exceeds 1.5 MB"));
    }
    let mut archive =
        zip::ZipArchive::new(Cursor::new(raw)).map_err(|_| bad("Invalid skill ZIP"))?;
    if archive.len() > 100 {
        return Err(bad("Too many skill ZIP entries"));
    }
    let mut files = Map::new();
    let mut total = 0;
    for i in 0..archive.len() {
        let entry = archive
            .by_index(i)
            .map_err(|_| bad("Unreadable ZIP entry"))?;
        if entry.is_dir() {
            continue;
        }
        if entry.is_symlink() || entry.encrypted() || entry.size() > 200000 {
            return Err(bad("Unsafe ZIP entry"));
        }
        total += entry.size();
        if total > MAX as u64 {
            return Err(bad("Decompressed ZIP exceeds 1.5 MB"));
        }
        let name = entry.name().to_owned();
        let mut text = String::new();
        entry
            .take(200001)
            .read_to_string(&mut text)
            .map_err(|_| bad("ZIP entry is not UTF-8"))?;
        if text.len() > 200000 || files.insert(name, json!(text)).is_some() {
            return Err(bad("Duplicate or oversized ZIP entry"));
        }
    }
    if !files.contains_key("SKILL.md") {
        let roots: Vec<_> = files
            .keys()
            .filter_map(|p| {
                p.strip_suffix("/SKILL.md")
                    .filter(|prefix| !prefix.contains('/'))
            })
            .collect();
        if roots.len() != 1 {
            return Err(bad("ZIP requires a unique Skill root"));
        }
        let prefix = format!("{}/", roots[0]);
        let mut stripped = Map::new();
        for (name, text) in files {
            let name = name
                .strip_prefix(&prefix)
                .ok_or_else(|| bad("ZIP contains files outside its Skill root"))?;
            stripped.insert(name.into(), text);
        }
        files = stripped;
    }
    Ok(validate_files(&Value::Object(files))?.0)
}
