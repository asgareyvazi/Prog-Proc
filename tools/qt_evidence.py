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
import math
import re
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


def _finite(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value) and value >= 0


def _ordered(markers: list[str], required: tuple[str, ...]) -> bool:
    position = -1
    for item in required:
        if item not in markers:
            return False
        found = markers.index(item)
        if found <= position:
            return False
        position = found
    return True


# Child-process exit, then the markers that must appear in that order.
# A scenario can observe Qt exit 4 or 7 while the child process itself returns 0.
_TRANSCRIPTS: dict[str, tuple[int, tuple[str, ...]]] = {
    "refused": (0, ("REFUSED_KEPT_ALIVE", "SHUTDOWN_OK")),
    "quit-idle": (0, ("LOOP_STARTED", "QUIT_REQUESTED", "EXEC_RETURNED", "IDLE_QUIT_OK")),
    "quit-active": (0, ("LOOP_STARTED", "QUIT_REQUESTED", "EXEC_RETURNED", "EXTERNAL_QUIT_OK")),
    "quit-several": (0, ("LOOP_STARTED", "QUIT_REQUESTED", "EXEC_RETURNED", "SEVERAL_OK")),
    "last-window": (
        0,
        (
            "LOOP_STARTED",
            "CLOSE_REFUSED",
            "CLOSE_AGAIN_ACCEPTED",
            "EXEC_RETURNED",
            "LAST_WINDOW_OK",
        ),
    ),
    "exit-route": (
        0,
        ("LOOP_STARTED", "EXIT_REQUESTED", "EXEC_RETURNED", "SHUTDOWN_CODE 7", "EXIT_ROUTE_OK"),
    ),
    "deadline": (4, ("REAL_QTHREAD", "LOOP_STARTED", "QUIT_REQUESTED", "EXEC_RETURNED")),
    "deadline-synthetic": (4, ("SYNTHETIC_DEADLINE",)),
    "bound": (0, ("BOUNDED_INCOMPLETE", "RESUME_OK")),
    "real-qthread": (
        0,
        (
            "LOOP_STARTED",
            "QUIT_REQUESTED",
            "EXEC_RETURNED",
            "SHUTDOWN_CODE 0",
            "REAL_QTHREAD_QUIT_OK",
        ),
    ),
    "quit-queued": (0, ("LOOP_STARTED", "QUIT_REQUESTED", "EXEC_RETURNED", "QUEUED_QUIT_OK")),
    "quit-gui": (0, ("LOOP_STARTED", "GUI_THREAD_MATCH", "EXEC_RETURNED", "GUI_QUIT_OK")),
    "quit-before": (0, ("PRE_EXEC_QUIT", "PRE_EXEC_NOT_A_LIVE_LOOP_PROOF")),
    "real-several": (0, ("LOOP_STARTED", "QUIT_REQUESTED", "EXEC_RETURNED", "REAL_SEVERAL_OK")),
    "finish-race": (0, ("LOOP_STARTED", "QUIT_REQUESTED", "EXEC_RETURNED", "RACE_OK")),
    "repeat": (0, ("EXEC_RETURNED", "SHUTDOWN_CODE 0", "AFTER_REPEAT_QUIT", "REPEAT_OK")),
    "pump-block": (4, ("PUMP_ARMED", "PUMP_BLOCKED")),
    "pump-cpu": (4, ("CPU_ARMED", "CPU_BLOCKED")),
}
_FAILURE_MODES = {"deadline", "deadline-synthetic", "pump-block", "pump-cpu"}
_HEADER = re.compile(r"ordinal=(\d+) node=(\S+) exit=(-?\d+) duration_s=(\S+) reason=(.*)$")
_ATTEMPT_LINE = re.compile(r"ATTEMPT (\d+) EXIT (-?\d+) (PASS|FAIL)(?: (.*))?$")


class _SummaryAttempt:
    def __init__(self, ordinal: int, exit_code: int, state: str, reason: str) -> None:
        self.ordinal = ordinal
        self.exit_code = exit_code
        self.state = state
        self.reason = reason


class _SummaryNode:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attempts: list[_SummaryAttempt] = []
        self.failed = 0
        self.result = ""


class _SummaryFooter:
    def __init__(self, nodes: int, failed: int, result: str) -> None:
        self.nodes = nodes
        self.failed = failed
        self.result = result


