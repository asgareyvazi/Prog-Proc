#!/usr/bin/env bash
# Single installation contract for the Qt-enabled pytest collection job and
# every Qt full-suite shard job (Phase B2).
#
# The collection job and all shard jobs MUST resolve the identical dependency
# set; otherwise the canonical collection manifest digest legitimately differs
# and the shard plugin correctly refuses to run (fail-closed). Do not "simplify"
# one of the call sites back to a floating `pip install -e .[dev,ui]`.
set -euo pipefail

python -m pip install --upgrade pip
python -m pip install -c constraints/ui.txt -c constraints/build.txt -e ".[dev,ui]"
python -m pip freeze --all > ci-install-fingerprint.txt
sha256sum constraints/ui.txt constraints/build.txt >> ci-install-fingerprint.txt
