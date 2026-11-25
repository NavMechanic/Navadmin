"""N3 contracts → ``schemas/n3_*.schema.json`` (13 published families plus job / vendor / routing records)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..models import (BomItem, ConnectorType, Decision, DesignConstraints, DesignScope,  # noqa: F401
                      Money, Requirement, TestLimit)
from . import APPROVAL_SCOPE

_ID = r"^[A-Za-z0-9][A-Za-z0-9._:\-]{1,127}$"
_SHA = r"^[0-9a-f]{64}$"

PackageKind = Literal["ELECTRICAL_SCHEMATIC", "PCB_LAYOUT", "DFM", "CIRCUIT_SIMULATION", "MECHANICAL", "THERMAL"]
SCOPE_PACKAGES: dict[str, list[str]] = {
    "schematic_pcb": ["ELECTRICAL_SCHEMATIC", "PCB_LAYOUT", "DFM"],
    "circuit_simulation": ["CIRCUIT_SIMULATION"],
    "mechanical_thermal": ["MECHANICAL", "THERMAL"],
}
KIND_SCOPE = {k: s for s, ks in SCOPE_PACKAGES.items() for k in ks}
KIND_REQUIREMENTS: dict[str, dict[str, list[str]]] = {
    "ELECTRICAL_SCHEMATIC": {"native": ["SCHEMATIC"], "evidence": ["ERC_REPORT", "BOM"]},
    "PCB_LAYOUT": {"native": ["PCB", "SCHEMATIC"], "evidence": ["DRC_REPORT", "BOM"]},
    "DFM": {"native": ["PCB"], "evidence": ["GERBER", "DRILL"]},
    "CIRCUIT_SIMULATION": {"native": ["NETLIST"], "evidence": ["SIM_LOG"]},
    "MECHANICAL": {"native": ["GEOMETRY"], "evidence": []},
    "THERMAL": {"native": ["GEOMETRY"], "evidence": ["THERMAL_REPORT"]},
}
TOOL_KINDS = {"kicad-cli": {"ELECTRICAL_SCHEMATIC", "PCB_LAYOUT", "DFM"}, "ngspice": {"CIRCUIT_SIMULATION"}}
FileRole = Literal["SCHEMATIC", "PCB", "PROJECT", "GEOMETRY", "NETLIST", "GERBER", "DRILL", "ODB", "BOM",
                   "ERC_REPORT", "DRC_REPORT", "SIM_LOG", "SIM_RAW", "THERMAL_REPORT", "DOCUMENT", "IMAGE", "OTHER"]
Outcome = Literal["BLOCK", "REWORK", "INFO"]
N3Decision = Literal["PASS", "REWORK", "BLOCK"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ----------------------------------------------------------------------------------------------
# N3.1 baseline
# ----------------------------------------------------------------------------------------------
class BaselineRequest(_Strict):
    prd_id: str = Field(pattern=_ID)
    prd_revision: Optional[str] = Field(default=None, description="default: the latest PRDRelease")
    project_id: str = Field(pattern=_ID)
    design_source_id: str = Field(pattern=_ID, description="N0 source that design files and N3 releases are "
                                                           "registered under (must permit the N4/N5 purposes)")


class N3DesignBaseline(_Strict):
    model_config = ConfigDict(extra="forbid", title="N3DesignBaseline")
    schema_version: Literal["N3DesignBaseline.v1"] = "N3DesignBaseline.v1"
    baseline_id: str
    tenant_id: str
    prd_id: str
    prd_artifact: str = Field(description="PRD ARTIFACT_ID@REVISION")
    prd_revision: str
    prd_payload_sha256: str = Field(pattern=_SHA)
    prd_release_id: str
    prd_manifest_id: str
    prd_approval_id: str
    prd_approval_hash: str = Field(pattern=_SHA)
    project_id: str
    design_source_id: str
    product_name: str
    design_scopes: list[DesignScope]
    constraints: DesignConstraints
    requirements: list[Requirement]
    test_limits: list[TestLimit]
    synthetic: bool
    locked_by: str
    locked_at: datetime
    baseline_sha256: Optional[str] = None
    approval_scope: str = APPROVAL_SCOPE


# ----------------------------------------------------------------------------------------------
# N3.2 planning
# ----------------------------------------------------------------------------------------------
class N3WorkPackageRequest(_Strict):
    model_config = ConfigDict(extra="forbid", title="N3WorkPackageRequest")
    baseline_id: str
    kind: PackageKind
    title: Optional[str] = None
    budget_cap: Money
    due_date: Optional[date] = None
    acceptance_notes: Optional[str] = Field(default=None, max_length=4000)


class PlanRequest(_Strict):
    scopes: Optional[list[DesignScope]] = None
    budget_per_package: Money


class N3WorkPackage(_Strict):
    model_config = ConfigDict(extra="forbid", title="N3WorkPackage")
    schema_version: Literal["N3WorkPackage.v1"] = "N3WorkPackage.v1"
    work_package_id: str
    tenant_id: str
    baseline_id: str
    prd_artifact: str
    scope: DesignScope
    kind: PackageKind
    title: str
    required_native_roles: list[str]
    required_evidence_roles: list[str]
    acceptance_criteria: list[str]
    budget_cap: Money
    due_date: Optional[date] = None
    created_by: str
    created_at: datetime
    package_sha256: Optional[str] = Field(default=None, description="immutable content hash of this package")


# ----------------------------------------------------------------------------------------------
# N3.3 routing
# ----------------------------------------------------------------------------------------------
class N3VendorProfile(_Strict):
    model_config = ConfigDict(extra="forbid", title="N3VendorProfile")
    vendor_id: str = Field(pattern=_ID)
    tenant_id: str
    name: str
    capabilities: list[PackageKind] = Field(min_length=1)
    ip_terms: Literal["ASSIGNED_TO_CUSTOMER", "EXCLUSIVE_LICENSE"]
    nda_on_file: bool
    max_budget: Money
    onboarded_by: Optional[str] = None
    onboarded_at: Optional[datetime] = None


class RouteRequest(_Strict):
    executor: Literal["TOOL", "VENDOR"]
    tool: Optional[Literal["kicad-cli", "ngspice"]] = None
    vendor_id: Optional[str] = None
    budget: Money
    connector_type: ConnectorType = "FILE_PORTAL"


class N3Routing(_Strict):
    model_config = ConfigDict(extra="forbid", title="N3Routing")
    route_id: str
    tenant_id: str
    work_package_id: str
    executor: Literal["TOOL", "VENDOR"]
    tool: Optional[str] = None
    vendor_id: Optional[str] = None
    budget: Money
    vendor_work_package_id: Optional[str] = Field(default=None, description="N0 VendorWorkPackage.v1 id")
    authorized: bool
    reasons: list[str]
    routed_by: str
    routed_at: datetime


# ----------------------------------------------------------------------------------------------
# N3.4 execution: files, deliveries, tool jobs
# ----------------------------------------------------------------------------------------------
class N3FileManifestEntry(_Strict):
    path: str = Field(min_length=1, max_length=255)
    role: FileRole
    sha256: str = Field(pattern=_SHA)
    tool: Optional[str] = None
    tool_version: Optional[str] = None


class N3FileManifest(_Strict):
    model_config = ConfigDict(extra="forbid", title="N3FileManifest")
    design_revision: str = Field(min_length=1, max_length=16)
    entries: list[N3FileManifestEntry] = Field(min_length=1)
    replaces_previous: bool = Field(default=True, description="true: this delivery is the complete file set and "
                                                              "replaces earlier deliveries; false: files are merged "
                                                              "by path")
    notes: Optional[str] = Field(default=None, max_length=4000)


class N3FileEvidence(_Strict):
    model_config = ConfigDict(extra="forbid", title="N3FileEvidence")
    path: str
    role: FileRole
    sha256: str = Field(pattern=_SHA)
    blob_id: str
    uri: str
    size_bytes: int = Field(ge=0)
    native: bool
    detected_format: str
    tool: Optional[str] = None
    tool_version: Optional[str] = None
    revision: Optional[str] = None
    origin: Literal["VENDOR", "TOOL_JOB", "ENGINEER"]
    delivery_id: Optional[str] = None
    job_id: Optional[str] = None


class N3VendorDelivery(_Strict):
    model_config = ConfigDict(extra="forbid", title="N3VendorDelivery")
    schema_version: Literal["N3VendorDelivery.v1"] = "N3VendorDelivery.v1"
    delivery_id: str
    tenant_id: str
    work_package_id: str
    vendor_id: Optional[str] = None
    submitted_by: str
    submitted_at: datetime
    manifest: N3FileManifest
    files: list[N3FileEvidence]
    status: Literal["ACCEPTED", "REJECTED"]
    rejection_codes: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    n0_vendor_result_id: Optional[str] = Field(default=None, description="N0 VendorResult.v1 decision id for "
                                                                         "deliveries against a vendor route")


class Corner(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    params: dict[str, float] = Field(default_factory=dict)
    temp_c: float = 27.0


ToolOperation = Literal["sch_erc", "pcb_drc", "pcb_export_gerbers", "pcb_export_drill", "pcb_export_odb",
                        "pcb_export_step", "sch_export_bom", "spice_corners"]


class ToolJobRequest(_Strict):
    tool: Literal["kicad-cli", "ngspice"]
    operation: ToolOperation
    input_sha256: list[str] = Field(min_length=1, description="files already delivered to this work package")
    corners: Optional[list[Corner]] = Field(default=None, description="spice_corners: default = PRD worst cases")


class N3ToolJob(_Strict):
    model_config = ConfigDict(extra="forbid", title="N3ToolJob")
    job_id: str
    tenant_id: str
    work_package_id: str
    tool: str
    operation: str
    input_sha256: list[str]
    corners: list[Corner] = Field(default_factory=list)
    status: Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "TIMEOUT"]
    tool_version: Optional[str] = None
    argv: list[list[str]] = Field(default_factory=list)
    exit_codes: list[Optional[int]] = Field(default_factory=list)
    outputs: list[N3FileEvidence] = Field(default_factory=list)
    output_exit_codes: dict[str, Optional[int]] = Field(default_factory=dict, description="output path → exit code "
                                                                                           "of the step that wrote it")
    limits: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    submitted_by: str
    submitted_at: datetime
    finished_at: Optional[datetime] = None


# ----------------------------------------------------------------------------------------------
# N3.5 / N3.6 verification and decision
# ----------------------------------------------------------------------------------------------
class N3Finding(_Strict):
    model_config = ConfigDict(extra="forbid", title="N3Finding")
    finding_id: str
    code: str
    severity: Literal["CRITICAL", "MAJOR", "MINOR", "INFO"]
    outcome: Outcome
    category: str
    message: str
    evidence: list[str] = Field(default_factory=list, description="file paths / SHA-256s the finding rests on")
    refdes: Optional[str] = None
    corner: Optional[str] = None


class N3Assessment(_Strict):
    model_config = ConfigDict(extra="forbid", title="N3Assessment")
    schema_version: Literal["N3Assessment.v1"] = "N3Assessment.v1"
    assessment_id: str
    tenant_id: str
    work_package_id: str
    baseline_id: str
    scope: DesignScope
    kind: PackageKind
    design_revision: Optional[str]
    files: list[N3FileEvidence]
    findings: list[N3Finding]
    decision: N3Decision
    metrics: dict[str, Any]
    engine_version: str
    assessed_by: str
    assessed_at: datetime
    assessment_sha256: Optional[str] = None
    approval_scope: str = APPROVAL_SCOPE


class ActionCandidate(_Strict):
    action_id: str = Field(pattern=_ID)
    action_type: Literal["FIX_IN_HOUSE", "VENDOR_REWORK", "RERUN_SIMULATION", "ADD_CORNER", "RELAYOUT",
                         "COMPONENT_SWAP", "ADD_THERMAL_MITIGATION", "WAIVER_REQUEST"]
    addresses: list[str] = Field(min_length=1, description="finding codes this action is meant to resolve")
    cost: Decimal = Field(ge=0)
    delay_days: int = Field(default=0, ge=0)
    delay_cost_per_day: Decimal = Field(default=Decimal(0), ge=0)
    risk_reduction_value: Decimal = Field(ge=0, description="user-supplied value of resolving the findings")
    description: Optional[str] = None


class N3ActionRequest(_Strict):
    model_config = ConfigDict(extra="forbid", title="N3ActionRequest")
    assessment_id: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    budget_cap: Optional[Decimal] = Field(default=None, ge=0)
    candidates: list[ActionCandidate] = Field(min_length=1)


class RankedProposal(_Strict):
    rank: int
    action_id: str
    action_type: str
    addresses: list[str]
    total_cost: Decimal
    net_value: Decimal
    recommended: bool
    status: Literal["PROPOSED_NOT_EXECUTED"] = "PROPOSED_NOT_EXECUTED"
    rationale: str


class N3Action(_Strict):
    model_config = ConfigDict(extra="forbid", title="N3Action")
    schema_version: Literal["N3Action.v1"] = "N3Action.v1"
    proposal_id: str
    tenant_id: str
    assessment_id: str
    assessment_decision: N3Decision
    decision_unchanged: Literal[True] = True
    ranked: list[RankedProposal]
    blocked: list[dict[str, Any]]
    note: str = ("Rework proposals are recommendations only. They never change the assessment decision; hard "
                 "safety and engineering findings stay BLOCK / REWORK until a new delivery passes.")
    created_by: str
    created_at: datetime


class N3ReviewRequest(_Strict):
    model_config = ConfigDict(extra="forbid", title="N3ReviewRequest")
    assessment_id: str
    decision: Literal["APPROVED", "REJECTED"]
    reason: str = Field(min_length=5)


class N3Review(_Strict):
    model_config = ConfigDict(extra="forbid", title="N3Review")
    review_id: str
    tenant_id: str
    assessment_id: str
    work_package_id: str
    reviewer: str
    decision: Literal["APPROVED", "REJECTED"]
    reason: str
    approval_event_id: str
    reviewed_at: datetime
    approval_scope: str = APPROVAL_SCOPE


# ----------------------------------------------------------------------------------------------
# N3.7 release
# ----------------------------------------------------------------------------------------------
class ReleaseRequest(_Strict):
    scope: DesignScope
    targets: list[Literal["N4", "N5"]] = Field(min_length=1)


class N3ReleasePackage(_Strict):
    target: Literal["N4", "N5"]
    artifact_type: str
    artifact_id: str
    revision: str
    status: Literal["RELEASED", "REFUSED", "NOT_APPLICABLE"]
    n0_decision: Optional[Decision] = None
    n0_decision_id: Optional[str] = None
    violated_codes: list[str] = Field(default_factory=list)
    manifest_id: Optional[str] = None
    approval_id: Optional[str] = None
    reason: str


class N3Release(_Strict):
    model_config = ConfigDict(extra="forbid", title="N3Release")
    schema_version: Literal["N3Release.v1"] = "N3Release.v1"
    release_id: str
    tenant_id: str
    baseline_id: str
    scope: DesignScope
    prd_artifact: str
    design_revision: str
    payload_release_id: str = Field(description="release_id inside the N0 DESIGN_RELEASE / BOM_RELEASE payloads")
    status: Literal["RELEASED", "PARTIAL", "REFUSED"]
    work_package_ids: list[str]
    assessment_ids: list[str]
    review_ids: list[str]
    packages: list[N3ReleasePackage]
    released_by: str
    released_at: datetime
    approval_scope: str = APPROVAL_SCOPE


class FeedbackRequest(_Strict):
    target_kind: Literal["ASSESSMENT", "FINDING", "RELEASE", "JOB"]
    target_id: str
    label: Literal["CORRECT", "FALSE_POSITIVE", "MISSED_ISSUE", "TOOL_ERROR", "OTHER"]
    reason: str = Field(min_length=3)


class N3Feedback(_Strict):
    model_config = ConfigDict(extra="forbid", title="N3Feedback")
    feedback_id: str
    tenant_id: str
    target_kind: str
    target_id: str
    label: str
    reason: str
    created_by: str
    created_at: datetime
    training_eligible: bool


class StatusView(_Strict):
    subject: str
    status: str
    history: list[dict[str, Any]]


N3_SCHEMAS: dict[str, type[BaseModel]] = {
    "n3_design_baseline": N3DesignBaseline, "n3_work_package": N3WorkPackage,
    "n3_work_package_request": N3WorkPackageRequest, "n3_vendor_delivery": N3VendorDelivery,
    "n3_file_manifest": N3FileManifest, "n3_file_evidence": N3FileEvidence, "n3_finding": N3Finding,
    "n3_assessment": N3Assessment, "n3_action": N3Action, "n3_action_request": N3ActionRequest,
    "n3_review_request": N3ReviewRequest, "n3_release": N3Release, "n3_feedback": N3Feedback,
}
N3_EXTRA_SCHEMAS: dict[str, type[BaseModel]] = {"n3_tool_job": N3ToolJob, "n3_vendor_profile": N3VendorProfile,
                                                "n3_routing": N3Routing, "bom_item.v1": BomItem}
