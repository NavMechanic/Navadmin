"""ATHENA N0 schema families (Pydantic v2 → JSON Schema Draft 2020-12).

The eight versioned contracts:

    SourceRecord.v1      rights & permitted use of a data source
    Artifact.v1          the envelope every incoming package / cross-node handoff uses
    Requirement.v1       one typed engineering value with explicit value_origin + evidence
    ModelRun.v1          one LLM/SLM call (model, prompt revision, input hash, usage, cost)
    VendorWorkPackage.v1 what may be sent to a vendor connector, under which budget
    VendorResult.v1      what came back (native files, manifest, checksum, tool version)
    GatewayDecision.v1   PASS | REWORK | BLOCK | REVIEW with violated rules and destinations
    ApprovalEvent.v1     an immutable human approval / rejection for an exact revision

plus per-``artifact_type`` payload models, the RFQ extraction draft and the API envelopes.
Every model is ``extra="forbid"``: unknown fields are rejected, never silently carried.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ----------------------------------------------------------------------------------------------
# vocabularies
# ----------------------------------------------------------------------------------------------
NodeId = Literal["N0", "N1", "N2", "N3", "N4", "N5", "N6", "N7", "N8"]
DownstreamNode = Literal["N1", "N2", "N3", "N4", "N5", "N6", "N7", "N8"]
ArtifactType = Literal["RFQ", "DATASHEET", "EDA_PACKAGE", "SUPPLIER_QUOTE", "TEST_MEASUREMENT", "MARKET_FEED",
                       "FINANCE_RECORD", "TELEMETRY", "MODEL_OUTPUT",
                       # market & opportunity intelligence (N1 inputs, then N1 → N6 / N7 outputs)
                       "MARKET_BATCH", "BUYER_RECORD", "COMMERCIAL_SHORTLIST", "SAM_EVIDENCE",
                       # product & design digital thread (N2 → N3 → N4 / N5)
                       "PRD", "DESIGN_RELEASE", "BOM_RELEASE",
                       # verification thread (N4 → N6 / N7 / N8)
                       "VERIFICATION_RELEASE"]
ProcessingPurpose = Literal[
    "RFQ_ANALYSIS", "PRODUCT_DEFINITION", "DESIGN_ENGINEERING", "VERIFICATION", "SOURCING", "COMMERCIAL",
    "FINANCE", "LIFECYCLE", "MARKET_INTELLIGENCE", "EVALUATION",
    # capability grants (checked before the corresponding action, never a business purpose by themselves)
    "LLM_EXTRACTION", "VENDOR_TRANSMISSION", "MODEL_TRAINING", "PERSONAL_DATA_PROCESSING"]
CAPABILITY_GRANTS = ("LLM_EXTRACTION", "VENDOR_TRANSMISSION", "MODEL_TRAINING", "PERSONAL_DATA_PROCESSING")
ReleaseStage = Literal["INTAKE", "CONCEPT", "PROTOTYPE", "PILOT", "PRODUCTION"]
RELEASE_STAGE_ORDER = {"INTAKE": 0, "CONCEPT": 1, "PROTOTYPE": 2, "PILOT": 3, "PRODUCTION": 4}
Confidentiality = Literal["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"]
CONFIDENTIALITY_RANK = {"PUBLIC": 0, "INTERNAL": 1, "CONFIDENTIAL": 2, "RESTRICTED": 3}
ValueOrigin = Literal["CUSTOMER", "MEASURED", "DERIVED", "MODEL_EXTRACTED"]
Decision = Literal["PASS", "REWORK", "BLOCK", "REVIEW"]
ApprovalStatus = Literal["PENDING", "APPROVED", "REJECTED", "REVOKED"]
ConnectorType = Literal["REST_GRAPHQL", "CLI_SDK", "FILE_PORTAL", "SCPI_VISA", "MES_ERP", "MQTT_OPCUA"]
Severity = Literal["BLOCK", "REWORK", "REVIEW"]
Stage = Literal["N0.1", "N0.2", "N0.3", "N0.4", "N0.5", "N0.6"]

_ID = r"^[A-Za-z0-9][A-Za-z0-9._:\-]{1,127}$"
_SHA = r"^[0-9a-f]{64}$"
_PARAM = r"^[a-z][a-z0-9_]{1,79}$"
QUALIFICATION_DISCLAIMER = ("PASS means the artifact satisfied N0's checks for its declared artifact_type and "
                            "release_stage. It is not engineering qualification or product certification.")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _utc(v: Optional[datetime]) -> Optional[datetime]:
    if v is not None and (v.tzinfo is None or v.utcoffset() != timedelta(0)):
        raise ValueError("timestamps must be timezone-aware UTC")
    return v


# ----------------------------------------------------------------------------------------------
# 1. SourceRecord.v1
# ----------------------------------------------------------------------------------------------
class SourceRecord(_Strict):
    """Rights and permitted use of one data source, per tenant. Nothing flows until APPROVED."""
    model_config = ConfigDict(extra="forbid", title="SourceRecord.v1")
    schema_version: Literal["SourceRecord.v1"] = "SourceRecord.v1"
    source_id: str = Field(pattern=_ID)
    tenant_id: str = Field(pattern=_ID)
    publisher: str = Field(min_length=1, max_length=256)
    license: str = Field(min_length=1, max_length=128,
                         description="SPDX id, LicenseRef-* or customer contract reference")
    permitted_use: list[ProcessingPurpose] = Field(min_length=1)
    approval_status: ApprovalStatus = "PENDING"
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    data_origin: Literal["REAL", "SYNTHETIC", "MIXED"] = "REAL"
    confidentiality: Confidentiality = "CONFIDENTIAL"
    allowed_vendor_ids: list[str] = Field(default_factory=list,
                                          description="vendors this source may be transmitted to; empty = none")
    valid_until: Optional[datetime] = None
    registered_by: Optional[str] = None

    @field_validator("approved_at", "valid_until")
    @classmethod
    def _utc_fields(cls, v):
        return _utc(v)

    @model_validator(mode="after")
    def _approval_fields(self):
        decided = self.approval_status in ("APPROVED", "REJECTED", "REVOKED")
        if decided != (self.approved_by is not None and self.approved_at is not None):
            raise ValueError("approved_by and approved_at are set exactly when approval_status is decided")
        return self


# ----------------------------------------------------------------------------------------------
# 3. Requirement.v1 (defined before Artifact payloads, which embed it)
# ----------------------------------------------------------------------------------------------
class EvidenceSnippet(_Strict):
    source_ref: str = Field(min_length=1, description="blob_id, file URI or 'payload'")
    text: str = Field(min_length=1, max_length=2000)
    page: Optional[int] = Field(default=None, ge=1)
    char_start: Optional[int] = Field(default=None, ge=0)
    char_end: Optional[int] = Field(default=None, ge=0)


class Tolerance(_Strict):
    kind: Literal["ABS", "PCT"]
    plus: float
    minus: float


class Derivation(_Strict):
    formula: str
    inputs: list[str] = Field(min_length=1)
    computed_by: str = "n0-deterministic"


class Requirement(_Strict):
    """One value. ``value_origin`` says who stated it: the customer, a measurement, N0's own
    deterministic derivation, or a model extraction awaiting human confirmation."""
    model_config = ConfigDict(extra="forbid", title="Requirement.v1")
    schema_version: Literal["Requirement.v1"] = "Requirement.v1"
    req_id: str = Field(pattern=_ID)
    parameter: str = Field(pattern=_PARAM, description="snake_case with a unit suffix, e.g. output_voltage_v")
    value: Union[float, int, str, bool, None]
    unit: Optional[str] = None
    tolerance: Optional[Tolerance] = None
    operating_conditions: dict[str, Any] = Field(default_factory=dict)
    value_origin: ValueOrigin
    evidence_snippets: list[EvidenceSnippet] = Field(default_factory=list)
    derivation: Optional[Derivation] = None
    si_value: Optional[float] = None
    si_unit: Optional[str] = None


# ----------------------------------------------------------------------------------------------
# per-artifact-type payloads (validated as JSON Schema 2020-12 inside N0.4)
# ----------------------------------------------------------------------------------------------
class Money(_Strict):
    amount: Decimal = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class RFQPayload(_Strict):
    model_config = ConfigDict(extra="forbid", title="RFQPayload.v1")
    rfq_id: str = Field(pattern=_ID)
    customer_name: str = Field(min_length=1)
    product_category: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    target_unit_price: Optional[Money] = None
    requested_delivery_date: Optional[date] = None
    requirements: list[Requirement] = Field(min_length=1)
    compliance_standards: list[str] = Field(default_factory=list)
    extraction_model_run_id: Optional[str] = None
    notes: Optional[str] = None


class DatasheetPayload(_Strict):
    model_config = ConfigDict(extra="forbid", title="DatasheetPayload.v1")
    part_number: str
    manufacturer: str
    datasheet_revision: Optional[str] = None
    requirements: list[Requirement] = Field(min_length=1)


class EDAPackagePayload(_Strict):
    model_config = ConfigDict(extra="forbid", title="EDAPackagePayload.v1")
    design_id: str = Field(pattern=_ID)
    eda_tool: str
    tool_version: str
    layer_count: int = Field(ge=1, le=64)
    native_files: list[str] = Field(min_length=1)
    drc_errors: int = Field(ge=0)
    erc_errors: int = Field(ge=0)


class SupplierQuotePayload(_Strict):
    model_config = ConfigDict(extra="forbid", title="SupplierQuotePayload.v1")
    quote_id: str = Field(pattern=_ID)
    supplier_id: str = Field(pattern=_ID)
    mpn: str
    quantity: int = Field(gt=0)
    unit_price: Money
    valid_until: datetime
    lead_time_days: int = Field(ge=0)


class Instrument(_Strict):
    instrument_id: str
    calibration_due: datetime


class TestMeasurementPayload(_Strict):
    __test__ = False
    model_config = ConfigDict(extra="forbid", title="TestMeasurementPayload.v1")
    test_id: str = Field(pattern=_ID)
    unit_id: str = Field(pattern=_ID)
    measured_at: datetime
    instruments: list[Instrument] = Field(min_length=1)
    measurements: list[Requirement] = Field(min_length=1)
    safety_related: bool = False
    result: Literal["PASS", "FAIL"]


class SeriesPoint(_Strict):
    name: str
    value: float
    unit: str


class MarketFeedPayload(_Strict):
    model_config = ConfigDict(extra="forbid", title="MarketFeedPayload.v1")
    feed_id: str = Field(pattern=_ID)
    as_of: datetime
    series: list[SeriesPoint] = Field(min_length=1)


class FinanceRecordPayload(_Strict):
    model_config = ConfigDict(extra="forbid", title="FinanceRecordPayload.v1")
    record_id: str = Field(pattern=_ID)
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    category: str
    amount: Money


class TelemetryPayload(_Strict):
    model_config = ConfigDict(extra="forbid", title="TelemetryPayload.v1")
    device_id: str = Field(pattern=_ID)
    observed_at: datetime
    readings: list[Requirement] = Field(min_length=1)


class ModelOutputPayload(_Strict):
    model_config = ConfigDict(extra="forbid", title="ModelOutputPayload.v1")
    model_run_id: str = Field(pattern=_ID)
    task: str
    requirements: list[Requirement] = Field(default_factory=list)
    output: dict[str, Any] = Field(default_factory=dict)


# ----------------------------------------------------------------------------------------------
# market & opportunity intelligence payloads
# ----------------------------------------------------------------------------------------------
ObservationType = Literal["TENDER_NOTICE", "CONTRACT_AWARD", "TRADE_STATISTIC", "BUDGET_DOCUMENT",
                          "BUYER_INTERVIEW", "CRM_RECORD"]
SourceConnector = Literal["UN_COMTRADE", "SAM_GOV", "TED_EU", "INDIA_CPPP", "CRM", "INTERVIEW", "MANUAL"]
SpendType = Literal["VERIFIED_BUDGET", "INDICATIVE", "HYPOTHETICAL"]
AmountKind = Literal["AWARD_VALUE", "ESTIMATED_VALUE", "BUDGET", "TRADE_VALUE", "STATED_SPEND"]
RelationshipStage = Literal["NONE", "COLD", "WARM", "ACTIVE"]
BudgetStatus = Literal["APPROVED", "PLANNED", "EXPLORATORY"]


class MarketAmount(_Strict):
    """Amount as stated by the source. Deliberately unconstrained in sign: N1.4 guardrails exclude
    negative or malformed values per observation instead of rejecting the whole batch."""
    amount: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class TenderAttachment(_Strict):
    name: str = Field(min_length=1)
    sha256: Optional[str] = Field(default=None, pattern=_SHA)
    required: bool = False
    present: bool = True


class MarketObservation(_Strict):
    """One piece of market evidence: a tender, an award, a trade statistic, a budget line, an
    interview or a CRM fact. ``spend_type`` is decided by N1, not by the submitter."""
    model_config = ConfigDict(extra="forbid", title="MarketObservation.v1")
    schema_version: Literal["MarketObservation.v1"] = "MarketObservation.v1"
    evidence_id: str = Field(pattern=_ID)
    observation_type: ObservationType
    source_connector: SourceConnector
    source_url: Optional[str] = Field(default=None, pattern=r"^(https?|n0)://", max_length=2048)
    source_record_id: Optional[str] = Field(default=None, max_length=256)
    buyer_name: Optional[str] = Field(default=None, max_length=512)
    buyer_identifiers: dict[str, str] = Field(default_factory=dict, description="e.g. {'uei': ..., 'vat': ...}")
    buyer_country: Optional[str] = Field(default=None, pattern=r"^[A-Z]{2}$")
    buyer_type: Optional[Literal["PUBLIC", "PRIVATE"]] = None
    title: Optional[str] = Field(default=None, max_length=1024)
    description: Optional[str] = Field(default=None, max_length=20000)
    service_category: Optional[str] = Field(default=None, pattern=_PARAM)
    classification_codes: dict[str, list[str]] = Field(default_factory=dict,
                                                       description="{'cpv': [...], 'naics': [...], 'hs': [...]}")
    amount: Optional[MarketAmount] = None
    amount_kind: Optional[AmountKind] = None
    budget_status: Optional[BudgetStatus] = None
    published_at: datetime
    deadline_at: Optional[datetime] = None
    quantity: Optional[int] = None
    requirements: list[Requirement] = Field(default_factory=list)
    compliance_standards: list[str] = Field(default_factory=list)
    payment_terms: Optional[str] = Field(default=None, max_length=512)
    corrigendum_seq: int = Field(default=0, ge=0)
    related_evidence_ids: list[str] = Field(default_factory=list,
                                            description="e.g. an award lists the notice it awards")
    attachments: list[TenderAttachment] = Field(default_factory=list)
    relationship_stage: Optional[RelationshipStage] = None
    do_not_contact: bool = False
    extraction_method: Literal["STRUCTURED", "SLM", "MANUAL"] = "STRUCTURED"
    extraction_model_run_id: Optional[str] = None
    evidence_snippets: list[EvidenceSnippet] = Field(default_factory=list)
    raw_sha256: Optional[str] = Field(default=None, pattern=_SHA)
    synthetic: bool = False


class MarketBatchPayload(_Strict):
    model_config = ConfigDict(extra="forbid", title="MarketBatchPayload.v1")
    batch_id: str = Field(pattern=_ID)
    connector: SourceConnector
    retrieved_at: datetime
    query: dict[str, Any] = Field(default_factory=dict)
    raw_payload_sha256: Optional[str] = Field(default=None, pattern=_SHA)
    observations: list[MarketObservation] = Field(min_length=1)


class BuyerRecord(_Strict):
    """A named-buyer fact from the CRM or a customer interview."""
    record_id: str = Field(pattern=_ID)
    record_type: Literal["CRM_ACCOUNT", "INTERVIEW"]
    buyer_name: str = Field(min_length=1, max_length=512)
    buyer_identifiers: dict[str, str] = Field(default_factory=dict)
    buyer_country: Optional[str] = Field(default=None, pattern=r"^[A-Z]{2}$")
    buyer_type: Optional[Literal["PUBLIC", "PRIVATE"]] = None
    relationship_stage: RelationshipStage = "NONE"
    recorded_at: datetime
    service_categories: list[str] = Field(default_factory=list)
    stated_need: Optional[str] = Field(default=None, max_length=4000)
    stated_budget: Optional[MarketAmount] = None
    budget_status: Optional[BudgetStatus] = None
    do_not_contact: bool = False
    source_url: Optional[str] = Field(default=None, pattern=r"^(https?|n0)://", max_length=2048)
    synthetic: bool = False


class BuyerRecordPayload(_Strict):
    model_config = ConfigDict(extra="forbid", title="BuyerRecordPayload.v1")
    batch_id: str = Field(pattern=_ID)
    connector: Literal["CRM", "INTERVIEW"]
    retrieved_at: datetime
    records: list[BuyerRecord] = Field(min_length=1)


class TenderDeadline(_Strict):
    evidence_id: str
    deadline_at: datetime
    source_url: Optional[str] = None


class ShortlistEntry(_Strict):
    rank: int = Field(ge=1)
    account_id: str
    legal_name: str
    country: Optional[str] = None
    heuristic_priority_score: float = Field(ge=0, le=100)
    score_kind: Literal["HEURISTIC_PRIORITIZATION"] = "HEURISTIC_PRIORITIZATION"
    tender_deadlines: list[TenderDeadline] = Field(default_factory=list)
    eligibility_risk_flags: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(min_length=1)


class CommercialShortlistPayload(_Strict):
    model_config = ConfigDict(extra="forbid", title="CommercialShortlistPayload.v1")
    report_id: str = Field(pattern=_ID)
    report_sha256: str = Field(pattern=_SHA)
    generated_at: datetime
    entries: list[ShortlistEntry] = Field(min_length=1)
    disclaimer: str


class VerifiedSpendItem(_Strict):
    evidence_id: str
    account_id: str
    spend_type: SpendType
    synthetic: bool
    attestation_id: Optional[str] = None
    original: MarketAmount
    fx_rate: Decimal = Field(gt=0)
    fx_as_of: date
    amount_base: Decimal = Field(ge=0)
    source_url: Optional[str] = None


class CostedAction(_Strict):
    action_id: str
    action_type: str
    target_account_id: Optional[str] = None
    execution_cost: Decimal = Field(ge=0)
    net_value: Decimal
    status: Literal["RECOMMENDED_NOT_EXECUTED"] = "RECOMMENDED_NOT_EXECUTED"


class SamEvidencePayload(_Strict):
    model_config = ConfigDict(extra="forbid", title="SamEvidencePayload.v1")
    report_id: str = Field(pattern=_ID)
    report_sha256: str = Field(pattern=_SHA)
    as_of: date
    base_currency: str = Field(pattern=r"^[A-Z]{3}$")
    verified_real_sam: Decimal = Field(ge=0)
    indicative_spend: Decimal = Field(ge=0)
    hypothetical_addressable_spend: Decimal = Field(ge=0)
    verified_items: list[VerifiedSpendItem] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    costed_actions: list[CostedAction] = Field(default_factory=list)


# ----------------------------------------------------------------------------------------------
# product & design digital thread payloads (N2 PRD → N3 design releases → N4 / N5)
# ----------------------------------------------------------------------------------------------
DesignScope = Literal["schematic_pcb", "circuit_simulation", "mechanical_thermal"]
DESIGN_SCOPES = ("schematic_pcb", "circuit_simulation", "mechanical_thermal")
REQUIRED_CORNERS = ("vin_min", "vin_max", "temp_max", "load_max")
NATIVE_EXTENSIONS = {".kicad_sch": "SCHEMATIC", ".kicad_pcb": "PCB", ".kicad_pro": "PROJECT", ".step": "GEOMETRY",
                     ".stp": "GEOMETRY", ".fcstd": "GEOMETRY", ".sldprt": "GEOMETRY", ".sldasm": "GEOMETRY",
                     ".cir": "NETLIST", ".sp": "NETLIST", ".spice": "NETLIST", ".net": "NETLIST", ".lib": "NETLIST"}
NON_NATIVE_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".tif", ".tiff", ".webp"}


class DesignConstraints(_Strict):
    """Engineering constraints N2 locks into the PRD; N3 verifies designs against them."""
    input_voltage_min_v: Optional[float] = None
    input_voltage_max_v: Optional[float] = None
    ambient_min_c: Optional[float] = None
    ambient_max_c: Optional[float] = None
    load_max_a: Optional[float] = Field(default=None, ge=0)
    load_max_w: Optional[float] = Field(default=None, ge=0)
    derating_factor: float = Field(default=0.8, gt=0, le=1)
    min_clearance_mm: Optional[float] = Field(default=None, gt=0)
    min_creepage_mm: Optional[float] = Field(default=None, gt=0)
    thermal_margin_c: float = Field(default=10.0, ge=0)
    max_pcb_layers: Optional[int] = Field(default=None, ge=1, le=64)


class TestLimit(_Strict):
    __test__ = False
    parameter: str = Field(pattern=_PARAM)
    min: Optional[float] = None
    max: Optional[float] = None
    unit: Optional[str] = None
    requirement_ref: Optional[str] = None


class Hazard(_Strict):
    """A product hazard from the N2 risk analysis. Every requirement it links becomes safety-critical
    for N4: 100 % physical verification coverage before release."""
    hazard_id: str = Field(pattern=_ID)
    description: str = Field(min_length=3)
    severity: int = Field(ge=1, le=10)
    requirement_ids: list[str] = Field(min_length=1)
    standard_ref: Optional[str] = Field(default=None, description="e.g. IEC 62368-1 clause 5.4.9")


class PRDPayload(_Strict):
    model_config = ConfigDict(extra="forbid", title="PRDPayload.v1")
    prd_id: str = Field(pattern=_ID)
    product_name: str = Field(min_length=1)
    customer_name: str = Field(min_length=1)
    product_category: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    source_rfq: Optional[str] = Field(default=None, description="ARTIFACT_ID@REVISION of the N1 RFQ seed")
    requirements: list[Requirement] = Field(min_length=1)
    design_constraints: DesignConstraints
    design_scopes: list[DesignScope] = Field(min_length=1)
    test_limits: list[TestLimit] = Field(default_factory=list)
    compliance_standards: list[str] = Field(default_factory=list)
    hazards: list[Hazard] = Field(default_factory=list)
    safety_critical_requirement_ids: list[str] = Field(default_factory=list, description="in addition to every "
                                                       "requirement a hazard links")
    authored_by: str


class DesignFile(_Strict):
    path: str = Field(min_length=1, max_length=512)
    role: str = Field(pattern=r"^[A-Z][A-Z_]{1,31}$")
    sha256: Optional[str] = Field(default=None, pattern=_SHA)
    tool: Optional[str] = None
    tool_version: Optional[str] = None
    blob_uri: Optional[str] = None


class CheckSummary(_Strict):
    tool: str
    tool_version: Optional[str] = None
    critical: int = Field(ge=0)
    errors: int = Field(ge=0)
    warnings: int = Field(ge=0)
    report_sha256: Optional[str] = Field(default=None, pattern=_SHA)
    waived: int = Field(default=0, ge=0)


class CornerResult(_Strict):
    name: str
    converged: bool
    parameters: dict[str, float] = Field(default_factory=dict)
    measures: dict[str, float] = Field(default_factory=dict)
    log_sha256: Optional[str] = Field(default=None, pattern=_SHA)


class SimulationEvidence(_Strict):
    solver: Optional[str] = None
    solver_version: Optional[str] = None
    required_corners: list[str] = Field(default_factory=lambda: list(REQUIRED_CORNERS))
    corners: list[CornerResult] = Field(default_factory=list)


class ThermalEvidence(_Strict):
    solver: Optional[str] = None
    solver_version: Optional[str] = None
    boundary_conditions: dict[str, Any] = Field(default_factory=dict)
    ambient_c: Optional[float] = None
    load_w: Optional[float] = None
    max_temps_c: dict[str, float] = Field(default_factory=dict)
    limits_c: dict[str, float] = Field(default_factory=dict)
    margin_c: float = Field(default=10.0, ge=0)


class DeratingCheck(_Strict):
    refdes: str
    parameter: str
    stress: float
    rating: float = Field(gt=0)
    factor: float = Field(gt=0, le=1)


class ClearanceCheck(_Strict):
    kind: Literal["clearance", "creepage"]
    required_mm: float = Field(gt=0)
    actual_mm: float = Field(ge=0)


class DesignReleasePayload(_Strict):
    """N3 → N4: a design-authority-approved design package for the next controlled engineering step.
    It is not safety certification, laboratory qualification or mass-manufacturing release."""
    model_config = ConfigDict(extra="forbid", title="DesignReleasePayload.v1")
    release_id: str = Field(pattern=_ID)
    scope: DesignScope
    prd_ref: str = Field(description="PRD ARTIFACT_ID@REVISION the design is locked to")
    prd_payload_sha256: str = Field(pattern=_SHA)
    design_revision: str = Field(min_length=1, max_length=16)
    files: list[DesignFile] = Field(min_length=1)
    erc: Optional[CheckSummary] = None
    drc: Optional[CheckSummary] = None
    simulation: Optional[SimulationEvidence] = None
    thermal: Optional[ThermalEvidence] = None
    derating: list[DeratingCheck] = Field(default_factory=list)
    clearance: list[ClearanceCheck] = Field(default_factory=list)
    test_limits: list[TestLimit] = Field(default_factory=list)
    assessment_ids: list[str] = Field(min_length=1)
    review_ids: list[str] = Field(min_length=1)
    approval_scope: str


class BomItem(_Strict):
    model_config = ConfigDict(extra="forbid", title="bom_item.v1")
    schema_version: Literal["bom_item.v1"] = "bom_item.v1"
    line: int = Field(ge=1)
    mpn: Optional[str] = None
    manufacturer: Optional[str] = None
    refdes: list[str] = Field(min_length=1)
    qty: int = Field(ge=0)
    value: Optional[str] = None
    footprint: Optional[str] = None
    description: Optional[str] = None
    dnp: bool = False


class BomReleasePayload(_Strict):
    """N3 → N5: engineering BOM locked to one design revision."""
    model_config = ConfigDict(extra="forbid", title="BomReleasePayload.v1")
    release_id: str = Field(pattern=_ID)
    scope: DesignScope
    prd_ref: str
    design_revision: str = Field(min_length=1, max_length=16)
    pcb_revision: Optional[str] = Field(default=None, max_length=16)
    bom_source_sha256: str = Field(pattern=_SHA)
    items: list[BomItem] = Field(min_length=1)


VerificationMethod = Literal["SIM", "BENCH_ATE", "ACCREDITED_LAB", "INSPECTION"]
VERIFICATION_METHODS = ("SIM", "BENCH_ATE", "ACCREDITED_LAB", "INSPECTION")
MeasurementProvenance = Literal["PHYSICAL_ATE", "PHYSICAL_BENCH", "PHYSICAL_INSPECTION", "ACCREDITED_LAB",
                                "SIMULATION", "SYNTHETIC", "MOCK_FIXTURE"]
PHYSICAL_PROVENANCE = ("PHYSICAL_ATE", "PHYSICAL_BENCH", "PHYSICAL_INSPECTION", "ACCREDITED_LAB")
METHOD_PROVENANCE: dict[str, tuple[str, ...]] = {
    "SIM": ("SIMULATION",), "BENCH_ATE": ("PHYSICAL_ATE", "PHYSICAL_BENCH"), "ACCREDITED_LAB": ("ACCREDITED_LAB",),
    "INSPECTION": ("PHYSICAL_INSPECTION",)}
RELIABILITY_RESEARCH_LABEL = "RESEARCH_ONLY_STATISTICAL_ILLUSTRATION"
PrioritizationMethod = Literal["CUSTOM_ATTENTION_HEURISTIC", "AIAG_VDA_AP_LICENSED"]


class VerifiedMeasurement(_Strict):
    """One measured (or simulated) characteristic with its guard-banded verdict. ``value``, the
    expanded uncertainty and both limits share ``unit``."""
    measurement_id: str
    test_id: str
    characteristic: str
    unit_serial: Optional[str] = None
    value: Decimal
    unit: str
    expanded_uncertainty: Optional[Decimal] = Field(default=None, ge=0)
    coverage_factor: float = Field(default=2.0, gt=0)
    lower_limit: Optional[Decimal] = None
    upper_limit: Optional[Decimal] = None
    verdict: Literal["PASS", "FAIL", "INDETERMINATE"]
    method: VerificationMethod
    provenance: MeasurementProvenance
    instrument_id: Optional[str] = None
    calibration_id: Optional[str] = None
    calibration_due: Optional[datetime] = None
    captured_at: datetime
    raw_sha256: Optional[str] = Field(default=None, pattern=_SHA)


class TraceEntry(_Strict):
    requirement_id: str
    safety_critical: bool
    hazard_ids: list[str] = Field(default_factory=list)
    test_ids: list[str] = Field(default_factory=list)
    methods: list[VerificationMethod] = Field(default_factory=list)
    verdict: Literal["VERIFIED", "NOT_VERIFIED"]


class AttributeInspection(_Strict):
    """Attribute (go / no-go) inspection of one unit, e.g. AOI. Model detections never decide it: the
    verdict is a human adjudication, and every model-flagged or ambiguous call must be adjudicated."""
    test_id: str
    unit_serial: str
    method: VerificationMethod = "INSPECTION"
    provenance: MeasurementProvenance = "PHYSICAL_INSPECTION"
    defects_confirmed: int = Field(ge=0)
    unadjudicated: int = Field(ge=0)
    human_verdict: Literal["ACCEPT", "REJECT"]
    adjudicator: str
    evidence_sha256: str = Field(pattern=_SHA)


class VerificationReleasePayload(_Strict):
    """N4 → N8 / N6 / N7: a design-revision-locked verification package. It is not product safety
    certification unless an accredited lab certificate in it says so for its stated scope."""
    model_config = ConfigDict(extra="forbid", title="VerificationReleasePayload.v1")
    release_id: str = Field(pattern=_ID)
    target: Literal["N6", "N7", "N8"]
    prd_ref: str
    design_refs: list[str] = Field(min_length=1, description="N3 DESIGN_RELEASE ARTIFACT_ID@REVISION")
    design_revision: str = Field(min_length=1, max_length=16)
    pcb_revision: Optional[str] = Field(default=None, max_length=16)
    plan_id: str
    plan_sha256: str = Field(pattern=_SHA)
    assessment_id: str
    evidence_sha256: str = Field(pattern=_SHA)
    review_id: str
    approved_by: str
    evidence_submitters: list[str] = Field(default_factory=list)
    verification_status: Literal["VERIFIED", "NOT_VERIFIED"]
    measurements: list[VerifiedMeasurement] = Field(min_length=1)
    attribute_inspections: list[AttributeInspection] = Field(default_factory=list)
    traceability: list[TraceEntry] = Field(min_length=1)
    unit_serials: list[str] = Field(default_factory=list)
    prioritization_method: PrioritizationMethod = "CUSTOM_ATTENTION_HEURISTIC"
    ap_license_ref: Optional[str] = None
    reliability: Optional[dict[str, Any]] = None
    control_plan: list[dict[str, Any]] = Field(default_factory=list)
    lab_certificates: list[dict[str, Any]] = Field(default_factory=list)
    commercial: Optional[dict[str, Any]] = None
    finance: Optional[dict[str, Any]] = None
    approval_scope: str


PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "RFQ": RFQPayload, "DATASHEET": DatasheetPayload, "EDA_PACKAGE": EDAPackagePayload,
    "SUPPLIER_QUOTE": SupplierQuotePayload, "TEST_MEASUREMENT": TestMeasurementPayload,
    "MARKET_FEED": MarketFeedPayload, "FINANCE_RECORD": FinanceRecordPayload, "TELEMETRY": TelemetryPayload,
    "MODEL_OUTPUT": ModelOutputPayload, "MARKET_BATCH": MarketBatchPayload, "BUYER_RECORD": BuyerRecordPayload,
    "COMMERCIAL_SHORTLIST": CommercialShortlistPayload, "SAM_EVIDENCE": SamEvidencePayload,
    "PRD": PRDPayload, "DESIGN_RELEASE": DesignReleasePayload, "BOM_RELEASE": BomReleasePayload,
    "VERIFICATION_RELEASE": VerificationReleasePayload,
}
REQUIREMENT_LISTS = {"RFQ": "requirements", "DATASHEET": "requirements", "TEST_MEASUREMENT": "measurements",
                     "TELEMETRY": "readings", "MODEL_OUTPUT": "requirements", "PRD": "requirements"}


# ----------------------------------------------------------------------------------------------
# 2. Artifact.v1
# ----------------------------------------------------------------------------------------------
class FileRef(_Strict):
    uri: str = Field(pattern=r"^n0://(quarantine|released)/[A-Za-z0-9._:\-]+/[0-9a-f]{64}$")
    sha256: str = Field(pattern=_SHA)
    mime: Optional[str] = None
    size_bytes: Optional[int] = Field(default=None, ge=0)
    blob_id: Optional[str] = None


class Artifact(_Strict):
    model_config = ConfigDict(extra="forbid", title="Artifact.v1")
    tenant_id: str = Field(pattern=_ID)
    project_id: str = Field(pattern=_ID)
    artifact_id: str = Field(pattern=_ID)
    schema_version: Literal["Artifact.v1"] = "Artifact.v1"
    artifact_type: ArtifactType
    source_id: str = Field(pattern=_ID)
    processing_purpose: ProcessingPurpose
    release_stage: ReleaseStage
    revision: str = Field(min_length=1, max_length=16)
    synthetic: bool
    source_sha256: str = Field(pattern=_SHA, description="SHA-256 of the primary file, or of the canonical "
                                                         "payload JSON when the artifact has no files")
    file_refs: list[FileRef] = Field(default_factory=list)
    payload: dict[str, Any]
    source_artifact_ids: list[str] = Field(default_factory=list,
                                           description="upstream artifacts this one derives from, as ID@REVISION")

    @field_validator("source_artifact_ids")
    @classmethod
    def _pinned(cls, v):
        for ref in v:
            if "@" not in ref or not ref.split("@", 1)[0] or not ref.split("@", 1)[1]:
                raise ValueError(f"upstream reference {ref!r} must be pinned as ARTIFACT_ID@REVISION")
        return v


# ----------------------------------------------------------------------------------------------
# 4. ModelRun.v1
# ----------------------------------------------------------------------------------------------
class TokenUsage(_Strict):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)


class ModelRun(_Strict):
    model_config = ConfigDict(extra="forbid", title="ModelRun.v1")
    schema_version: Literal["ModelRun.v1"] = "ModelRun.v1"
    run_id: str = Field(pattern=_ID)
    tenant_id: str = Field(pattern=_ID)
    model_name: str
    model_version: str
    provider: str
    prompt_revision: str
    input_hash: str = Field(pattern=_SHA)
    output_payload: dict[str, Any]
    token_usage: TokenUsage
    estimated_cost_usd: float = Field(ge=0)
    status: Literal["SUCCEEDED", "FAILED", "REFUSED"] = "SUCCEEDED"
    source_id: Optional[str] = None
    blob_id: Optional[str] = None
    started_at: datetime
    completed_at: datetime


# ----------------------------------------------------------------------------------------------
# 5./6. VendorWorkPackage.v1 / VendorResult.v1
# ----------------------------------------------------------------------------------------------
class ExpectedOutput(_Strict):
    name: str
    format: str
    schema_ref: Optional[str] = None


class VendorWorkPackage(_Strict):
    model_config = ConfigDict(extra="forbid", title="VendorWorkPackage.v1")
    schema_version: Literal["VendorWorkPackage.v1"] = "VendorWorkPackage.v1"
    work_package_id: str = Field(pattern=_ID)
    tenant_id: str = Field(pattern=_ID)
    project_id: str = Field(pattern=_ID)
    vendor_id: str = Field(pattern=_ID)
    connector_type: ConnectorType
    approved_input_ids: list[str] = Field(min_length=1, description="ARTIFACT_ID@REVISION, each must be PASS")
    budget_cap: Money
    expected_outputs: list[ExpectedOutput] = Field(min_length=1)


class VendorResult(_Strict):
    model_config = ConfigDict(extra="forbid", title="VendorResult.v1")
    schema_version: Literal["VendorResult.v1"] = "VendorResult.v1"
    work_package_id: str = Field(pattern=_ID)
    native_file_uris: list[str] = Field(min_length=1)
    result_manifest: dict[str, Any]
    checksum_sha256: str = Field(pattern=_SHA)
    tool_version: str = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------------------------------------
# 7. GatewayDecision.v1
# ----------------------------------------------------------------------------------------------
class RuleViolation(_Strict):
    rule_id: str
    code: str
    severity: Severity
    category: str
    stage: Stage
    message: str
    field: Optional[str] = None


class RightsDecision(_Strict):
    status: Literal["GRANTED", "DENIED"]
    source_id: str
    approval_status: Optional[ApprovalStatus] = None
    license: Optional[str] = None
    processing_purpose: str
    purpose_permitted: bool
    llm_processing_allowed: bool
    vendor_transmission_allowed: bool
    training_allowed: bool
    reasons: list[str] = Field(default_factory=list)


class GatewayDecision(_Strict):
    model_config = ConfigDict(extra="forbid", title="GatewayDecision.v1")
    schema_version: Literal["GatewayDecision.v1"] = "GatewayDecision.v1"
    decision_id: str
    tenant_id: str
    artifact_id: str
    revision: str
    decision: Decision
    violated_rules: list[RuleViolation]
    missing_fields: list[str]
    rights_decision: RightsDecision
    explanation: str
    allowed_destinations: list[DownstreamNode]
    validator_version: str
    quality_score: float = Field(ge=0, le=100, description="advisory only; never overrides a hard policy")
    origin_node: NodeId = "N0"
    decided_at: datetime
    resolved_by_approval_id: Optional[str] = Field(
        default=None, description="set when a human REVIEW_RESOLUTION approval turned a REVIEW into PASS/REWORK; "
                                  "BLOCK decisions are never resolvable")
    qualification_disclaimer: str = QUALIFICATION_DISCLAIMER


# ----------------------------------------------------------------------------------------------
# 8. ApprovalEvent.v1
# ----------------------------------------------------------------------------------------------
class ApprovalEvent(_Strict):
    model_config = ConfigDict(extra="forbid", title="ApprovalEvent.v1")
    schema_version: Literal["ApprovalEvent.v1"] = "ApprovalEvent.v1"
    approval_id: str
    tenant_id: str
    artifact_id: str = Field(description="artifact id, or source:<source_id> for rights approvals")
    exact_revision: str
    approver_id: str
    approver_role: str
    decision: Literal["APPROVED", "REJECTED"]
    scope: Literal["SOURCE_RIGHTS", "RELEASE", "REVIEW_RESOLUTION", "FEEDBACK", "EVIDENCE_ATTESTATION"]
    reason: str = Field(min_length=5)
    timestamp_utc: datetime
    prev_hash: str = Field(pattern=_SHA)
    event_hash: str = Field(pattern=_SHA)


# ----------------------------------------------------------------------------------------------
# extraction draft
# ----------------------------------------------------------------------------------------------
class RFQDraft(_Strict):
    model_config = ConfigDict(extra="forbid", title="RFQDraft.v1")
    schema_version: Literal["RFQDraft.v1"] = "RFQDraft.v1"
    draft_id: str
    tenant_id: str
    source_id: str
    blob_id: str
    customer_name: Optional[str]
    product_category: Optional[str]
    quantity: Optional[int]
    requirements: list[Requirement]
    missing_fields: list[str]
    evidence_snippets: list[EvidenceSnippet]
    model_run_id: str


# ----------------------------------------------------------------------------------------------
# API envelopes
# ----------------------------------------------------------------------------------------------
class SourceRegisterRequest(_Strict):
    source_id: str = Field(pattern=_ID)
    tenant_id: str = Field(pattern=_ID)
    publisher: str = Field(min_length=1)
    license: str = Field(min_length=1)
    permitted_use: list[ProcessingPurpose] = Field(min_length=1)
    data_origin: Literal["REAL", "SYNTHETIC", "MIXED"] = "REAL"
    confidentiality: Confidentiality = "CONFIDENTIAL"
    allowed_vendor_ids: list[str] = Field(default_factory=list)
    valid_until: Optional[datetime] = None


class SourceApprovalRequest(_Strict):
    decision: Literal["APPROVED", "REJECTED", "REVOKED"] = "APPROVED"
    reason: str = Field(min_length=5)


class SourceApprovalResponse(_Strict):
    source: SourceRecord
    approval_event: ApprovalEvent


class BlobRecord(_Strict):
    blob_id: str
    tenant_id: str
    source_id: str
    sha256: str
    size_bytes: int
    filename: str
    declared_mime: Optional[str]
    sniffed_mime: str
    status: Literal["QUARANTINED", "SCAN_PASSED", "REJECTED", "RELEASED"]
    scan: dict[str, Any]
    file_ref: FileRef
    created_at: datetime


class EvaluateRequest(_Strict):
    artifact: Artifact
    origin_node: NodeId = "N0"
    requested_destinations: Optional[list[DownstreamNode]] = None


class Provenance(_Strict):
    source_id: str
    source_sha256: str
    revision: str
    payload_sha256: str
    file_refs: list[FileRef]
    upstream: list[str]
    model_run_ids: list[str]


class QualityReport(_Strict):
    violated_rules: list[RuleViolation]
    error_codes: list[str]
    missing_fields: list[str]
    checks_run: list[str]
    derived_values: list[Requirement]
    pii_findings: list[dict[str, Any]]
    quality_score: float


class ReleaseManifest(_Strict):
    model_config = ConfigDict(extra="forbid", title="ReleaseManifest.v1")
    schema_version: Literal["ReleaseManifest.v1"] = "ReleaseManifest.v1"
    manifest_id: str
    tenant_id: str
    artifact_id: str
    revision: str
    status: Literal["RELEASABLE", "NOT_RELEASED", "RELEASED"]
    source_node: NodeId
    allowed_destinations: list[DownstreamNode]
    target_nodes: list[DownstreamNode] = Field(default_factory=list)
    source_sha256: str
    payload_sha256: str
    decision_id: str
    approval_id: Optional[str] = None
    released_by: Optional[str] = None
    issued_at: datetime
    validator_version: str
    manifest_sha256: Optional[str] = None


class AuditEventOut(_Strict):
    event_id: str
    event_type: str
    tenant_id: str
    actor_id: str
    actor_roles: list[str]
    subject: str
    validator_version: str
    model_runs: list[dict[str, Any]]
    stage_timings_ms: dict[str, float]
    details: dict[str, Any]
    created_at: datetime
    prev_hash: str
    event_hash: str


class EvaluationResult(_Strict):
    """The seven N0 deliverables."""
    canonical_artifact: Optional[Artifact]
    provenance: Provenance
    quality_report: QualityReport
    rights_decision: RightsDecision
    decision: GatewayDecision
    release_manifest: ReleaseManifest
    audit_event: AuditEventOut


class ExtractionRequest(_Strict):
    tenant_id: str = Field(pattern=_ID)
    project_id: str = Field(pattern=_ID)
    source_id: str = Field(pattern=_ID)
    blob_id: str
    target: Literal["RFQDraft"] = "RFQDraft"


class ExtractionResponse(_Strict):
    draft: RFQDraft
    model_run: ModelRun
    violated_rules: list[RuleViolation]
    missing_fields: list[str]
    decision_preview: Decision
    pii_findings: list[dict[str, Any]]
    parser: str


class ApprovalRequest(_Strict):
    artifact_id: str
    exact_revision: str
    scope: Literal["RELEASE", "REVIEW_RESOLUTION"]
    decision: Literal["APPROVED", "REJECTED"]
    reason: str = Field(min_length=5)


class ApprovalResponse(_Strict):
    approval_event: ApprovalEvent
    resulting_decision: Optional[GatewayDecision] = None


class HandoffReleaseRequest(_Strict):
    artifact_id: str
    revision: str
    source_node: NodeId
    target_nodes: list[DownstreamNode] = Field(min_length=1)
    approval_id: Optional[str] = None


class Correction(_Strict):
    req_id: Optional[str] = None
    field: str
    corrected_value: Union[float, int, str, bool, None]
    corrected_unit: Optional[str] = None
    reason: str = Field(min_length=3)


class TrainingLabel(_Strict):
    label: str = Field(min_length=1)
    use_for_training: bool
    notes: Optional[str] = None


class FeedbackRequest(_Strict):
    artifact_id: str
    revision: str
    corrections: list[Correction] = Field(default_factory=list)
    override_reason: Optional[str] = None
    training_label: Optional[TrainingLabel] = None


class FeedbackRecord(_Strict):
    feedback_id: str
    tenant_id: str
    artifact_id: str
    revision: str
    reviewer_id: str
    corrections: list[Correction]
    override_reason: Optional[str]
    training_label: Optional[TrainingLabel]
    training_eligible: bool
    approval_event: ApprovalEvent
    created_at: datetime


class TransmissionDecision(_Strict):
    work_package_id: str
    authorized: bool
    violated_rules: list[RuleViolation]
    authorized_inputs: list[str]


class ArtifactView(_Strict):
    artifact_id: str
    revision: str
    latest_revision: str
    superseded: bool
    revisions: list[str]
    canonical_artifact: Artifact
    decision: GatewayDecision
    releases: list[ReleaseManifest]


class VendorResultDecision(_Strict):
    result_id: str
    work_package_id: str
    status: Literal["ACCEPTED", "REJECTED"]
    decision: Decision
    violated_rules: list[RuleViolation]
    checksum_verified: bool
    native_file_uris: list[str]


class LineageNode(_Strict):
    id: str
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]*$", description="source, blob, artifact, model_run, decision, "
                                                                "approval, release, node, work_package, "
                                                                "vendor_result, or an extension's kind (n1_*)")
    attrs: dict[str, Any] = Field(default_factory=dict)


class LineageEdge(_Strict):
    source: str
    target: str
    relation: str


class LineageGraph(_Strict):
    artifact_id: str
    tenant_id: str
    nodes: list[LineageNode]
    edges: list[LineageEdge]


SCHEMA_FAMILIES: dict[str, type[BaseModel]] = {
    "SourceRecord.v1": SourceRecord, "Artifact.v1": Artifact, "Requirement.v1": Requirement,
    "ModelRun.v1": ModelRun, "VendorWorkPackage.v1": VendorWorkPackage, "VendorResult.v1": VendorResult,
    "GatewayDecision.v1": GatewayDecision, "ApprovalEvent.v1": ApprovalEvent,
}
EXTRA_SCHEMAS: dict[str, type[BaseModel]] = {"RFQDraft.v1": RFQDraft, "ReleaseManifest.v1": ReleaseManifest,
                                             "EvaluationResult.v1": EvaluationResult, "bom_item.v1": BomItem}
