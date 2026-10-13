"""Hash every Qt release file and reject a manifest that does not match the logs.

The stability manifest covers one directory. This one covers the release
archive: collection, the versions probe, every regular attempt log, the
workbench summary, the stability summary, every stability attempt, and the
stability manifest plus its checksum. ``evidence.sha256`` is written after
the manifest, so it is not an entry in the manifest. A verifier that rebuilds
the manifest from a summary is not this tool: the logs are the source, and a
byte that does not match the recorded hash fails the gate.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

_MANIFEST = "evidence.json"
_CHECKSUM = "evidence.sha256"
_REQUIRED = ("collection.txt", "versions.txt", "summary.txt")
_ABORT = (
    "QObject::killTimer",
    "Cannot create children for a parent that is in a different thread",
    "Fatal Python error: Aborted",
    "QObject::setParent: Cannot set parent, new parent is in a different thread",
)


def _load(name: str):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _relative(path: Path, root: Path) -> str:
    text = path.relative_to(root).as_posix()
    if text.startswith("/") or ".." in Path(text).parts:
        raise ValueError(f"path escapes the archive: {text}")
    return text


def _files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def write_manifest(root: Path) -> Path:
    """Hash the archive as it is. Does not invent attempt records."""
    root = root.resolve()
    files = []
    seen: set[str] = set()
    for path in _files(root):
        name = _relative(path, root)
        if name in {_MANIFEST, _CHECKSUM}:
            continue
        if name in seen:
            raise ValueError(f"duplicate archive path: {name}")
        seen.add(name)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append({"path": name, "bytes": path.stat().st_size, "sha256": digest})
    document = {"files": files}
    target = root / _MANIFEST
    target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    (root / _CHECKSUM).write_text(f"{checksum}  {_MANIFEST}\n", encoding="utf-8")
    return target


def verify_archive(root: Path, *, repeats: int) -> list[str]:
    """Return every problem. An empty list is a matching archive."""
    root = root.resolve()
    problems: list[str] = []
    manifest_path = root / _MANIFEST
    checksum_path = root / _CHECKSUM
    if not manifest_path.is_file() or not checksum_path.is_file():
        return ["evidence manifest or its checksum is missing"]
    recorded = checksum_path.read_text(encoding="utf-8").split()
    actual = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if not recorded or recorded[0] != actual:
        problems.append("evidence checksum does not match evidence.json")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return [*problems, "evidence.json is not json"]
    entries = document.get("files") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        return [*problems, "evidence.json has no file list"]
    listed: dict[str, dict[str, object]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            problems.append("evidence entry is not an object")
            continue
        name = entry.get("path")
        if (
            not isinstance(name, str)
            or name in listed
            or ".." in Path(name).parts
            or name.startswith("/")
        ):
            problems.append(f"evidence path is duplicate or unsafe: {name}")
            continue
        listed[name] = entry
    on_disk = {
        _relative(path, root): path
        for path in _files(root)
        if path.name not in {_MANIFEST, _CHECKSUM}
    }
    if set(listed) != set(on_disk):
        problems.append("evidence file list does not match the archive")
    for name, entry in listed.items():
        path = root / name
        if not path.is_file():
            problems.append(f"missing {name}")
            continue
        size = path.stat().st_size
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if entry.get("bytes") != size or entry.get("sha256") != digest:
            problems.append(f"hash or size mismatch: {name}")
    for name in _REQUIRED:
        if name not in listed:
            problems.append(f"required release file missing: {name}")
    versions = root / "versions.txt"
    if versions.is_file() and not versions.read_text(encoding="utf-8").strip():
        problems.append("versions.txt is empty")
    stability = _load("qt_stability.py")
    workbench = _load("qt_workbench.py")
    summary = root / "summary.txt"
    if summary.is_file():
        text = summary.read_text(encoding="utf-8")
        nodes: list[str] = []
        for line in text.splitlines():
            if not line.startswith("NODE ") or " ATTEMPT " not in line:
                continue
            node, rest = line.removeprefix("NODE ").split(" ATTEMPT ", 1)
            nodes.append(node)
            attempt = rest.split(" ", 1)[0]
            name = f"{workbench._safe(node)}.attempt-{attempt}.txt"
            if name not in listed:
                problems.append(f"regular attempt log missing: {name}")
        executed = [
            line for line in text.splitlines() if line.startswith("workbench tests executed:")
        ]
        if executed and int(executed[-1].rsplit(" ", 1)[-1]) != len(set(nodes)):
            problems.append("workbench summary count does not match its node lines")
    stability_summary = root / "stability" / "summary.txt"
    if not stability_summary.is_file():
        problems.append("stability summary is missing")
    else:
        text = stability_summary.read_text(encoding="utf-8")
        claimed = "stability result: PASS" in text
        for node in stability.HISTORIC_NODES:
            if node not in text:
                problems.append(f"stability summary omits {node}")
            node_dir = root / "stability" / stability.stability_dirname(node)
            attempts = sorted(node_dir.glob("attempt-*.txt")) if node_dir.is_dir() else []
            expected = [f"attempt-{ordinal:02d}.txt" for ordinal in range(1, repeats + 1)]
            found = [path.name for path in attempts]
            if found != expected:
                problems.append(f"{node} has {found}, expected {expected}")
            if not claimed:
                continue
            for path in attempts:
                body = path.read_text(encoding="utf-8", errors="replace")
                header = body.splitlines()[0] if body else ""
                if "exit=0" not in header or "reason=-" not in header:
                    problems.append(f"successful record has a failed attempt: {path.name}")
                if any(signature in body for signature in (*_ABORT, *workbench.ABORT_SIGNATURES)):
                    problems.append(f"successful record contains an abort signature: {path.name}")
        manifest = root / "stability" / "manifest.json"
        checksum = root / "stability" / "manifest.sha256"
        if manifest.is_file() and checksum.is_file():
            recorded_hash = checksum.read_text(encoding="utf-8").split()
            if (
                not recorded_hash
                or recorded_hash[0] != hashlib.sha256(manifest.read_bytes()).hexdigest()
            ):
                problems.append("stability manifest checksum does not match")
        else:
            problems.append("stability manifest or checksum is missing")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("--repeats", type=int, default=15)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.archive)
    if args.write:
        write_manifest(root)
    problems = verify_archive(root, repeats=args.repeats)
    if problems:
        print("qt evidence: FAIL")
        for problem in problems:
            print(problem)
        return 1
    print("qt evidence: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
