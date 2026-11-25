import hashlib
import json
import shutil
import subprocess
from pathlib import Path

FILES = ("index.html", "tasks.js", "authorization.py")


def digest(source: Path) -> str:
    h = hashlib.sha256()
    for name in (*FILES, "backend.py"):
        h.update(name.encode())
        h.update((source / name).read_bytes())
    return h.hexdigest()


def build(kind: str, destination: Path) -> Path:
    if kind not in ("clean", "visibility", "ownership"):
        raise ValueError("Unknown demo build")
    if destination.exists():
        raise ValueError("Build already exists; choose a new destination")
    destination.mkdir(parents=True)
    template = Path(__file__).parent / "application"
    for name in FILES:
        shutil.copy2(template / name, destination / name)
    shutil.copy2(Path(__file__).with_name("server.py"), destination / "backend.py")
    if kind == "visibility":
        path = destination / "tasks.js"
        path.write_text(path.read_text().replace("return tasks;", "return tasks.filter(task => task.completed);"), encoding="utf-8")
    if kind == "ownership":
        path = destination / "authorization.py"
        path.write_text(path.read_text().replace("return task_owner == current_user", "return True"), encoding="utf-8")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    manifest = {"source_commit": commit, "source_digest": digest(destination)}
    # No seeded-defect label is exposed to the agents.
    manifest["app_version"] = manifest["source_digest"][:16]
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return destination
