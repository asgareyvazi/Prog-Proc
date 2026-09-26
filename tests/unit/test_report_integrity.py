"""The reports in ``docs/`` must not make claims that are false the moment they are written.

The V4.1 report stated "HEAD after this mission: ``f4b45fb…``".  That was unverifiable when it was
written and false as soon as it was committed, for a structural reason: committing the sentence that
names a commit necessarily produces a *different* commit.  A document cannot contain the SHA of the
commit that carries it.  The line sat in the repository as an authoritative-looking falsehood until
the V4.2 audit found it, and every reader who trusted it was pointed at the wrong tree.

Restating the figure does not fix it - the correction would move HEAD again.  The only stable repair
is to stop making the claim: a report keeps the *work* commit, which is a static fact, and leaves the
carrying commit to ``git log``.  So the invariant here is that no report makes the un-holdable claim
at all, and that every SHA a report does name really exists.

Nothing in this suite rewrites history.  The correction is a new commit that says so.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[2] / "docs"
GIT = shutil.which("git")

#: Commits that predate this clone's fetch boundary, and whose absence is therefore expected.
#:
#: Naming them is what keeps the shallow-clone skip honest.  Without the list, "the object is not
#: here" would excuse *any* hash a report invents, because a shallow clone can always say the commit
#: might have been before the boundary - and a hash for a commit that was never pushed (the V4.3
#: work was committed locally as ``4ab2c452`` and never reached the remote) is exactly the case the
#: skip would otherwise hide.  With the list, an unlisted absent SHA fails here too.
PRE_BOUNDARY_SHAS = frozenset(
    {
        "b7703baf35abd84881432a93264a979c33751c6c",  # V3 baseline
        "921f89be4fdb03421cf02fec17011d4bbbd28368",  # human-review report
        "b2e76fad64e9b8acac187af45eb9bb8d9d1175c5",  # V3 ingestion certification
    }
)

#: The header form of the un-holdable claim: ``**HEAD after this mission:** `<40-hex>```.
HEAD_AFTER_CLAIM = re.compile(r"HEAD after this mission:\*\*\s*`([0-9a-f]{40})`")
#: Any 40-hex SHA in backticks, which is how every report cites a tree.
ANY_SHA = re.compile(r"`([0-9a-f]{40})`")


def _git(*args: str) -> tuple[int, str]:
    done = subprocess.run(
        [GIT, *args], cwd=DOCS.parent, capture_output=True, text=True, timeout=60, check=False
    )
    return done.returncode, done.stdout.strip()


def _reports() -> list[Path]:
    return sorted(DOCS.glob("*.md"))


def _is_shallow() -> bool:
    """A shallow clone cannot contain commits before its boundary, so absence proves nothing."""
    code, out = _git("rev-parse", "--is-shallow-repository")
    return code == 0 and out.strip() == "true"


pytestmark = pytest.mark.skipif(GIT is None, reason="git is not available")


def test_the_docs_directory_is_where_this_suite_thinks_it_is() -> None:
    assert DOCS.is_dir(), DOCS
    assert _reports(), "no markdown reports found"


def test_no_report_claims_a_head_sha_it_cannot_know() -> None:
    """A document cannot name the commit that carries it, so it must not try.

    This is the defect the V4.2 audit found in the V4.1 report.  It is not a typo to be re-typed with
    the right hash: any hash written here is stale by the time the file is committed.  Reports state
    the work commit and leave HEAD to ``git log``.
    """
    offenders = {
        path.name: match.group(1)
        for path in _reports()
        if (match := HEAD_AFTER_CLAIM.search(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "these reports claim a HEAD SHA they cannot know - committing the claim changes HEAD: "
        f"{offenders}.  State the *work* commit instead."
    )


def test_every_sha_a_report_names_exists_in_the_repository() -> None:
    """A SHA that is not in the object database is a typo wearing a uniform.

    A shallow clone is the one case where absence is not evidence: commits before the boundary were
    never fetched, so the V3 baseline is legitimately unreachable there.  Such a clone skips rather
    than passing silently - the strict form of this check is what runs on a complete checkout.
    """
    missing: dict[str, list[str]] = {}
    for path in _reports():
        for sha in set(ANY_SHA.findall(path.read_text(encoding="utf-8"))):
            # ``rev-parse --verify`` succeeds for a well-formed hash even when the object is absent;
            # ``cat-file -e`` is the check that actually looks it up.
            code, _out = _git("cat-file", "-e", f"{sha}^{{commit}}")
            if code != 0:
                missing.setdefault(path.name, []).append(sha)
    if missing and _is_shallow():
        pytest.skip(
            "shallow clone: these SHAs predate the fetch boundary, so their absence is unprovable "
            f"rather than a defect: {missing}"
        )
    assert not missing, missing


def test_an_absent_sha_that_is_not_pre_boundary_fails_even_in_a_shallow_clone() -> None:
    """The shallow-clone concession is narrow: it excuses the boundary, not a typo.

    ``test_every_sha_a_report_names_exists_in_the_repository`` skips wholesale when the clone is
    shallow, which is right for the three commits above and wrong for anything else - a hash for a
    commit that was never pushed is indistinguishable from a pre-boundary one to that test.  This
    one does not skip: an absent SHA outside :data:`PRE_BOUNDARY_SHAS` is a defect wherever it is
    found, shallow clone or not.
    """
    offenders: dict[str, list[str]] = {}
    for path in _reports():
        for sha in set(ANY_SHA.findall(path.read_text(encoding="utf-8"))):
            if sha in PRE_BOUNDARY_SHAS:
                continue
            code, _out = _git("cat-file", "-e", f"{sha}^{{commit}}")
            if code != 0:
                offenders.setdefault(path.name, []).append(sha)
    assert not offenders, (
        "these reports name commits that are not in the object database and are not pre-boundary, "
        f"so they never existed: {offenders}.  Cite the short form and say so in prose instead."
    )


def test_the_v3_baseline_is_recorded_as_not_an_ancestor() -> None:
    """Reports compare against ``b7703baf``; none of them may imply it is on this branch."""
    code, head = _git("rev-parse", "HEAD")
    assert code == 0 and head, "HEAD could not be resolved"

    baseline = "b7703baf35abd84881432a93264a979c33751c6c"
    code, _out = _git("cat-file", "-e", f"{baseline}^{{commit}}")
    if code != 0:
        # Not a defect in the report: a shallow clone never fetched it, so "not an ancestor" can be
        # neither confirmed nor denied here.  Skipping says so; passing would be a lie.
        pytest.skip("shallow clone: the V3 baseline predates the fetch boundary")

    # ``merge-base --is-ancestor`` prints nothing and exits 0 when the first argument is an ancestor.
    code, _out = _git("merge-base", "--is-ancestor", baseline, head)
    assert code != 0, "the V3 baseline is documented as *not* an ancestor of HEAD"
