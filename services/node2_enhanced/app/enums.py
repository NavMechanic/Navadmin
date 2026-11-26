"""Controlled vocabularies.

These are stored as strings rather than native PostgreSQL enums on purpose:
adding a value to a PG enum inside a transaction is awkward, and a PLM
vocabulary grows (a new artifact type, a new certification scheme) far more
often than it changes shape. The constraint is enforced by Pydantic on the way
in and by CHECK constraints on the columns that must never drift.
"""
from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """RBAC roles. Least privilege: reading the BOM is not approving a gate."""
    VIEWER = "viewer"                 # read everything
    ENGINEER = "engineer"             # create revisions, raise ECNs, upload artifacts
    QUALITY = "quality"               # own FMEA rows, close re-reviews
    GATEKEEPER = "gatekeeper"         # transition gate state (the only role that can pass a gate)
    CHANGE_BOARD = "change_board"     # approve/reject ECNs
    INTEGRATION = "integration"       # vault webhooks and connector callbacks
    # Field evidence can open a CAPA and propose an FMEA occurrence re-rating.
    # It cannot apply one. Occurrence drives Action Priority and Action Priority
    # is what the G2 gate blocks on, so a service that could write occurrence
    # directly could unblock a gate with nobody deciding to.
    FIELD_QUALITY = "field_quality"   # open CAPAs, propose re-ratings; apply nothing
    ADMIN = "admin"                   # everything


class PartType(StrEnum):
    SKU = "SKU"                       # a sellable, packed unit or kit
    ASSEMBLY = "ASSEMBLY"
    COMPONENT = "COMPONENT"
    PACKAGING = "PACKAGING"
    DOCUMENT = "DOCUMENT"             # manuals, labels — they carry revisions too
    CONSUMABLE = "CONSUMABLE"


class SupplyMode(StrEnum):
    SKD_IMPORT = "SKD_IMPORT"
    LOCAL_KITTING = "LOCAL_KITTING"
    IN_HOUSE = "IN_HOUSE"


class LifecycleState(StrEnum):
    """Revision lifecycle. Only RELEASED revisions may be consumed by a gate."""
    WIP = "WIP"
    IN_REVIEW = "IN_REVIEW"
    RELEASED = "RELEASED"
    SUPERSEDED = "SUPERSEDED"
    OBSOLETE = "OBSOLETE"


class ArtifactType(StrEnum):
    CAD_MASTER_MODEL = "CAD_MASTER_MODEL"        # parametric master, STEP AP242
    CAD_DRAWING = "CAD_DRAWING"
    SCHEMATIC = "SCHEMATIC"
    GERBER = "GERBER"
    BOM_EXPORT = "BOM_EXPORT"
    DFMEA = "DFMEA"
    PFMEA = "PFMEA"
    CONTROL_PLAN = "CONTROL_PLAN"
    TEST_REPORT = "TEST_REPORT"
    COMPLIANCE_CERTIFICATE = "COMPLIANCE_CERTIFICATE"
    CAE_REPORT = "CAE_REPORT"
    TOLERANCE_STACK = "TOLERANCE_STACK"
    SAFETY_ASSESSMENT = "SAFETY_ASSESSMENT"
    ARTWORK = "ARTWORK"
    GOLDEN_SAMPLE = "GOLDEN_SAMPLE"
    OTHER = "OTHER"


class ArtifactStatus(StrEnum):
    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    RELEASED = "RELEASED"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"


class CertificationScheme(StrEnum):
    BIS_CRS = "BIS_CRS"               # Compulsory Registration Scheme (India)
    WPC_ETA = "WPC_ETA"               # Wireless Planning & Coordination equipment approval
    UN38_3 = "UN38_3"                 # lithium battery transport test summary
    IEC_62133_2 = "IEC_62133_2"
    IEC_62368_1 = "IEC_62368_1"
    BQB = "BQB"                       # Bluetooth SIG qualification
    EPR = "EPR"                       # e-waste producer registration
    RoHS = "RoHS"
    EN_1811 = "EN_1811"               # nickel release, skin contact


class CertificateStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    APPLIED = "APPLIED"
    IN_TESTING = "IN_TESTING"
    VALID = "VALID"
    EXPIRED = "EXPIRED"
    SUSPENDED = "SUSPENDED"
    WITHDRAWN = "WITHDRAWN"


class FmeaType(StrEnum):
    DFMEA = "DFMEA"
    PFMEA = "PFMEA"


class ActionPriority(StrEnum):
    """AIAG-VDA Action Priority, which replaced the RPN threshold."""
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ReviewState(StrEnum):
    CURRENT = "CURRENT"
    PENDING_RE_REVIEW = "PENDING_RE_REVIEW"
    RE_REVIEW_IN_PROGRESS = "RE_REVIEW_IN_PROGRESS"


