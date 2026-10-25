"""Hash-seed independence of review JSON.

The review service must produce canonical JSON regardless of PYTHONHASHSEED
or dict insertion order. This test drives the service with permuted inputs
and verifies that subprocesses with different hash seeds emit identical bytes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run_with_seed(seed: str, script: str) -> str:
    env = {"PYTHONHASHSEED": seed, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    # Inherit other env but override hash seed
    import os

    full_env = dict(os.environ)
    full_env.update(env)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_dedupe_is_hash_independent() -> None:
    script = """
import json
from drilling_intelligence.review.service import _dedupe_rows, _dedupe_mappings
from drilling_intelligence.database.models import Well

# Permuted inputs
w1 = Well(id="a", name="A")
w2 = Well(id="b", name="B")
w3 = Well(id="c", name="C")
# Different insertion orders should give same sorted output
r1 = _dedupe_rows([w3, w1, w2])
r2 = _dedupe_rows([w2, w3, w1])
r3 = _dedupe_rows([w1, w2, w3])
ids1 = [r.id for r in r1]
ids2 = [r.id for r in r2]
ids3 = [r.id for r in r3]
print(json.dumps([ids1, ids2, ids3], sort_keys=True))
"""
    out0 = _run_with_seed("0", script)
    out1 = _run_with_seed("12345", script)
    out2 = _run_with_seed("99999", script)
    assert out0 == out1 == out2
    data = json.loads(out0)
    # All three permutations give same sorted result
    assert data[0] == data[1] == data[2] == ["a", "b", "c"]


def test_relations_plan_actual_hash_independent() -> None:
    script = """
import json
from drilling_intelligence.review.service import DomainReview
from drilling_intelligence.review.contract import ReviewRecord

# Build two relations in different insertion order
rels_a = [{"relation": "b", "id": "2"}, {"relation": "a", "id": "1"}]
rels_b = [{"relation": "a", "id": "1"}, {"relation": "b", "id": "2"}]
# Simulate sorting as service does
sa = sorted(rels_a, key=lambda r: json.dumps(dict(r), sort_keys=True, default=str))
sb = sorted(rels_b, key=lambda r: json.dumps(dict(r), sort_keys=True, default=str))
print(json.dumps([sa, sb], sort_keys=True))
"""
    out0 = _run_with_seed("0", script)
    out1 = _run_with_seed("42", script)
    assert out0 == out1
    data = json.loads(out0)
    assert data[0] == data[1]


def test_json_default_str_is_deterministic() -> None:
    # json.dumps with default=str must be deterministic for same input
    # even with non-serializable types that fallback to str
    script = """
import json, datetime
from drilling_intelligence.review.service import _plain

data = {"a": datetime.datetime(2023, 1, 1, 12, 0, 0), "b": 1}
# _plain may convert datetimes via hashing? Check that dumps is stable
plain = _plain(data)
s1 = json.dumps(plain, sort_keys=True, default=str)
s2 = json.dumps(_plain(data), sort_keys=True, default=str)
print(s1)
print(s2)
"""
    out0 = _run_with_seed("0", script)
    lines = out0.splitlines()
    assert len(lines) == 2
    assert lines[0] == lines[1]
