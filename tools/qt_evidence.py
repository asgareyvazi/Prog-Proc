"""Hash every Qt release file and reject a manifest that does not match the logs.

The stability manifest covers one directory. This one covers the release
archive: collection, the versions probe, every regular attempt log, the
workbench summary, the stability summary, every stability attempt, shutdown
transcripts, and the stability manifest plus its checksum. ``evidence.sha256``
is written after the manifest, so it is not an entry in the manifest.

``--write`` creates a new manifest from the files that are already on disk and
then verifies that archive. A verification-only invocation does not rewrite
either manifest. A checksum that matches its JSON is not enough: every nested
entry is compared with the file it names.
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


def _unsafe(name: object) -> bool:
    if not isinstance(name, str) or not name or name.startswith(("/", "\\")):
        return True
    return "\\" in name or ".." in Path(name).parts


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _verify_nested_stability(root: Path, *, repeats: int, claimed_pass: bool) -> list[str]:
    """Check the nested manifest against the files, not only against its checksum."""
    stability = _load("qt_stability.py")
    workbench = _load("qt_workbench.py")
    problems: list[str] = []
    manifest_path = root / "stability" / "manifest.json"
    checksum_path = root / "stability" / "manifest.sha256"
    if not manifest_path.is_file() or not checksum_path.is_file():
        return ["stability manifest or checksum is missing"]
    recorded_hash = checksum_path.read_text(encoding="utf-8").split()
    actual_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if not recorded_hash or recorded_hash[0] != actual_hash:
        problems.append("stability manifest checksum does not match")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return [*problems, "stability manifest is not json"]
    if not isinstance(document, dict):
        return [*problems, "stability manifest root is not an object"]
    attempts = _integer(document.get("attempts"))
    expected_attempts = repeats * len(stability.HISTORIC_NODES)
    if attempts != expected_attempts:
        problems.append("stability manifest attempt count is wrong")
    nodes = document.get("nodes")
    if not isinstance(nodes, list):
        problems.append("stability manifest nodes is not a list")
        nodes = []
    seen_nodes: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            problems.append("stability manifest node is not an object")
            continue
        name = node.get("node")
        count = _integer(node.get("attempts"))
        failed = _integer(node.get("failed"))
        records = node.get("records")
        if not isinstance(name, str) or name in seen_nodes:
            problems.append("stability manifest node name is missing or duplicate")
            continue
        seen_nodes.add(name)
        if count != repeats or failed is None or not isinstance(records, list):
            problems.append(f"stability manifest node fields are wrong: {name}")
        if claimed_pass and failed not in (None, 0):
            problems.append(f"successful stability record has a failed node: {name}")
    if seen_nodes != set(stability.HISTORIC_NODES):
        problems.append("stability manifest nodes do not match the historic nodes")
    entries = document.get("files")
    if not isinstance(entries, list):
        return [*problems, "stability manifest has no file list"]
    listed: dict[str, dict[str, object]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            problems.append("stability manifest entry is not an object")
            continue
        name = entry.get("name")
        if _unsafe(name) or name in listed:
            problems.append("stability manifest path is duplicate or unsafe")
            continue
        listed[str(name)] = entry
    stability_root = root / "stability"
    on_disk = {
        path.relative_to(stability_root).as_posix(): path
        for path in stability_root.rglob("*")
        if path.is_file() and path.name not in {"manifest.json", "manifest.sha256"}
    }
    if set(listed) != set(on_disk):
        problems.append("stability manifest file list does not match the directory")
    for name, entry in listed.items():
        path = stability_root / name
        if not path.is_file():
            problems.append(f"stability manifest lists a missing file: {name}")
            continue
        size = path.stat().st_size
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if entry.get("bytes") != size or entry.get("sha256") != digest:
            problems.append(f"stability manifest entry does not match file: {name}")
    if claimed_pass:
        for node in stability.HISTORIC_NODES:
            node_dir = stability_root / stability.stability_dirname(node)
            for path in sorted(node_dir.glob("attempt-*.txt")) if node_dir.is_dir() else []:
                body = path.read_text(encoding="utf-8", errors="replace")
                header = body.splitlines()[0] if body else ""
                if "exit=0" not in header or "reason=-" not in header:
                    problems.append(f"successful record has a failed attempt: {path.name}")
                if any(signature in body for signature in (*_ABORT, *workbench.ABORT_SIGNATURES)):
                    problems.append(f"successful record contains an abort signature: {path.name}")
    return problems


def _verify_workbench_summary(root: Path, listed: dict[str, dict[str, object]]) -> list[str]:
    workbench = _load("qt_workbench.py")
    summary = root / "summary.txt"
    collection = root / "collection.txt"
    if not summary.is_file():
        return ["workbench summary is missing"]
    text = summary.read_text(encoding="utf-8")
    problems: list[str] = []
    collected_lines = [line for line in text.splitlines() if line.startswith("collected ")]
    executed_lines = [
        line for line in text.splitlines() if line.startswith("workbench tests executed:")
    ]
    if len(collected_lines) != 1 or len(executed_lines) != 1:
        return ["workbench summary count is missing or repeated"]
    try:
        collected = int(collected_lines[0].removeprefix("collected ").split()[0])
        executed = int(executed_lines[0].rsplit(" ", 1)[-1])
    except ValueError:
        return ["workbench summary count is not an integer"]
    attempts: dict[tuple[str, str], str] = {}
    results: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("NODE ") and " ATTEMPT " in line and " EXIT " in line:
            node, rest = line.removeprefix("NODE ").split(" ATTEMPT ", 1)
            ordinal, _, exit_code = rest.partition(" EXIT ")
            if not ordinal.isdigit() or not exit_code.lstrip("-").isdigit():
                problems.append("workbench attempt record is malformed")
                continue
            key = (node, ordinal)
            if key in attempts:
                problems.append(f"duplicate workbench attempt: {node} {ordinal}")
                continue
            attempts[key] = exit_code
            name = f"{workbench._safe(node)}.attempt-{ordinal}.txt"
            if name not in listed:
                problems.append(f"regular attempt log missing: {name}")
        elif line.startswith("NODE ") and " RESULT " in line:
            node, _, result = line.removeprefix("NODE ").partition(" RESULT ")
            if node in results:
                problems.append(f"duplicate workbench result: {node}")
            results[node] = result
    if collected != executed or collected != len(results):
        problems.append("workbench summary count does not match its node lines")
    if collection.is_file():
        collected_nodes = workbench.parse_collected_nodes(collection.read_text(encoding="utf-8"))
        if len(collected_nodes) != len(set(collected_nodes)) or set(collected_nodes) != set(
            results
        ):
            problems.append("workbench collection does not match the summary nodes")
    for node, result in results.items():
        first = attempts.get((node, "1"))
        second = attempts.get((node, "2"))
        if result == "FIRST_PASS" and (first != "0" or second is not None):
            problems.append(f"FIRST_PASS does not match attempts: {node}")
        elif result == "RETRY_PASS" and not (first == "134" and second == "0"):
            problems.append(f"RETRY_PASS does not match the one abort retry: {node}")
        elif result not in {"FIRST_PASS", "RETRY_PASS"}:
            problems.append(f"workbench result is not a pass: {node}")
        extra = [
            ordinal for (seen, ordinal) in attempts if seen == node and ordinal not in {"1", "2"}
        ]
        if extra:
            problems.append(f"unexpected workbench attempt ordinal: {node}")
    return problems


def _verify_transcripts(root: Path, listed: dict[str, dict[str, object]]) -> list[str]:
    problems: list[str] = []
    directory = root / "shutdown"
    if not directory.is_dir():
        return problems
    for path in sorted(directory.glob("*.json")):
        name = f"shutdown/{path.name}"
        if name not in listed:
            problems.append(f"shutdown transcript is not in the manifest: {name}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            problems.append(f"shutdown transcript is not json: {path.name}")
            continue
        if not isinstance(payload, dict):
            problems.append(f"shutdown transcript root is not an object: {path.name}")
            continue
        markers = payload.get("markers")
        exit_code = payload.get("exit_code")
        if (
            payload.get("mode") != path.stem
            or not isinstance(markers, list)
            or not all(isinstance(item, str) for item in markers)
            or isinstance(exit_code, bool)
            or not isinstance(exit_code, int)
        ):
            problems.append(f"shutdown transcript fields are wrong: {path.name}")
            continue
        if path.stem == "real-qthread":
            required = ("LOOP_STARTED", "QUIT_REQUESTED", "EXEC_RETURNED", "SHUTDOWN_CODE 0")
            positions = [markers.index(item) for item in required if item in markers]
            if len(positions) != len(required) or positions != sorted(positions):
                problems.append("real-qthread transcript is missing the live-loop order")
    summary = root / "summary.txt"
    needs_transcript = summary.is_file() and (
        "test_real_qthread_external_quit_uses_application_shutdown"
        in summary.read_text(encoding="utf-8")
    )
    if needs_transcript and "shutdown/real-qthread.json" not in listed:
        problems.append("real-qthread transcript is missing from the release archive")
    return problems


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
    problems.extend(_verify_workbench_summary(root, listed))
    stability = _load("qt_stability.py")
    stability_summary = root / "stability" / "summary.txt"
    if not stability_summary.is_file():
        problems.append("stability summary is missing")
        claimed = False
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
    problems.extend(_verify_nested_stability(root, repeats=repeats, claimed_pass=claimed))
    problems.extend(_verify_transcripts(root, listed))
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
