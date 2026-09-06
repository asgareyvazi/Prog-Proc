"""Well-centric domain: workspaces, projects, wells, sections and states.

A workspace is the unit of the corpus (folder + registry database); wells and
sections carry the lifecycle and the PLANNED/ACTUAL separation that keeps a
forecast from being mistaken for an offset well's actuals.
"""

from .repository import WellRepository
from .workspace import WORKSPACE_MARKER, Workspace, WorkspaceConfig

__all__ = [
    "WORKSPACE_MARKER",
    "WellRepository",
    "Workspace",
    "WorkspaceConfig",
]
