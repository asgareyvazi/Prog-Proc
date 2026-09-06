"""The operational history: what a well did, what went wrong, and what it cost in hours.

Four tables and one derivation step, in the order the records depend on each other: a *report* (a
daily drilling report, an NPT summary) says a day happened; *operations* are the activities that
filled it; *events* are the things that happened during those activities; *NPT records* are the hours
an event cost; and *problems* are what those events and hours add up to when someone asks the harder
question - not "what happened" but "why, and does it keep happening".

:mod:`drilling_intelligence.operations.promote` is the only thing here that writes rows from a
document, and it does so from tables and typed fields only.  The repositories are the only thing that
writes rows at all, so nothing in this package holds state: a rebuild of the derived records is a
delete-and-repromote, not a reconciliation job.
"""

from .assets import AssetRepository
from .promote import PromotionResult, VersionPromoter, promotion_identity
from .repository import REPORT_CLASSIFICATIONS, OperationsRepository, set_record_status
from .service import OperationalService

__all__ = [
    "REPORT_CLASSIFICATIONS",
    "AssetRepository",
    "OperationalService",
    "OperationsRepository",
    "PromotionResult",
    "VersionPromoter",
    "promotion_identity",
    "set_record_status",
]