def _parse_stability_summary(
    text: str, *, repeats: int, expected_nodes: set[str]
) -> tuple[dict[str, _SummaryNode], _SummaryFooter | None, list[str]]:
    """Parse node sections. A matching line in another section is not evidence."""
    problems: list[str] = []
    lines = text.splitlines()
    sections: dict[str, _SummaryNode] = {}
    index = 0
    while index < len(lines) and lines[index].startswith("NODE "):
        name = lines[index].removeprefix("NODE ")
        index += 1
        if not name or name in sections:
            problems.append("stability summary node is missing or duplicate")
        section = _SummaryNode(name or "<missing>")
        if name and name not in sections:
            sections[name] = section
        if index >= len(lines) or not lines[index].startswith("stability attempts: "):
            problems.append(f"stability summary attempt count is missing: {section.name}")
            break
        declared = lines[index].removeprefix("stability attempts: ")
        if not declared.isdigit() or int(declared) != repeats:
            problems.append(f"stability summary attempt count is wrong: {section.name}")
        index += 1
        while index < len(lines) and lines[index].startswith("ATTEMPT "):
            match = _ATTEMPT_LINE.match(lines[index])
            index += 1
            if match is None:
                problems.append(f"stability summary attempt line is malformed: {section.name}")
                continue
            ordinal = int(match.group(1))
            if section.attempts and ordinal != section.attempts[-1].ordinal + 1:
                problems.append(f"stability summary attempts are out of order: {section.name}")
            if (
                any(item.ordinal == ordinal for item in section.attempts)
                or not 1 <= ordinal <= repeats
            ):
                problems.append(f"stability summary attempt ordinal is wrong: {section.name}")
            section.attempts.append(
                _SummaryAttempt(ordinal, int(match.group(2)), match.group(3), match.group(4) or "")
            )
        if index >= len(lines) or not lines[index].startswith("stability failed: "):
            problems.append(f"stability summary node failure count is missing: {section.name}")
            break
        failed_text = lines[index].removeprefix("stability failed: ")
        index += 1
        if not failed_text.isdigit():
            problems.append(f"stability summary node failure count is wrong: {section.name}")
            failed = -1
        else:
            failed = int(failed_text)
        section.failed = failed
        if index >= len(lines) or not lines[index].startswith("stability result: "):
            problems.append(f"stability summary node result is missing: {section.name}")
            break
        result = lines[index].removeprefix("stability result: ")
        index += 1
        if result not in {"PASS", "FAIL"}:
            problems.append(f"stability summary node result is wrong: {section.name}")
        section.result = result
        actual_failed = sum(1 for item in section.attempts if item.state == "FAIL")
        if (
            failed != actual_failed
            or (result == "PASS" and actual_failed != 0)
            or (result == "FAIL" and actual_failed == 0)
            or [item.ordinal for item in section.attempts] != list(range(1, repeats + 1))
        ):
            problems.append(
                f"stability summary node result does not match its attempts: {section.name}"
            )
    if index >= len(lines) or not lines[index].startswith("stability nodes: "):
        problems.append("stability summary footer is missing")
        return sections, None, problems
    nodes_text = lines[index].removeprefix("stability nodes: ")
    index += 1
    if index >= len(lines) or not lines[index].startswith("stability nodes failed: "):
        problems.append("stability summary footer is missing")
        return sections, None, problems
    failed_nodes_text = lines[index].removeprefix("stability nodes failed: ")
    index += 1
    if index >= len(lines) or not lines[index].startswith("stability result: "):
        problems.append("stability summary terminal result is missing")
        return sections, None, problems
    terminal = lines[index].removeprefix("stability result: ")
    index += 1
    if index != len(lines):
        problems.append("stability summary has extra lines")
    if (
        not nodes_text.isdigit()
        or not failed_nodes_text.isdigit()
        or terminal not in {"PASS", "FAIL"}
    ):
        problems.append("stability summary footer is malformed")
        return sections, None, problems
    footer = _SummaryFooter(int(nodes_text), int(failed_nodes_text), terminal)
    node_failures = sum(1 for section in sections.values() if section.failed)
    if footer.nodes != len(expected_nodes) or set(sections) != expected_nodes:
        problems.append("stability summary nodes do not match the historic nodes")
    if (
        footer.failed != node_failures
        or (terminal == "PASS" and (footer.failed or node_failures))
        or (terminal == "FAIL" and footer.failed == 0)
    ):
        problems.append("stability summary terminal result contradicts its nodes")
    return sections, footer, problems


