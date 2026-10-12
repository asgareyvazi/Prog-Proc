"""Destroy Qt windows while QApplication is still alive.

PySide 6.12, released 2026-10-08, aborts in ``destroyQCoreApplication`` when a
top-level widget is still alive at interpreter shutdown. The warning is
``QObject: shared QObject was deleted directly`` and the next native fault is
``malloc_consolidate(): unaligned fastbin chunk detected``. ``close()`` only
hides a window. The C++ object has to be deleted, and the Python wrapper
invalidated, before the application object dies.
"""

from __future__ import annotations

from typing import Any


def release_top_level_widgets(application: Any) -> int:
    """Close and delete every still-valid top-level widget. Returns how many were deleted."""
    import shiboken6
    from PySide6.QtWidgets import QApplication

    released = 0
    for widget in list(QApplication.topLevelWidgets()):
        if not shiboken6.isValid(widget):
            continue
        # A refused close means a worker is still running. Deleting the window
        # under that thread is the crash this function exists to avoid.
        if widget.close() is False:
            continue
        if shiboken6.isValid(widget):
            shiboken6.delete(widget)
            released += 1
    application.processEvents()
    return released
