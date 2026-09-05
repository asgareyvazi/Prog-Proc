"""``python -m drilling_intelligence.cli`` - the same command as the ``drillintel`` script.

The console script in ``pyproject.toml`` is what an installed checkout gets; running the package
is what a source tree gets without installing anything.  Both have to work, because the tests and
a developer's first five minutes both use the second form.
"""

from __future__ import annotations

import sys

from .app import main

if __name__ == "__main__":
    sys.exit(main())