def _verify_attempt_records(
    stability_root: Path,
    stability: object,
    node: str,
    records: list[object],
    section: _SummaryNode | None,
    *,
    repeats: int,
    failed: int,
    claimed_pass: bool,
) -> list[str]:
    """Match every nested attempt record to its own node section and log."""
    problems: list[str] = []
    dirname = stability.stability_dirname(node)  # type: ignore[attr-defined]
    if section is None:
        problems.append(f"stability summary omits {node}")
    seen: set[int] = set()
    failed_records = 0
    for record in records:
        if not isinstance(record, dict):
            problems.append(f"stability attempt record is not an object: {node}")
            continue
        ordinal = _integer(record.get("ordinal"))
        exit_code = _integer(record.get("exit"))
        reason = record.get("reason")
        log_name = record.get("log")
        digest = record.get("sha256")
        expected_log = f"{dirname}/attempt-{ordinal:02d}.txt" if ordinal is not None else ""
        if (
            ordinal is None
            or ordinal < 1
            or ordinal > repeats
            or ordinal in seen
            or exit_code is None
            or not _finite(record.get("duration_s"))
            or not isinstance(reason, str)
            or _unsafe(log_name)
            or log_name != expected_log
            or not isinstance(digest, str)
        ):
            problems.append(f"stability attempt record fields are wrong: {node}")
            continue
        seen.add(ordinal)
        path = stability_root / str(log_name)
        if not path.is_file():
            problems.append(f"stability attempt log is missing: {log_name}")
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            problems.append(f"stability attempt record hash does not match: {log_name}")
            continue
        header = path.read_text(encoding="utf-8", errors="replace").splitlines()
        match = _HEADER.match(header[0]) if header else None
        duration = float(match.group(4)) if match else None
        header_reason = (
            "" if match and match.group(5) == "-" else (match.group(5) if match else None)
        )
        if (
            match is None
            or int(match.group(1)) != ordinal
            or match.group(2) != node
            or int(match.group(3)) != exit_code
            or duration is None
            or not math.isclose(duration, float(record["duration_s"]), rel_tol=0, abs_tol=0.001)
            or header_reason != reason
        ):
            problems.append(f"stability attempt record does not match its log: {log_name}")
        if match is not None and match.group(2) != node:
            problems.append(f"stability attempt log names the wrong node: {log_name}")
        summary_attempt = (
            next((item for item in section.attempts if item.ordinal == ordinal), None)
            if section is not None
            else None
        )
        if (
            summary_attempt is None
            or summary_attempt.exit_code != exit_code
            or summary_attempt.reason != reason
            or (summary_attempt.state == "FAIL") != bool(reason)
        ):
            problems.append(f"stability attempt record does not match the summary: {log_name}")
        if reason:
            failed_records += 1
        if claimed_pass and (exit_code != 0 or reason):
            problems.append(f"successful stability record has a failed attempt: {path.name}")
    ordinals = [
        _integer(record.get("ordinal")) if isinstance(record, dict) else None for record in records
    ]
    if ordinals != list(range(1, repeats + 1)):
        problems.append(f"stability attempt records are out of order: {node}")
    if seen != set(range(1, repeats + 1)) or failed_records != failed:
        problems.append(f"stability attempt records do not match the node summary: {node}")
    return problems


def _version_token(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, str) or not value.strip():
        return None
    if value.strip().lower() == "unavailable":
        return None
    return value.strip()


def _toolkit_tokens(text: str) -> dict[str, str]:
    tokens = text.split()
    found: dict[str, str] = {}
    for index, token in enumerate(tokens[:-1]):
        if token in {"pyside", "qt", "python"} and token not in found:
            found[token] = tokens[index + 1]
    return found


