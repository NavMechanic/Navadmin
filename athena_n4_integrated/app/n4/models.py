"""N4 contracts → ``schemas/n4_*.schema.json`` (21 published families plus adapter input formats)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models import (AttributeInspection, Hazard, MeasurementProvenance, PrioritizationMethod,  # noqa: F401
                      Tolerance, VerificationMethod, VerifiedMeasurement)
from . import APPROVAL_SCOPE, HEURISTIC_LABEL

_ID = r"^[A-Za-z0-9][A-Za-z0-9._:\-]{1,127}$"
_SHA = r"^[0-9a-f]{64}$"
Decision = Literal["PASS", "REWORK", "BLOCK"]
Verdict = Literal["PASS", "FAIL", "INDETERMINATE", "NOT_TESTED"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ----------------------------------------------------------------------------------------------
# registries: methods (SOPs), instruments, calibration, accepted labs, qualifications
# ----------------------------------------------------------------------------------------------
class MethodRef(_Strict):
    method_id: str = Field(pattern=_ID)
    revision: str = Field(min_length=1, max_length=16)


class TestMethodRequest(_Strict):
    __test__ = False
    method_id: str = Field(pattern=_ID)
    revision: str = Field(min_length=1, max_length=16)
    title: str = Field(min_length=3)
    method: VerificationMethod
    characteristics: list[str] = Field(default_factory=list)
    summary: Optional[str] = Field(default=None, max_length=4000)


class N4TestMethod(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4TestMethod")
    method_id: str
    revision: str
    tenant_id: str
    title: str
    method: VerificationMethod
    characteristics: list[str]
    summary: Optional[str] = None
    document_sha256: Optional[str] = Field(default=None, pattern=_SHA)
    document_uri: Optional[str] = None
    status: Literal["DRAFT", "APPROVED", "WITHDRAWN"]
    authored_by: str
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None


class N4Instrument(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4Instrument")
    instrument_id: str = Field(pattern=_ID)
    tenant_id: str
    kind: Literal["DMM", "POWER_SUPPLY", "ELECTRONIC_LOAD", "OSCILLOSCOPE", "POWER_ANALYZER", "HIPOT_TESTER",
                  "INSULATION_TESTER", "TOUCH_CURRENT_METER", "THERMAL_CHAMBER", "AOI_STATION", "CALIPER",
                  "MEASURING_MICROSCOPE", "OTHER"]
    manufacturer: str
    model: str
    serial: str
    firmware: Optional[str] = None
    resource: Optional[str] = Field(default=None, description="VISA resource or TCPIP0::host::5025::SOCKET")
    registered_by: Optional[str] = None
    registered_at: Optional[datetime] = None


class CalibrationRequest(_Strict):
    calibration_id: str = Field(pattern=_ID)
    instrument_id: str = Field(pattern=_ID)
    certificate_number: str = Field(min_length=1)
    calibration_lab: str
    accreditation_ref: Optional[str] = Field(default=None, description="ISO/IEC 17025 accreditation of the cal lab")
    calibrated_at: datetime
    calibration_due: datetime
    uncertainty_statement: Optional[str] = None

    @model_validator(mode="after")
    def _order(self):
        if self.calibration_due <= self.calibrated_at:
            raise ValueError("calibration_due must be after calibrated_at")
        return self


class N4CalibrationRecord(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4CalibrationRecord")
    calibration_id: str
    tenant_id: str
    instrument_id: str
    instrument_serial: str
    certificate_number: str
    calibration_lab: str
    accreditation_ref: Optional[str] = None
    calibrated_at: datetime
    calibration_due: datetime
    uncertainty_statement: Optional[str] = None
    certificate_sha256: Optional[str] = Field(default=None, pattern=_SHA)
    certificate_uri: Optional[str] = None
    status: Literal["VALID", "REVOKED"]
    recorded_by: str
    recorded_at: datetime


class N4AcceptedLab(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4AcceptedLab")
    lab_id: str = Field(pattern=_ID)
    tenant_id: Optional[str] = None
    name: str
    accreditation_body: str
    accreditation_number: str
    scopes: list[str] = Field(min_length=1, description="standards / test methods the accreditation covers")
    valid_until: date
    approved_by: Optional[str] = None


class N4Qualification(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4Qualification")
    principal_id: str
    tenant_id: Optional[str] = None
    methods: list[VerificationMethod] = Field(min_length=1)
    valid_until: date
    basis: str = Field(min_length=3, description="training record / competence evidence")
    granted_by: Optional[str] = None
    granted_at: Optional[datetime] = None


# ----------------------------------------------------------------------------------------------
# N4.1 intake of the N3 design revision and N2 requirements
# ----------------------------------------------------------------------------------------------
class IntakeRequest(_Strict):
    prd_id: str = Field(pattern=_ID)
    project_id: str = Field(pattern=_ID)
    release_source_id: str = Field(pattern=_ID, description="N0 source the N4 releases are registered under "
                                                            "(must permit LIFECYCLE / COMMERCIAL / FINANCE)")


class DesignRef(_Strict):
    scope: str
    artifact: str = Field(description="DSN ARTIFACT_ID@REVISION")
    design_revision: str
    payload_sha256: str = Field(pattern=_SHA)
    manifest_id: str
    approval_id: str
    n3_release_id: str


class TraceRequirement(_Strict):
    req_id: str
    parameter: str
    value: Any = None
    unit: Optional[str] = None
    tolerance: Optional[Tolerance] = None
    value_origin: Optional[str] = Field(default=None, description="DERIVED values (N0 arithmetic) need no own test")
    safety_critical: bool
    hazard_ids: list[str] = Field(default_factory=list)


class N4DesignIntake(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4DesignIntake")
    schema_version: Literal["N4DesignIntake.v1"] = "N4DesignIntake.v1"
    intake_id: str
    tenant_id: str
    project_id: str
    prd_id: str
    prd_artifact: str
    prd_revision: str
    prd_payload_sha256: str = Field(pattern=_SHA)
    design_refs: list[DesignRef] = Field(min_length=1)
    design_revision: str
    pcb_revision: Optional[str] = None
    requirements: list[TraceRequirement]
    hazards: list[Hazard]
    simulation_corners: list[dict[str, Any]] = Field(default_factory=list)
    release_source_id: str
    synthetic: bool
    locked_by: str
    locked_at: datetime
    intake_sha256: Optional[str] = None
    approval_scope: str = APPROVAL_SCOPE


# ----------------------------------------------------------------------------------------------
# N4.2 test plan and traceability
# ----------------------------------------------------------------------------------------------
class AcceptanceLimit(_Strict):
    characteristic: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    unit: str
    lower: Optional[Decimal] = None
    upper: Optional[Decimal] = None

    @model_validator(mode="after")
    def _one(self):
        if self.lower is None and self.upper is None:
            raise ValueError("an acceptance limit needs a lower and / or an upper bound")
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError("lower limit exceeds upper limit")
        return self


class TestCaseSpec(_Strict):
    __test__ = False
    model_config = ConfigDict(extra="forbid", title="N4TestCase")
    test_id: str = Field(pattern=_ID)
    title: str = Field(min_length=3)
    requirement_ids: list[str] = Field(min_length=1)
    method: VerificationMethod
    acceptance: list[AcceptanceLimit] = Field(min_length=1)
    method_ref: Optional[MethodRef] = Field(default=None, description="approved SOP + revision (required to release)")
    owner: Optional[str] = Field(default=None, description="accountable engineer (required to release)")
    sample_size: int = Field(default=1, ge=1)
    conditions: dict[str, Any] = Field(default_factory=dict)
    sim_corner: Optional[str] = Field(default=None, description="SIM: N3 worst-case corner name, or 'all'")
    sim_model_uncertainty: Optional[Decimal] = Field(default=None, gt=0, description="SIM: model uncertainty "
                                                                                    "in the limit unit")


class TestPlanRequest(_Strict):
    __test__ = False
    model_config = ConfigDict(extra="forbid", title="N4TestPlanRequest")
    title: str = Field(min_length=3)
    test_cases: list[TestCaseSpec] = Field(min_length=1)


class N4TestPlan(_Strict):
    __test__ = False
    model_config = ConfigDict(extra="forbid", title="N4TestPlan")
    schema_version: Literal["N4TestPlan.v1"] = "N4TestPlan.v1"
    plan_id: str
    tenant_id: str
    intake_id: str
    title: str
    prd_artifact: str
    design_revision: str
    pcb_revision: Optional[str] = None
    test_cases: list[TestCaseSpec]
    synthetic: bool
    created_by: str
    created_at: datetime
    plan_sha256: Optional[str] = None
    approval_scope: str = APPROVAL_SCOPE


class TraceRow(_Strict):
    requirement_id: str
    parameter: str
    safety_critical: bool
    hazard_ids: list[str]
    test_ids: list[str]
    methods: list[VerificationMethod]
    owners: list[str]
    gaps: list[str]


class N4TraceabilityMatrix(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4TraceabilityMatrix")
    plan_id: str
    rows: list[TraceRow]
    safety_critical_total: int
    safety_critical_covered: int
    safety_critical_coverage_pct: str
    uncovered_safety_critical: list[str]
    uncovered_other: list[str]
    unknown_requirement_refs: list[str]
    complete: bool


# ----------------------------------------------------------------------------------------------
# N4.3 PFMEA, control plan, CAPA
# ----------------------------------------------------------------------------------------------
class DetectionControl(_Strict):
    description: str = Field(min_length=3)
    test_id: Optional[str] = None


class PfmeaAction(_Strict):
    action_id: str = Field(pattern=_ID)
    description: str = Field(min_length=3)
    owner: str
    due: Optional[date] = None
    capa_id: Optional[str] = None
    status: Literal["OPEN", "DONE", "VERIFIED"] = "OPEN"


class PfmeaEntryInput(_Strict):
    entry_id: str = Field(pattern=_ID)
    process_step: str
    function: str
    failure_mode: str = Field(min_length=3)
    effect: str = Field(min_length=3)
    cause: str = Field(min_length=3)
    prevention_controls: list[str] = Field(default_factory=list)
    detection_controls: list[DetectionControl] = Field(default_factory=list)
    severity: int = Field(ge=1, le=10)
    occurrence: int = Field(ge=1, le=10)
    detection: int = Field(ge=1, le=10)
    hazard_ids: list[str] = Field(default_factory=list)
    characteristic: Optional[str] = None
    special_characteristic: bool = False
    actions: list[PfmeaAction] = Field(default_factory=list)
    adopted_from_suggestion: Optional[str] = Field(default=None, description="N4Suggestion the human adopted")


class N4PfmeaEntry(PfmeaEntryInput):
    model_config = ConfigDict(extra="forbid", title="N4PfmeaEntry")
    rated_by: str = Field(description="the human who set S / O / D (never a model)")
    rpn_illustrative: int
    attention_level: Literal["HIGH", "MEDIUM", "LOW"]
    attention_basis: str
    action_priority: Optional[Literal["H", "M", "L"]] = Field(default=None, description="only with a licensed table")


class PfmeaRequest(_Strict):
    entries: list[PfmeaEntryInput] = Field(min_length=1)


class N4Pfmea(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4Pfmea")
    pfmea_id: str
    tenant_id: str
    plan_id: str
    revision: int
    entries: list[N4PfmeaEntry]
    prioritization_method: PrioritizationMethod
    prioritization_label: str = HEURISTIC_LABEL
    ap_license_ref: Optional[str] = None
    created_by: str
    created_at: datetime
    pfmea_sha256: Optional[str] = None


class ControlPlanItem(_Strict):
    characteristic: str
    test_id: Optional[str] = None
    specification: Optional[AcceptanceLimit] = None
    measurement_method: str
    sample_size: int = Field(ge=1)
    frequency: str
    reaction_plan: str = Field(min_length=3)
    special_characteristic: bool = False


class ControlPlanRequest(_Strict):
    items: list[ControlPlanItem] = Field(min_length=1)


class N4ControlPlan(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4ControlPlan")
    control_plan_id: str
    tenant_id: str
    plan_id: str
    revision: int
    items: list[ControlPlanItem]
    created_by: str
    created_at: datetime


class CapaRequest(_Strict):
    capa_id: str = Field(pattern=_ID)
    plan_id: str
    title: str = Field(min_length=3)
    pfmea_entry_ids: list[str] = Field(min_length=1)
    root_cause: str = Field(min_length=3)
    corrective_action: str = Field(min_length=3)
    owner: str


class CapaVerifyRequest(_Strict):
    evidence_id: str
    effective: bool
    notes: str = Field(min_length=3)


class N4Capa(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4Capa")
    capa_id: str
    tenant_id: str
    plan_id: str
    title: str
    pfmea_entry_ids: list[str]
    root_cause: str
    corrective_action: str
    owner: str
    status: Literal["OPEN", "VERIFIED_EFFECTIVE", "VERIFIED_INEFFECTIVE"]
    opened_by: str
    opened_at: datetime
    verified_by: Optional[str] = None
    verified_at: Optional[datetime] = None
    verification_evidence_id: Optional[str] = None
    verification_evidence_sha256: Optional[str] = None
    notes: Optional[str] = None


# ----------------------------------------------------------------------------------------------
# N4.4 evidence intake: adapter input formats and records
# ----------------------------------------------------------------------------------------------
class ScpiInstrumentBlock(_Strict):
    resource: Optional[str] = None
    idn: str = Field(min_length=3, description="*IDN? response: manufacturer,model,serial,firmware")
    calibration_id: str


class ScpiRecord(_Strict):
    t: datetime = Field(description="UTC timestamp of the query")
    command: str
    response: str
    characteristic: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    unit: str
    expanded_uncertainty: Decimal = Field(gt=0)
    k: float = Field(default=2.0, gt=0)


class LogAttachment(_Strict):
    path: str
    sha256: str = Field(pattern=_SHA)
    role: Literal["WAVEFORM", "RAW_LOG", "IMAGE", "OTHER"] = "WAVEFORM"


class ScpiLog(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4ScpiLog")
    format: Literal["athena-scpi-log/1"] = "athena-scpi-log/1"
    station_id: str
    operator: Optional[str] = None
    test_id: str
    unit_serial: str
    method_ref: Optional[MethodRef] = None
    instrument: ScpiInstrumentBlock
    records: list[ScpiRecord] = Field(min_length=1)
    attachments: list[LogAttachment] = Field(default_factory=list)
    claimed_provenance: Literal["PHYSICAL", "SYNTHETIC"] = "PHYSICAL"
    capture: Literal["LIVE_DRIVER", "ATE_SOFTWARE_EXPORT"] = "ATE_SOFTWARE_EXPORT"


class LabBlock(_Strict):
    name: str
    accreditation_body: str
    accreditation_number: str


class LabResult(_Strict):
    test_id: str
    characteristic: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    unit_serial: str
    value: Decimal
    unit: str
    expanded_uncertainty: Decimal = Field(gt=0)
    k: float = Field(default=2.0, gt=0)
    instrument_id: str
    calibration_id: str
    calibration_due: datetime
    captured_at: datetime
    method_ref: Optional[MethodRef] = None
    standard_clause: Optional[str] = None


class LabReportManifest(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4LabReportManifest")
    format: Literal["athena-lab-report/1"] = "athena-lab-report/1"
    lab: LabBlock
    certificate_number: str = Field(min_length=3)
    report_revision: str = "1"
    issued_at: datetime
    product: str
    pcb_revision: str = Field(min_length=1, max_length=16)
    design_artifact: Optional[str] = None
    report_sha256: str = Field(pattern=_SHA)
    results: list[LabResult] = Field(min_length=1)
    claimed_provenance: Literal["PHYSICAL", "SYNTHETIC"] = "PHYSICAL"


class AoiModelBlock(_Strict):
    name: str
    version: str
    framework: str = "ultralytics-yolov8"
    weights_sha256: str = Field(pattern=_SHA)


class AoiRawDetection(_Strict):
    cls: str = Field(alias="class")
    confidence: float = Field(ge=0, le=1)
    bbox_xyxy: list[float] = Field(min_length=4, max_length=4)
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class AoiImage(_Strict):
    path: str
    sha256: str = Field(pattern=_SHA)
    detections: list[AoiRawDetection] = Field(default_factory=list)


class AoiInference(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4AoiInference")
    format: Literal["athena-aoi/1"] = "athena-aoi/1"
    station_id: str
    instrument_id: str
    test_id: str
    unit_serial: str
    pcb_revision: str
    captured_at: datetime
    model: AoiModelBlock
    images: list[AoiImage] = Field(min_length=1)
    claimed_provenance: Literal["PHYSICAL", "SYNTHETIC"] = "PHYSICAL"


class EvidenceFile(_Strict):
    path: str
    role: Literal["RAW_LOG", "WAVEFORM", "IMAGE", "LAB_REPORT", "MANIFEST", "CERTIFICATE", "OTHER"]
    sha256: str = Field(pattern=_SHA)
    blob_id: str
    uri: str
    size_bytes: int = Field(ge=0)
    mime: str


class N4Measurement(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4Measurement")
    measurement_id: str
    tenant_id: str
    plan_id: str
    evidence_id: str
    test_id: str
    characteristic: str
    unit_serial: Optional[str] = None
    value: Decimal
    unit: str
    expanded_uncertainty: Optional[Decimal] = None
    coverage_factor: float = 2.0
    instrument_id: Optional[str] = None
    instrument_serial: Optional[str] = None
    instrument_firmware: Optional[str] = None
    calibration_id: Optional[str] = None
    calibration_due: Optional[datetime] = Field(default=None, description="ACCREDITED_LAB: as stated by the lab")
    calibration_source: Literal["N4_REGISTER", "ACCREDITED_LAB", "NOT_APPLICABLE"] = "N4_REGISTER"
    captured_at: datetime
    provenance: MeasurementProvenance
    method: VerificationMethod
    method_ref: Optional[MethodRef] = None
    operator: Optional[str] = None
    raw_sha256: str = Field(pattern=_SHA)
    source_command: Optional[str] = None


class AoiDetection(_Strict):
    detection_id: str
    image_sha256: str
    cls: str
    confidence: float
    bbox_xyxy: list[float]
    triage: Literal["MODEL_FLAGGED", "AMBIGUOUS", "BELOW_THRESHOLD"]


class N4EvidenceIntake(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4EvidenceIntake")
    schema_version: Literal["N4EvidenceIntake.v1"] = "N4EvidenceIntake.v1"
    evidence_id: str
    tenant_id: str
    plan_id: str
    adapter: Literal["SCPI_VISA", "SCPI_LIVE", "ACCREDITED_LAB", "AOI_VISION", "CSV_OFFLINE"]
    source_id: str
    submitted_by: str
    submitted_at: datetime
    claimed_provenance: Optional[str] = None
    derived_provenance: Optional[MeasurementProvenance] = None
    files: list[EvidenceFile]
    measurement_ids: list[str] = Field(default_factory=list)
    detections: list[AoiDetection] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    evidence_sha256: Optional[str] = Field(default=None, description="SHA-256 over the sorted file hashes")
    status: Literal["ACCEPTED", "REJECTED"]
    rejection_codes: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)


class AdjudicationDecision(_Strict):
    detection_id: str
    label: Literal["CONFIRMED_DEFECT", "FALSE_CALL"]


class AdjudicationRequest(_Strict):
    evidence_id: str
    decisions: list[AdjudicationDecision] = Field(default_factory=list)
    unit_verdict: Literal["ACCEPT", "REJECT"]
    notes: str = Field(min_length=3)


class N4Adjudication(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4Adjudication")
    adjudication_id: str
    tenant_id: str
    evidence_id: str
    adjudicator: str
    decisions: list[AdjudicationDecision]
    unit_verdict: Literal["ACCEPT", "REJECT"]
    notes: str
    independent: bool
    created_at: datetime


class CaptureStep(_Strict):
    query: str
    characteristic: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    unit: str
    expanded_uncertainty: Decimal = Field(gt=0)
    k: float = Field(default=2.0, gt=0)


class LiveCaptureRequest(_Strict):
    test_id: str
    instrument_id: str
    unit_serial: str
    source_id: str
    station_id: str
    method_ref: Optional[MethodRef] = None
    steps: list[CaptureStep] = Field(min_length=1, max_length=64)


# ----------------------------------------------------------------------------------------------
# N4.5 / N4.6 assessment and review
# ----------------------------------------------------------------------------------------------
class N4Finding(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4Finding")
    finding_id: str
    code: str
    outcome: Literal["BLOCK", "REWORK", "INFO"]
    category: str
    message: str
    test_id: Optional[str] = None
    requirement_id: Optional[str] = None
    evidence_ids: list[str] = Field(default_factory=list)


class TestVerdict(_Strict):
    __test__ = False
    test_id: str
    method: VerificationMethod
    verdict: Verdict
    measurements: list[VerifiedMeasurement] = Field(default_factory=list)
    attribute_inspections: list[AttributeInspection] = Field(default_factory=list)


class N4VerificationAssessment(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4VerificationAssessment")
    schema_version: Literal["N4VerificationAssessment.v1"] = "N4VerificationAssessment.v1"
    assessment_id: str
    tenant_id: str
    plan_id: str
    intake_id: str
    design_revision: str
    pcb_revision: Optional[str] = None
    decision: Decision
    findings: list[N4Finding]
    test_verdicts: list[TestVerdict]
    traceability: N4TraceabilityMatrix
    evidence_ids: list[str]
    evidence_submitters: list[str]
    evidence_sha256: str = Field(pattern=_SHA, description="binds plan, design revision and every evidence file")
    plan_sha256: str
    pfmea_id: Optional[str] = None
    prioritization_method: PrioritizationMethod
    unit_serials: list[str]
    engine_version: str
    assessed_by: str
    assessed_at: datetime
    assessment_sha256: Optional[str] = None
    approval_scope: str = APPROVAL_SCOPE


class N4ReviewRequest(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4ReviewRequest")
    assessment_id: str
    evidence_sha256: str = Field(pattern=_SHA, description="must equal the assessment's evidence hash")
    design_revision: str
    decision: Literal["APPROVED", "REJECTED"]
    reason: str = Field(min_length=5)


class N4Review(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4Review")
    review_id: str
    tenant_id: str
    assessment_id: str
    plan_id: str
    reviewer: str
    reviewer_role: str
    decision: Literal["APPROVED", "REJECTED"]
    reason: str
    evidence_sha256: str
    design_revision: str
    approval_event_id: str
    reviewed_at: datetime
    approval_scope: str = APPROVAL_SCOPE


# ----------------------------------------------------------------------------------------------
# N4.7 release
# ----------------------------------------------------------------------------------------------
class N4ReleaseRequest(_Strict):
    targets: list[Literal["N6", "N7", "N8"]] = Field(min_length=1)


class N4ReleasePackage(_Strict):
    target: Literal["N6", "N7", "N8"]
    artifact_id: str
    revision: str
    status: Literal["RELEASED", "REFUSED"]
    n0_decision: Optional[str] = None
    n0_decision_id: Optional[str] = None
    violated_codes: list[str] = Field(default_factory=list)
    manifest_id: Optional[str] = None
    approval_id: Optional[str] = None
    reason: str


class N4VerificationRelease(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4VerificationRelease")
    schema_version: Literal["N4VerificationRelease.v1"] = "N4VerificationRelease.v1"
    release_id: str
    tenant_id: str
    plan_id: str
    assessment_id: str
    review_id: str
    design_revision: str
    evidence_sha256: str
    payload_release_id: str
    status: Literal["RELEASED", "PARTIAL", "REFUSED"]
    packages: list[N4ReleasePackage]
    released_by: str
    released_at: datetime
    approval_scope: str = APPROVAL_SCOPE


# ----------------------------------------------------------------------------------------------
# AI (opt-in, gated) and reliability (research only)
# ----------------------------------------------------------------------------------------------
class CorpusDocumentRequest(_Strict):
    doc_id: str = Field(pattern=_ID)
    kind: Literal["STANDARD_EXCERPT", "SOP", "VERIFIED_CAPA"]
    title: str
    source_id: str
    text: str = Field(min_length=20, max_length=200_000)
    capa_id: Optional[str] = Field(default=None, description="VERIFIED_CAPA: the verified CAPA it summarises")


class SuggestionRequest(_Strict):
    kind: Literal["PFMEA_FAILURE_MODE", "TEST_CASE", "RCA_HYPOTHESIS"]
    question: str = Field(min_length=5, max_length=2000)
    max_items: int = Field(default=5, ge=1, le=10)


class Citation(_Strict):
    doc_id: str
    chunk_id: str
    quote: str = Field(min_length=8)


class SuggestionItem(_Strict):
    title: str
    text: str
    citations: list[Citation] = Field(min_length=1)


class N4Suggestion(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4Suggestion")
    suggestion_id: str
    tenant_id: str
    plan_id: str
    kind: str
    status: Literal["DRAFT_UNREVIEWED"] = "DRAFT_UNREVIEWED"
    items: list[SuggestionItem]
    rejected_items: list[dict[str, Any]] = Field(default_factory=list)
    provider: str
    model: str
    corpus_doc_ids: list[str]
    created_by: str
    created_at: datetime
    note: str = ("Draft suggestions only: grounded in the cited authorized documents, never rated (S/O/D are "
                 "set by a human), never approved automatically and never used in a release decision.")


class UnitHours(_Strict):
    unit_serial: str
    hours: float = Field(ge=0)
    failed: bool = False


class ReliabilityRequest(_Strict):
    units: list[UnitHours] = Field(min_length=1)
    confidence: float = Field(default=0.6, gt=0, lt=1)


class N4ReliabilityIllustration(_Strict):
    model_config = ConfigDict(extra="forbid", title="N4ReliabilityIllustration")
    illustration_id: str
    tenant_id: str
    plan_id: str
    label: Literal["RESEARCH_ONLY_STATISTICAL_ILLUSTRATION"] = "RESEARCH_ONLY_STATISTICAL_ILLUSTRATION"
    release_eligible: Literal[False] = False
    model: str = "exponential (constant failure rate), chi-square bound"
    total_unit_hours: float
    failures: int
    mtbf_point_h: Optional[float]
    mtbf_lower_bound_h: Optional[float]
    confidence: float
    missing_prerequisites: list[str]
    created_by: str
    created_at: datetime


N4_SCHEMAS: dict[str, type[BaseModel]] = {
    "n4_design_intake": N4DesignIntake, "n4_test_method": N4TestMethod, "n4_instrument": N4Instrument,
    "n4_calibration_record": N4CalibrationRecord, "n4_test_case": TestCaseSpec, "n4_test_plan_request": TestPlanRequest,
    "n4_test_plan": N4TestPlan, "n4_traceability_matrix": N4TraceabilityMatrix, "n4_pfmea_entry": N4PfmeaEntry,
    "n4_pfmea": N4Pfmea, "n4_control_plan": N4ControlPlan, "n4_capa": N4Capa, "n4_measurement": N4Measurement,
    "n4_evidence_intake": N4EvidenceIntake, "n4_lab_report_manifest": LabReportManifest,
    "n4_aoi_inference": AoiInference, "n4_adjudication": N4Adjudication,
    "n4_verification_assessment": N4VerificationAssessment, "n4_review_request": N4ReviewRequest,
    "n4_verification_release": N4VerificationRelease, "n4_reliability_illustration": N4ReliabilityIllustration,
}
N4_EXTRA_SCHEMAS: dict[str, type[BaseModel]] = {
    "n4_scpi_log": ScpiLog, "n4_finding": N4Finding, "n4_accepted_lab": N4AcceptedLab,
    "n4_qualification": N4Qualification, "n4_suggestion": N4Suggestion, "n4_review": N4Review,
}
