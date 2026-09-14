use super::*;
use base64::{engine::general_purpose::STANDARD, Engine};

const SOURCE: &str = r#"import json, re
from pathlib import Path

def run(relative):
    root = Path.cwd().resolve()
    path = Path(relative)
    if path.is_absolute() or not relative or ".." in path.parts or "\\" in relative:
        raise ValueError("Input must be a relative workspace text file")
    cursor = root
    for part in path.parts:
        if part.startswith(".") or part in ("node_modules", "venv", "__pycache__"):
            raise ValueError("Hidden and environment files are excluded")
        cursor = cursor / part
        if cursor.is_symlink(): raise ValueError("Symlinks are excluded")
    if not cursor.resolve().is_relative_to(root) or not cursor.is_file() or cursor.stat().st_nlink != 1:
        raise ValueError("Input must be a regular workspace file")
    if cursor.stat().st_size > 200000: raise ValueError("File exceeds 200 KB")
    text = cursor.read_text(encoding="utf-8")
    return {"path": relative, "characters": len(text), "lines": len(text.splitlines()),
            "words": len(re.findall(r"\S+", text)),
            "headings": sum(bool(re.match(r"^#{1,6}\s", line)) for line in text.splitlines())}
"#;

const BOOTSTRAP: &str = r#"import base64, hashlib, json, os, stat
from pathlib import Path
c = json.loads(base64.b64decode(CONFIG))
root = Path.cwd().resolve()
folder = root
for part in (".forge-tools", c["id"], c["version"]):
    folder = folder / part
    if folder.is_symlink(): raise ValueError("Unsafe tool directory")
    if c["install"]: folder.mkdir(exist_ok=True)
    if not folder.is_dir() or not folder.resolve().is_relative_to(root): raise ValueError("Tool not installed")
target = folder / "forge_text_tools.py"
if target.is_symlink(): raise ValueError("Unsafe tool file")
if c["install"]:
    raw = base64.b64decode(c["source"])
    if hashlib.sha256(raw).hexdigest() != c["digest"]: raise ValueError("Source digest mismatch")
    if not target.exists():
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o444)
        with os.fdopen(fd, "wb") as f: f.write(raw)
info = target.lstat()
if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 20000: raise ValueError("Unsafe installed file")
raw = target.read_bytes()
if hashlib.sha256(raw).hexdigest() != c["digest"]: raise ValueError("Installed tool digest mismatch")
if c["install"]:
    print(json.dumps({"installed": True, "package_id": c["id"], "version": c["version"], "sha256": c["digest"]}))
else:
    scope = {"__name__": "forge_text_tools"}
    exec(compile(raw, "approved-offline-tool", "exec"), scope)
    print(json.dumps(scope["run"](c["path"]), ensure_ascii=False))
"#;

pub(super) fn package() -> Value {
    let sha = format!("{:x}", Sha256::digest(SOURCE.as_bytes()));
    // Preserve the shipped Python manifest byte-for-byte, including its command.
    let config = format!("{{\"id\": \"forge-text-tools\", \"version\": \"1.0.0\", \"digest\": \"{sha}\", \"install\": true, \"path\": \"\", \"source\": \"{}\"}}", STANDARD.encode(SOURCE));
    let script = format!("CONFIG='{}'\n{BOOTSTRAP}", STANDARD.encode(config));
    let command = format!("python3 -I -c '{}'", script.replace('\'', "'\"'\"'"));
    json!({"id":"forge-text-tools","name":"\u{6587}\u{672c}\u{4e0e} Markdown \u{7edf}\u{8ba1}","version":"1.0.0","source":"bundled://agent-forge/forge-text-tools/1.0.0","language":"python","sha256":sha,"license":"project-owned","permissions":["read one approved workspace text file","write fixed package directory during installation"],"network":"none","requires":["python3","run_command","explicit project execution backend"],"operations":["text_metrics"],"files":{"forge_text_tools.py":SOURCE},"install_command":command})
}