def _verify_version_metadata(root: Path, versions: object, *, claimed_pass: bool) -> list[str]:
    """A successful Qt run must name Python, PySide6, and Qt. ``unavailable`` is not a success."""
    problems: list[str] = []
    if not isinstance(versions, dict) or _version_token(versions.get("python")) is None:
        problems.append("stability manifest versions are missing or malformed")
        versions = {}
    workbench = root / "summary.txt"
    workbench_text = workbench.read_text(encoding="utf-8") if workbench.is_file() else ""
    claimed_qt = claimed_pass or any(
        token in workbench_text for token in ("RESULT FIRST_PASS", "RESULT RETRY_PASS")
    )
    if not claimed_qt or not isinstance(versions, dict):
        return problems
    versions_file = root / "versions.txt"
    file_tokens = (
        _toolkit_tokens(versions_file.read_text(encoding="utf-8"))
        if versions_file.is_file()
        else {}
    )
    for key in ("python", "pyside", "qt"):
        manifest_token = _version_token(versions.get(key)) if isinstance(versions, dict) else None
        file_token = _version_token(file_tokens.get(key))
        if manifest_token is None or file_token is None:
            problems.append(f"successful Qt archive is missing a {key} version")
        elif manifest_token != file_token:
            problems.append(f"successful Qt archive {key} versions do not agree")
    return problems


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
    versions = document.get("versions")
    stability_root = root / "stability"
    summary_path = stability_root / "summary.txt"
    summary_text = summary_path.read_text(encoding="utf-8") if summary_path.is_file() else ""
    sections, footer, summary_problems = _parse_stability_summary(
        summary_text, repeats=repeats, expected_nodes=set(stability.HISTORIC_NODES)
    )
    problems.extend(summary_problems)
    if footer is not None and footer.result == "PASS":
        claimed_pass = True
    elif summary_problems:
        claimed_pass = False
    problems.extend(_verify_version_metadata(root, versions, claimed_pass=claimed_pass))
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
            continue
        problems.extend(
            _verify_attempt_records(
                stability_root,
                stability,
                name,
                records,
                sections.get(name),
                repeats=repeats,
                failed=failed,
                claimed_pass=claimed_pass,
            )
        )
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
        elif result == "RETRY_PASS":
            log_name = f"{workbench._safe(node)}.attempt-1.txt"
            body = ""
            log_path = root / log_name
            if log_path.is_file():
                body = log_path.read_text(encoding="utf-8", errors="replace")
            if (
                first != "134"
                or second != "0"
                or not workbench.qualifies_for_abort_retry(134, body)
            ):
                problems.append(f"RETRY_PASS is not an eligible abort retry: {node}")
        elif result not in {"FIRST_PASS", "RETRY_PASS"}:
            problems.append(f"workbench result is not a pass: {node}")
        extra = [
            ordinal for (seen, ordinal) in attempts if seen == node and ordinal not in {"1", "2"}
        ]
        if extra:
            problems.append(f"unexpected workbench attempt ordinal: {node}")
    return problems


def _verify_transcript_files(directory: Path, listed: dict[str, dict[str, object]]) -> list[str]:
    problems: list[str] = []
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
        contract = _TRANSCRIPTS.get(path.stem)
        schema = payload.get("schema_version", 1)
        if (
            contract is None
            or isinstance(schema, bool)
            or not isinstance(schema, int)
            or schema != 1
            or exit_code != contract[0]
            or not _finite(payload.get("duration_s"))
            or payload.get("result") != "observed"
            or not _ordered(markers, contract[1])
        ):
            problems.append(f"shutdown transcript contract failed: {path.name}")
            continue
        if path.stem in _FAILURE_MODES and (
            "SHUTDOWN_CODE 0" in markers or "REAL_QTHREAD_QUIT_OK" in markers
        ):
            problems.append(f"shutdown failure was recorded as a clean shutdown: {path.name}")
        if path.stem == "quit-before" and "REAL_QTHREAD_QUIT_OK" in markers:
            problems.append("quit-before transcript claims a live-loop proof")
        if path.stem == "quit-gui" and "GUI_THREAD_MISS" in markers:
            problems.append("quit-gui transcript ran on the wrong thread")
    return problems


def _verify_transcripts(root: Path, listed: dict[str, dict[str, object]]) -> list[str]:
    problems: list[str] = []
    directory = root / "shutdown"
    if directory.is_dir():
        problems.extend(_verify_transcript_files(directory, listed))
    summary = root / "summary.txt"
    summary_text = summary.read_text(encoding="utf-8") if summary.is_file() else ""
    if "tests/ui/test_qt_shutdown.py" in summary_text:
        for mode in _TRANSCRIPTS:
            if f"shutdown/{mode}.json" not in listed:
                problems.append(f"required shutdown transcript is missing: {mode}")
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
        claimed = "stability result: PASS" in text and "stability result: FAIL" not in text
        if "RETRY_PASS" in text:
            problems.append("stability summary claims a retry")
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