class GateCode(StrEnum):
    G0_CONCEPT = "G0_CONCEPT"
    G1_FEASIBILITY = "G1_FEASIBILITY"
    G2_DESIGN_EVT = "G2_DESIGN_EVT"
    G3_EVT_EXIT = "G3_EVT_EXIT"
    G4_DVT_EXIT = "G4_DVT_EXIT"
    G5_PVT_EXIT = "G5_PVT_EXIT"
    G6_SOP = "G6_SOP"
    G7_POST_LAUNCH = "G7_POST_LAUNCH"

    @property
    def ordinal(self) -> int:
        return int(self.value[1])


class GateState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    OPEN = "OPEN"
    BLOCKED = "BLOCKED"
    PASSED = "PASSED"
    WAIVED = "WAIVED"                 # passed by explicit, recorded deviation


class EvaluationStatus(StrEnum):
    PASSED = "PASSED"
    BLOCKED = "BLOCKED"


class RuleStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    WARNING = "WARNING"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class RuleSeverity(StrEnum):
    BLOCKING = "BLOCKING"             # a failure blocks the transition
    ADVISORY = "ADVISORY"             # a failure is reported but does not block


class CapaState(StrEnum):
    """CAPA lifecycle. A CAPA is opened by evidence and closed by verification."""
    OPEN = "OPEN"
    CONTAINMENT = "CONTAINMENT"          # interim action while root cause is found
    INVESTIGATION = "INVESTIGATION"
    ACTION_IMPLEMENTED = "ACTION_IMPLEMENTED"
    EFFECTIVENESS_CHECK = "EFFECTIVENESS_CHECK"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"                # investigated and found not systemic


class CapaSource(StrEnum):
    FIELD_CLUSTER = "FIELD_CLUSTER"      # Node 6 clustering engine
    LINE_QUALITY = "LINE_QUALITY"        # Node 5
    AUDIT = "AUDIT"
    CUSTOMER_COMPLAINT = "CUSTOMER_COMPLAINT"
    REGULATORY = "REGULATORY"
    INTERNAL = "INTERNAL"


class ReratingState(StrEnum):
    """A proposed FMEA occurrence change is not an applied one.

    Occurrence feeds Action Priority, and Action Priority is what the G2 gate
    rule blocks on. An automated service that could write occurrence directly
    could lower an AP from HIGH to MEDIUM and unblock a gate with no human in
    the loop. So field evidence *proposes*; a named quality engineer *applies*.
    """
    NONE = "NONE"
    PROPOSED = "PROPOSED"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"


class ECNState(StrEnum):
    DRAFT = "DRAFT"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    IMPLEMENTED = "IMPLEMENTED"
    CANCELLED = "CANCELLED"


class ECNCategory(StrEnum):
    SAFETY = "SAFETY"
    REGULATORY = "REGULATORY"
    QUALITY = "QUALITY"
    COST = "COST"
    SUPPLY_CONTINUITY = "SUPPLY_CONTINUITY"   # the NRND/EOL case
    COSMETIC = "COSMETIC"
    DOCUMENTATION = "DOCUMENTATION"


class ChangeType(StrEnum):
    SUBSTITUTE = "SUBSTITUTE"
    ADD = "ADD"
    REMOVE = "REMOVE"
    QTY_CHANGE = "QTY_CHANGE"
    SPEC_CHANGE = "SPEC_CHANGE"
    SUPPLIER_CHANGE = "SUPPLIER_CHANGE"


class EffectivityType(StrEnum):
    IMMEDIATE = "IMMEDIATE"
    DATE = "DATE"
    SERIAL = "SERIAL"
    STOCK_EXHAUSTED = "STOCK_EXHAUSTED"


class ImpactObjectType(StrEnum):
    DFMEA_ROW = "DFMEA_ROW"
    PFMEA_ROW = "PFMEA_ROW"
    CONTROL_PLAN_ITEM = "CONTROL_PLAN_ITEM"
    TEST_CASE = "TEST_CASE"
    CERTIFICATE = "CERTIFICATE"
    ARTIFACT = "ARTIFACT"
    DESIGN_PARAMETER = "DESIGN_PARAMETER"


class ImpactState(StrEnum):
    PENDING_RE_REVIEW = "PENDING_RE_REVIEW"
    IN_PROGRESS = "IN_PROGRESS"
    RE_REVIEWED = "RE_REVIEWED"
    NOT_AFFECTED = "NOT_AFFECTED"
