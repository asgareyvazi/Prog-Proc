"""Engineering records: what was planned, what was approved, and what is being carried.

Four things belong together here because they are read together and because they share the machinery that
makes an engineering record safe to rely on:

*   **procedures** and **programs** as revision chains - a new revision is a new row, the old one stays
    readable, and only one revision is current per code (the database enforces it, migration 0004);
*   **targets**, the numbers a program commits each section to, which are the planned half of
    plan-versus-actual - the actual half is already on the section rows the well registry maintains;
*   **risks**, whose assessment columns store what a source stated and nothing this layer computed: the
    platform persists a 5x5 score, it does not invent one;
*   and **cost items**, planned against actual, grouped by their WBS/CBS codes because the codes are what
    make a cost structure a structure.

The one number this package *computes* is the lost-time roll-up in :mod:`.service`, and it is addition
over promoted NPT rows: the production path that gives ``calculation`` a writer outside the tests, with
every row it summed cited as its own input.  Nothing else here generates content: a procedure's text, a
program's targets and a risk's severity
come from a person or from a promoted document; what this layer adds is the versioning, the lifecycle, the
scope validation and the evidence links that make those inputs answerable later.
"""

from .costs import CostRepository
from .repository import EngineeringRepository
from .risk import RiskRepository
from .service import EngineeringService

__all__ = ["CostRepository", "EngineeringRepository", "EngineeringService", "RiskRepository"]
