"""Document taxonomy: the types, their aliases and their evidence signatures.

Everything the classifier knows is data in this module - it is a taxonomy, not a
scattered pile of string comparisons in five places.  An LLM classifier added
later must produce one of these same values (see
:meth:`Classifier.validate`), which is what keeps deterministic and AI
classification comparable and auditable.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.enums import DocumentClassification


@dataclass(frozen=True)
class TypeSignature:
    """Evidence profile for one document type."""

    classification: DocumentClassification
    #: Filename patterns (regex, case-insensitive) with weights.
    filename_patterns: tuple[tuple[str, float], ...] = ()
    #: Content phrases that indicate the type, with weights and page scope.
    content_patterns: tuple[tuple[str, float], ...] = ()
    #: Phrases that argue *against* the type.
    negative: tuple[str, ...] = ()
    #: Typical extensions (adds a small amount of evidence, never decides alone).
    extensions: tuple[str, ...] = ()
    #: The default source-authority tier a document of this type gets (section 83).
    authority_tier: str = "historical_report"
    #: Whether this type is expected to be a table-heavy document.
    tabular: bool = False
    description: str = ""


#: Authority tiers per type follow the *configurable* ladder in the settings
#: (section 19).  These are the defaults the platform ships with; they are not a
#: law of nature and the settings file can reorder them.
TAXONOMY: tuple[TypeSignature, ...] = (
    TypeSignature(
        classification=DocumentClassification.DRILLING_PROGRAM,
        filename_patterns=(
            (r"\bdrill(ing)?[\s_-]*program\b", 0.55),
            (r"\bwell\s*plan\b", 0.35),
            (r"\bcasing\s*(and\s*)?mud\s*program\b", 0.3),
            (r"\bprogramme\b", 0.25),
        ),
        content_patterns=(
            (r"\bhole sections?\b", 0.2),
            (r"\bcasing program\b", 0.2),
            (r"\bdrilling (fluid|mud) program\b", 0.2),
            (r"\bmud weight\b.{0,40}(ppg|kg/m3|SG)", 0.2),
            (r"\bwell control\b.{0,60}(requirements|parameters)", 0.15),
            (r"\bobjectives\b", 0.1),
            (r"\bbha\b.{0,40}(assembly|no\.|program)", 0.12),
            (r"\bcement(ing)? program\b", 0.18),
            (r"\bsection\b.{0,25}(top|bottom|from|to)\b", 0.1),
            (r"\bfracture gradient\b", 0.12),
            (r"\bpore pressure\b", 0.12),
        ),
        negative=(r"\bdaily drilling report\b", r"\bDDR\b"),
        extensions=(".pdf", ".docx", ".xlsx"),
        authority_tier="approved_drilling_program",
        description="Planned well construction document: sections, casing, mud, BHA, requirements.",
    ),
    TypeSignature(
        classification=DocumentClassification.DDR,
        filename_patterns=(
            (r"\bddr\b", 0.6),
            (r"\bdaily\s*drill(ing)?\s*report\b", 0.6),
            (r"\bdrill(ing)?\s*report\b", 0.3),
            (r"\bdaily\s*report\b", 0.25),
        ),
        content_patterns=(
            (r"\breport no\.?\b", 0.15),
            (r"\bdepth\s*(from|to)\b", 0.2),
            (r"\bactivity\s*code\b", 0.15),
            (r"\bRIG-?\d*\b.{0,40}(spud|on bottom|trip)", 0.15),
            (r"\b0[0-9]:[0-9]{2}\b.{0,80}\b0[0-9]:[0-9]{2}\b", 0.1),
            (r"\btime\b.{0,20}(24|clock)", 0.1),
            (r"\bNPT\b", 0.12),
            (r"\bbit no\.?\b", 0.1),
        ),
        extensions=(".xlsx", ".xlsm", ".pdf"),
        authority_tier="current_operational_report",
        tabular=True,
        description="Daily Drilling Report: time-activity log, depth progress, NPT, crews.",
    ),
    TypeSignature(
        classification=DocumentClassification.MUD_REPORT,
        filename_patterns=(
            (r"\bmud\s*(report|log|daily)\b", 0.6),
            (r"\bfluids?\s*report\b", 0.4),
            (r"\bmud\s*checking\b", 0.5),
            (r"\bdaily\s*mud\b", 0.5),
        ),
        content_patterns=(
            (r"\bplastic viscosity\b|\bPV\b", 0.25),
            (r"\byield point\b|\bYP\b", 0.25),
            (r"\bgel strength\b", 0.25),
            (r"\bmarsh funnel\b", 0.2),
            (r"\bretort\b", 0.2),
            (r"\bfluids?\s*property\b", 0.2),
            (r"\bmud weight\b.{0,30}(ppg|SG|kg/m3)", 0.2),
            (r"\bsand content\b", 0.15),
            (r"\bLBT\b|\blow gravity test\b", 0.2),
        ),
        extensions=(".xlsx", ".pdf"),
        authority_tier="current_operational_report",
        tabular=True,
        description="Mud/fluids daily report: properties, additives, solids control.",
    ),
    TypeSignature(
        classification=DocumentClassification.BHA_REPORT,
        filename_patterns=(
            (r"\bbha\b", 0.5),
            (r"\bbottom.?hole.?assembly\b", 0.55),
            (r"\bassembly\s*(no|sheet|sketch)\b", 0.3),
        ),
        content_patterns=(
            (r"\bdrill\s*collar\b", 0.25),
            (r"\bjar\b|\baccelerator\b", 0.2),
            (r"\bstabiliser\b|\bstabilizer\b", 0.2),
            (r"\bmotor\b|\bpdc\b|\bbearing\b", 0.15),
            (r"\bnozzle[s]?\b", 0.15),
            (r"\btorque\s*(and|&)\s*drag\b", 0.1),
        ),
        extensions=(".pdf", ".xlsx", ".docx"),
        authority_tier="approved_engineering_document",
        description="Bottom hole assembly composition, component tally and running record.",
    ),
    TypeSignature(
        classification=DocumentClassification.BIT_RECORD,
        filename_patterns=(
            (r"\bbit\s*(record|sheet|no|run|log)\b", 0.6),
            (r"\bdrill\s*bit\b", 0.45),
        ),
        content_patterns=(
            (r"\bbit\s*no\.?\b", 0.3),
            (r"\biadc\b", 0.35),
            (r"\bnozzle\s*sizes?\b", 0.2),
            (r"\bT\+?\s?\d+\s*h\b|\btime\s*on\s*bit\b", 0.2),
            (r"\bfootage\s*drilled\b", 0.25),
            (r"\bbearing\s*seal\b|\bjournal\s*seal\b", 0.2),
        ),
        extensions=(".pdf", ".xlsx"),
        authority_tier="current_operational_report",
        description="Bit run record: type, nozzles, hours, footage, condition/IADC.",
    ),
    TypeSignature(
        classification=DocumentClassification.DIRECTIONAL_SURVEY,
        filename_patterns=(
            (r"\b(survey|directional|wellpath|trajectory)\b", 0.5),
            (r"\bprior\s*well\b", 0.15),
        ),
        content_patterns=(
            (r"\binclination\b|\bINC\b", 0.3),
            (r"\bazimuth\b|\bAZ\b", 0.3),
            (r"\bnorthings?\b|\bN\s*\d{6,7}\b", 0.25),
            (r"\beastings?\b|\bE\s*\d{6,7}\b", 0.25),
            (r"\bDLS\b|dog.?leg", 0.3),
            (r"\btool\s*face\b", 0.2),
            (r"\bMD\b.{0,10}TVD\b", 0.2),
        ),
        extensions=(".xlsx", ".csv", ".txt", ".pdf"),
        authority_tier="current_operational_report",
        tabular=True,
        description="Directional survey (MD/TVD/inclination/azimuth, closure, DLS).",
    ),
    TypeSignature(
        classification=DocumentClassification.CEMENT_REPORT,
        filename_patterns=(
            (r"\bcement\b", 0.6),
            (r"\bcement(ing)?\s*(job|report|program|plan)\b", 0.6),
        ),
        content_patterns=(
            (r"\bslack\s*time\b", 0.3),
            (r"\bwait on cement|\bWOC\b", 0.3),
            (r"\bclass\s*[GHJ]\b", 0.3),
            (r"\bTOC\b|\btop of cement\b", 0.35),
            (r"\bdump\s*bucket\b", 0.2),
            (r"\bspacer\b|\bwiper\s*plug\b", 0.2),
            (r"\bdensity\b.{0,20}slurry", 0.2),
        ),
        extensions=(".pdf", ".xlsx"),
        authority_tier="current_operational_report",
        description="Cementing job card: slurry design, volumes, placement, TOC.",
    ),
    TypeSignature(
        classification=DocumentClassification.CASING_REPORT,
        filename_patterns=(
            (r"\bcasing\b.{0,12}(report|running| tally|program|record)\b", 0.6),
            (r"\bcasing\s*tally\b", 0.55),
            (r"\brunning\s*report\b", 0.3),
        ),
        content_patterns=(
            (r"\bcasing\s*hanger\b", 0.25),
            (r"\bpup\s*joint\b", 0.3),
            (r"\bweight per (metre|meter|foot)\b|\blb/ft\b", 0.3),
            (r"\bgrade\b.{0,10}(J55|K55|N80|L80|P110|Q125)", 0.35),
            (r"\bcement\s*top\b", 0.15),
            (r"\btension\b.{0,30}(lim|design)", 0.2),
            (r"\bbuoyan(c|ty)\b", 0.2),
        ),
        extensions=(".pdf", ".xlsx", ".docx"),
        authority_tier="current_operational_report",
        tabular=True,
        description="Casing running report/tally: joints, grades, weights, depths.",
    ),
    TypeSignature(
        classification=DocumentClassification.WELL_CONTROL,
        filename_patterns=(
            (r"\bwell\s*control\b", 0.55),
            (r"\bkill\b.{0,20}(sheet|report|record|card)", 0.4),
            (r"\bbop\b", 0.4),
            (r"\bpressure\s*test\b", 0.25),
        ),
        content_patterns=(
            (r"\bSIDPP\b", 0.4),
            (r"\bSICP\b", 0.4),
            (r"\bkick\s*volume\b|\bkick\s*tolerance\b", 0.4),
            (r"\bMAASP\b", 0.4),
            (r"\bdriller'?s method\b|\bwait and weight\b", 0.4),
            (r"\bchoke\s*(pressure|line)\b", 0.25),
            (r"\bshut[- ]?in\b.{0,30}(circulat|pressure)", 0.2),
            (r"\bBOP\b.{0,40}(test|configuration|stack)", 0.3),
        ),
        extensions=(".pdf", ".xlsx", ".docx"),
        authority_tier="current_operational_report",
        description="Well control: kick records, kill sheets, BOP tests, MAASP.",
    ),
    TypeSignature(
        classification=DocumentClassification.LOGGING,
        filename_patterns=(
            (r"\blog(s|ging)?\s*(report|summary|program)\b", 0.5),
            (r"\bwireline\b", 0.35),
            (r"\bLWD\b|\bMWD\b", 0.35),
        ),
        content_patterns=(
            (r"\bgamma\s*ray\b|\bGR\b", 0.25),
            (r"\bresistivity\b", 0.25),
            (r"\bsonic\b|\bdipole\b", 0.2),
            (r"\bporosity\b|\bSW\b|\bVSH\b", 0.2),
            (r"\bformation\s*top\b", 0.15),
        ),
        extensions=(".pdf", ".xlsx"),
        authority_tier="current_operational_report",
        description="Open/closure hole logging and LWD/MWD acquisition reports.",
    ),
    TypeSignature(
        classification=DocumentClassification.NPT,
        filename_patterns=(
            (r"\bnpt\b", 0.6),
            (r"\bnon[- ]productive\b", 0.5),
            (r"\btime\s*(loss|lost)\b", 0.35),
        ),
        content_patterns=(
            (r"\bNPT\b.{0,30}(hours|h|days|classification)", 0.35),
            (r"\bclassification\b.{0,30}(stuck|losses|equipment|weather)", 0.35),
            (r"\bcause\b.{0,40}(stuck|lost circulation|failure)", 0.3),
            (r"\bballad\b", 0.15),
        ),
        extensions=(".xlsx", ".pdf"),
        authority_tier="current_operational_report",
        tabular=True,
        description="NPT/time-loss records with causes and durations.",
    ),
    TypeSignature(
        classification=DocumentClassification.TIME_BREAKDOWN,
        filename_patterns=(
            (r"\btime\s*(sheet|breakdown|summary|analysis)\b", 0.6),
            (r"\bcost\s*time\b", 0.25),
        ),
        content_patterns=(
            (r"\bproductive\b.{0,20}time", 0.3),
            (r"\bflat\s*time\b", 0.35),
            (r"\binvisible\b", 0.2),
            (r"\btrip\s*time\b", 0.25),
            (r"\brig\s*time\b", 0.2),
            (r"\bAFE\b", 0.1),
        ),
        extensions=(".xlsx", ".csv"),
        authority_tier="historical_report",
        tabular=True,
        description="Time breakdown by activity category (productive/NPT/flat).",
    ),
    TypeSignature(
        classification=DocumentClassification.COST,
        filename_patterns=(
            (r"\bcost\b", 0.4),
            (r"\binvoice\b|\bAPC\b|\bAFE\b", 0.4),
            (r"\bbudget\b", 0.3),
        ),
        content_patterns=(
            (r"\bday\s*rate\b", 0.3),
            (r"\bUSD\b|\$\s?\d", 0.25),
            (r"\bNPT\s*cost\b", 0.35),
            (r"\bcost per (metre|foot|barrel)\b", 0.3),
            (r"\btariff\b", 0.2),
        ),
        extensions=(".xlsx", ".pdf"),
        authority_tier="historical_report",
        tabular=True,
        description="Cost, invoice and AFE documentation.",
    ),
    TypeSignature(
        classification=DocumentClassification.EOWR,
        filename_patterns=(
            (r"\beowr\b", 0.7),
            (r"\bend\s*of\s*well\b", 0.65),
            (r"\bwell\s*(completion|termination)\s*report\b", 0.4),
        ),
        content_patterns=(
            (r"\blessons\s*learned\b", 0.2),
            (r"\brecommendations\b", 0.15),
            (r"\bplan\s*vs\.?\s*actual\b", 0.25),
            (r"\bexecution\s*summary\b", 0.2),
        ),
        extensions=(".pdf", ".docx", ".xlsx"),
        authority_tier="historical_report",
        description="End of Well Report: retrospective of execution against plan.",
    ),
    TypeSignature(
        classification=DocumentClassification.LESSON_LEARNED,
        filename_patterns=(
            (r"\blesson[s]?\b", 0.6),
            (r"\bllform\b|\bknowledge\s*(alert|product)\b", 0.45),
        ),
        content_patterns=(
            (r"\blesson\s*(learned|learnt)\b", 0.4),
            (r"\bwhat went (wrong|well)\b", 0.35),
            (r"\broot cause\b", 0.25),
            (r"\baction\s*(required|taken)\b", 0.2),
            (r"\brecommendation\b", 0.2),
        ),
        extensions=(".pdf", ".docx", ".md", ".txt"),
        authority_tier="historical_report",
        description="Lessons-learned form: event, cause, consequence, mitigation.",
    ),
    TypeSignature(
        classification=DocumentClassification.PROCEDURE,
        filename_patterns=(
            (r"\bprocedure\b|\bSOP\b|\bwork\s*instruction\b", 0.6),
            (r"\bguideline\b", 0.35),
        ),
        content_patterns=(
            (r"\bscope\b.{0,40}responsibilit", 0.2),
            (r"\bstep\s*\d\b", 0.25),
            (r"\bshall\b", 0.15),
            (r"\bmust\b", 0.1),
        ),
        extensions=(".pdf", ".docx"),
        authority_tier="technical_reference",
        description="Company procedure / SOP / work instruction.",
    ),
    TypeSignature(
        classification=DocumentClassification.STANDARD,
        filename_patterns=(
            (r"\bAPI\b|\bISO\b|\bRP\s*\d+\b", 0.5),
            (r"\bstandard\b", 0.4),
            (r"\bspecification\b", 0.3),
        ),
        content_patterns=(
            (r"\bAPI\s*RP\s*(59|53|65|14)\b", 0.5),
            (r"\bnorsok\b|\bNORSOK\b", 0.4),
            (r"\bspecification\s*\d", 0.2),
            (r"\bnormative reference\b", 0.3),
        ),
        extensions=(".pdf", ".docx"),
        authority_tier="technical_reference",
        description="Industry/company standard or specification.",
    ),
    TypeSignature(
        classification=DocumentClassification.TECHNICAL_REFERENCE,
        filename_patterns=(
            (r"\bhandbook\b|\bmanual\b|\btextbook\b|\breference\b", 0.45),
        ),
        content_patterns=(
            (r"\bchapter\s*\d+\b", 0.35),
            (r"\bfigure\s*\d+\.\d+\b", 0.2),
            (r"\bexample\s*\d", 0.2),
            (r"\breferences\b|\bbibliography\b", 0.25),
        ),
        extensions=(".pdf", ".md", ".txt", ".docx"),
        authority_tier="technical_reference",
        description="Technical book or manual used as engineering reference.",
    ),
    TypeSignature(
        classification=DocumentClassification.HSE,
        filename_patterns=(
            (r"\bHSE\b|\bsafety\b|\bpermit to work\b", 0.5),
            (r"\bincident\b|\bnear miss\b", 0.45),
        ),
        content_patterns=(
            (r"\bjob safety analysis\b|\bJSA\b", 0.35),
            (r"\btoolbox talk\b", 0.3),
            (r"\bH2S\b", 0.25),
            (r"\bincident\b.{0,30}(report|number|investigation)", 0.3),
        ),
        extensions=(".pdf", ".docx", ".xlsx"),
        authority_tier="current_operational_report",
        description="HSE documentation: permits, incidents, drills, risk assessments.",
    ),
    TypeSignature(
        classification=DocumentClassification.SERVICE_REPORT,
        filename_patterns=(
            (r"\bservice\s*(report|card|ticket|company)\b", 0.6),
            (r"\b(rig|site)\s*report\b", 0.25),
        ),
        content_patterns=(
            (r"\bservice(ly)? provided\b", 0.25),
            (r"\bequipment\s*used\b", 0.2),
            (r"\bpersonnel\b.{0,30}(engineer|crew)", 0.15),
            (r"\btime\s*card\b", 0.2),
        ),
        extensions=(".pdf", ".xlsx", ".docx"),
        authority_tier="current_operational_report",
        description="Service company report: scope, equipment, personnel, time.",
    ),
    TypeSignature(
        classification=DocumentClassification.CONTRACT,
        filename_patterns=((r"\bcontract\b|\bworkscope\b|\bscope of work\b", 0.6), (r"\bTBE\b|\bITB\b", 0.35)),
        content_patterns=((r"\bparties\b.{0,40}\bagree\b", 0.3), (r"\bliability\b", 0.2), (r"\breimbursable\b", 0.25)),
        extensions=(".pdf", ".docx"),
        authority_tier="approved_engineering_document",
        description="Contract, workscope and commercial terms.",
    ),
)

CLASSIFIER_TAXONOMY: dict[DocumentClassification, TypeSignature] = {signature.classification: signature for signature in TAXONOMY}


def authority_for(classification: DocumentClassification, *, status: str | None = None, is_current: bool = True) -> str:
    """Default authority tier for a document type, refined by approval state.

    'Approved' + current + program is the top of the ladder; the same document
    superseded drops a tier.  This mapping is a *default*, overridable per
    document in the registry (section 83).
    """
    signature = CLASSIFIER_TAXONOMY.get(classification)
    tier = signature.authority_tier if signature else "general_knowledge"
    if classification is DocumentClassification.DRILLING_PROGRAM:
        if (status or "").upper() in {"APPROVED", "CURRENT", "ISSUED FOR DRILLING"} and is_current:
            return "approved_drilling_program"
        if not is_current:
            return "previous_revision"
        return "current_program_revision"
    if (status or "").upper() == "APPROVED" and tier in {"historical_report", "current_operational_report"}:
        return "approved_engineering_document"
    return tier


__all__ = ["CLASSIFIER_TAXONOMY", "TAXONOMY", "DocumentClassification", "TypeSignature", "authority_for"]
