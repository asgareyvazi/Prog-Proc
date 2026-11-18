"""Standalone stability evidence verifier regression tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _make_valid_stability(tmp_path: Path, repeats=2, nodes=None):
    """Create a minimal valid stability directory for testing.

    Uses the same layout as tools/qt_stability.py: every selected historic node has repeats attempts.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("qt_stability", Path("tools/qt_stability.py"))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    root = tmp_path / "stability"
    root.mkdir(parents=True, exist_ok=True)
    selected_nodes = tuple(mod.HISTORIC_NODES if nodes is None else nodes)
    nodes = []
    for node in selected_nodes:
        dirname = mod.stability_dirname(node)
        node_dir = root / dirname
        node_dir.mkdir(parents=True, exist_ok=True)
        records = []
        for ordinal in range(1, repeats + 1):
            log_rel = f"{dirname}/attempt-{ordinal:02d}.txt"
            log_path = root / log_rel
            header = f"ordinal={ordinal} node={node} exit=0 duration_s=0.100 reason=-"
            log_path.write_text(header + "\noutput\n", encoding="utf-8")
            sha = hashlib.sha256(log_path.read_bytes()).hexdigest()
            records.append(
                {
                    "ordinal": ordinal,
                    "exit": 0,
                    "duration_s": 0.1,
                    "reason": "",
                    "log": log_rel,
                    "sha256": sha,
                }
            )
        nodes.append({"node": node, "attempts": repeats, "failed": 0, "records": records})

    # summary.txt (before manifest)
    lines = []
    for node in selected_nodes:
        lines.append(f"NODE {node}")
        lines.append(f"stability attempts: {repeats}")
        for ordinal in range(1, repeats + 1):
            lines.append(f"ATTEMPT {ordinal} EXIT 0 PASS")
        lines.append("stability failed: 0")
        lines.append("stability result: PASS")
    lines.append(f"stability nodes: {len(selected_nodes)}")
    lines.append("stability nodes failed: 0")
    lines.append("stability result: PASS")
    (root / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Now write manifest including summary
    files = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name in {"manifest.json", "manifest.sha256"}:
            continue
        rel = p.relative_to(root).as_posix()
        files.append(
            {
                "name": rel,
                "bytes": p.stat().st_size,
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            }
        )
    manifest = {
        "attempts": repeats * len(selected_nodes),
        "files": files,
        "nodes": nodes,
        "versions": {"python": "3.11.2", "pyside": "6.0", "qt": "6.0"},
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (root / "manifest.sha256").write_text(f"{digest}  manifest.json\n", encoding="utf-8")
    return root, mod


def test_valid_stability_passes(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "qt_stability_evidence", Path("tools/qt_stability_evidence.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    root, _ = _make_valid_stability(tmp_path, repeats=2)
    problems = mod.verify_stability_dir(root, repeats=2)
    assert problems == [], f"expected PASS, got {problems}"


def test_single_node_evidence_passes_only_when_that_node_is_expected(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "qt_stability_evidence", Path("tools/qt_stability_evidence.py")
    )
    assert spec and spec.loader
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)

    _root, stability = _make_valid_stability(tmp_path, repeats=2, nodes=None)
    node = stability.HISTORIC_NODES[0]
    one_node_root, _ = _make_valid_stability(
        tmp_path / "single-node",
        repeats=2,
        nodes=(node,),
    )
    assert (
        verifier.verify_stability_dir(
            one_node_root,
            repeats=2,
            expected_nodes=(node,),
        )
        == []
    )
    full_set_problems = verifier.verify_stability_dir(one_node_root, repeats=2)
    assert any("manifest attempts 2 != expected 6" in problem for problem in full_set_problems)


def test_expected_node_selection_cannot_be_empty_duplicated_or_invented(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "qt_stability_evidence", Path("tools/qt_stability_evidence.py")
    )
    assert spec and spec.loader
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)

    node = verifier.HISTORIC_NODES[0]
    root = tmp_path / "missing-evidence"
    assert verifier.verify_stability_dir(root, expected_nodes=()) == [
        "no expected stability nodes were configured"
    ]
    assert verifier.verify_stability_dir(root, expected_nodes=(node, node)) == [
        "expected stability nodes contain duplicates"
    ]
    assert verifier.verify_stability_dir(root, expected_nodes=("invented::node",)) == [
        "unexpected expected stability nodes: ['invented::node']"
    ]


def test_missing_log_fails(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "qt_stability_evidence", Path("tools/qt_stability_evidence.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    root, st_mod = _make_valid_stability(tmp_path, repeats=2)
    # Delete one log
    first_node = st_mod.HISTORIC_NODES[0]
    dirname = st_mod.stability_dirname(first_node)
    (root / dirname / "attempt-01.txt").unlink()
    problems = mod.verify_stability_dir(root, repeats=2)
    assert problems
    assert any("missing log" in p.lower() or "missing" in p.lower() for p in problems)


def test_duplicate_ordinal_fails(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "qt_stability_evidence", Path("tools/qt_stability_evidence.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    root, _ = _make_valid_stability(tmp_path, repeats=2)
    # Corrupt manifest: duplicate ordinal 1 twice
    manifest_path = root / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Duplicate first record of first node
    data["nodes"][0]["records"].append(data["nodes"][0]["records"][0].copy())
    manifest_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # Update checksum to match new manifest (so checksum error not triggered)
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (root / "manifest.sha256").write_text(f"{digest}  manifest.json\n", encoding="utf-8")
    problems = mod.verify_stability_dir(root, repeats=2)
    assert problems


def test_corrupted_hash_fails(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "qt_stability_evidence", Path("tools/qt_stability_evidence.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    root, st_mod = _make_valid_stability(tmp_path, repeats=2)
    first_node = st_mod.HISTORIC_NODES[0]
    dirname = st_mod.stability_dirname(first_node)
    # Corrupt a log file after manifest was written
    p = root / dirname / "attempt-01.txt"
    p.write_text("corrupted\n", encoding="utf-8")
    problems = mod.verify_stability_dir(root, repeats=2)
    assert problems
    assert any("hash" in p.lower() for p in problems)


def test_extra_file_fails(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "qt_stability_evidence", Path("tools/qt_stability_evidence.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    root, _ = _make_valid_stability(tmp_path, repeats=2)
    # Add an extra file not in manifest
    (root / "extra.txt").write_text("extra", encoding="utf-8")
    problems = mod.verify_stability_dir(root, repeats=2)
    assert problems


def test_reordered_ordinals_fails(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "qt_stability_evidence", Path("tools/qt_stability_evidence.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    root, _ = _make_valid_stability(tmp_path, repeats=2)
    manifest_path = root / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Swap ordinals 1 and 2 for first node
    recs = data["nodes"][0]["records"]
    recs[0], recs[1] = recs[1], recs[0]
    manifest_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (root / "manifest.sha256").write_text(f"{digest}  manifest.json\n", encoding="utf-8")
    problems = mod.verify_stability_dir(root, repeats=2)
    assert problems


def test_empty_directory_fails(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "qt_stability_evidence", Path("tools/qt_stability_evidence.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    empty = tmp_path / "empty"
    empty.mkdir()
    problems = mod.verify_stability_dir(empty, repeats=2)
    assert problems


def test_missing_manifest_fails(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "qt_stability_evidence", Path("tools/qt_stability_evidence.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    root, _ = _make_valid_stability(tmp_path, repeats=2)
    (root / "manifest.json").unlink()
    problems = mod.verify_stability_dir(root, repeats=2)
    assert problems
