"""Pydantic v2 contracts.

``GateEvaluationResponse`` is the published shape from the specification. Its
four required keys — ``gate``, ``status``, ``evaluation_timestamp``,
``blocking_reasons`` — plus ``passed_artifacts`` are frozen. Everything added
beyond them is optional, so a consumer written against the specified schema
keeps working.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (BaseModel, ConfigDict, Field, field_serializer, field_validator,
                      model_validator)

from .enums import (ActionPriority, ArtifactStatus, ArtifactType, CapaSource,
                    CapaState, CertificateStatus,
                    CertificationScheme, ChangeType, ECNCategory, ECNState,
                    EffectivityType, EvaluationStatus, FmeaType, GateCode, GateState,
                    ImpactObjectType, ImpactState, LifecycleState, PartType, ReratingState,
                    ReviewState, Role, RuleSeverity, RuleStatus, SupplyMode)

PartNumber = Annotated[str, Field(min_length=2, max_length=64)]
NonNegDecimal = Annotated[Decimal, Field(ge=0)]


def _z(dt: datetime | None) -> str | None:
    """Serialise as RFC 3339 with a trailing Z, matching the specified schema."""
    if dt is None:
        return None
    return dt.isoformat().replace("+00:00", "Z")


# =====================================================================
# Auth
# =====================================================================
class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    # Development sign-in. In production the token comes from the corporate IdP
    # and this endpoint is not mounted — see README, "Authentication".
    password: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    subject: str
    roles: list[Role]

    @field_serializer("expires_at")
    def _ser(self, v: datetime) -> str | None:
        return _z(v)


class Principal(BaseModel):
    """The authenticated caller, as the routers see it."""
    model_config = ConfigDict(extra="forbid")
    username: str
    full_name: str | None = None
    roles: list[Role] = Field(default_factory=list)

    def has(self, *roles: Role) -> bool:
        return Role.ADMIN in self.roles or any(r in self.roles for r in roles)


# =====================================================================
# Parts, revisions, BOM
# =====================================================================
class PartCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part_number: PartNumber
    name: str
    description: str | None = None
    part_type: PartType = PartType.COMPONENT
    supply_mode: SupplyMode | None = None
    supplier: str | None = None
    sourcing_status: str | None = None
    uom: str = "EA"
    is_sellable: bool = False
    lifecycle_risk: str | None = None
    safety_critical: bool = False


class PartOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    part_number: str
    name: str
    description: str | None = None
    part_type: str
    supply_mode: str | None = None
    supplier: str | None = None
    sourcing_status: str | None = None
    uom: str
    is_sellable: bool
    lifecycle_risk: str | None = None
    safety_critical: bool


class RevisionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rev: str = Field(min_length=1, max_length=8)
    change_summary: str | None = None
    unit_cost_usd: NonNegDecimal | None = None
    source_system: str | None = None
    source_ref: str | None = None


class RevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    part_id: int
    rev: str
    lifecycle_state: str
    unit_cost_usd: Decimal | None = None
    change_summary: str | None = None
    released_at: datetime | None = None
    released_by: str | None = None
    source_system: str | None = None
    source_ref: str | None = None

    @field_serializer("released_at")
    def _ser(self, v: datetime | None) -> str | None:
        return _z(v)


class BomLineCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    child_part_number: PartNumber
    quantity: Decimal = Field(default=Decimal("1"), gt=0)
    find_number: int | None = Field(default=None, ge=1)
    reference_designator: str | None = None
    # Omit to float to the child's current released revision. Supply it to pin,
    # which is what you want for anything safety-critical.
    child_revision: str | None = None
    notes: str | None = None


class BomLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    find_number: int
    child_part_number: str
    child_name: str
    quantity: Decimal
    child_revision: str | None = None
    revision_pinned: bool
    reference_designator: str | None = None
    notes: str | None = None


class BomNode(BaseModel):
    """One node of an exploded BOM tree."""
    part_number: str
    name: str
    part_type: str
    revision: str | None = None
    lifecycle_state: str | None = None
    level: int
    find_number: int | None = None
    quantity_per_parent: Decimal
    extended_quantity: Decimal
    unit_cost_usd: Decimal | None = None
    extended_cost_usd: Decimal | None = None
    supply_mode: str | None = None
    revision_pinned: bool = False
    path: str
    children: list["BomNode"] = Field(default_factory=list)


class BomExplosion(BaseModel):
    sku: str
    revision: str
    lifecycle_state: str
    generated_at: datetime
    max_level: int
    line_count: int
    distinct_part_count: int
    rolled_up_cost_usd: Decimal
    tree: list[BomNode]
    warnings: list[str] = Field(default_factory=list)

    @field_serializer("generated_at")
    def _ser(self, v: datetime) -> str | None:
        return _z(v)


class WhereUsedEntry(BaseModel):
    part_number: str
    name: str
    level: int
    path: str
    sku: str | None = None
    quantity_per_parent: Decimal


# =====================================================================
# Artifacts and certificates
# =====================================================================
class ArtifactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    artifact_type: ArtifactType
    revision: str = "1"
    status: ArtifactStatus = ArtifactStatus.DRAFT
    part_number: str | None = None
    sku: str | None = None
    gate_code: GateCode | None = None
    uri: str | None = None
    checksum_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    file_format: str | None = None
    source_system: str | None = None
    is_parametric: bool = False
    attributes: dict[str, Any] = Field(default_factory=dict)


class ArtifactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    artifact_type: str
    revision: str
    status: str
    sku: str | None = None
    part_id: int | None = None
    file_format: str | None = None
    source_system: str | None = None
    is_parametric: bool
    released_at: datetime | None = None
    checksum_sha256: str | None = None

    @field_serializer("released_at")
    def _ser(self, v: datetime | None) -> str | None:
        return _z(v)


class CertificateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    scheme: str
    sku: str
    certificate_number: str | None = None
    status: str
    issued_on: date | None = None
    expires_on: date | None = None
    lab: str | None = None
    scope: str | None = None
    last_verified_at: datetime | None = None
    verification_source: str | None = None

    @field_serializer("last_verified_at")
    def _ser(self, v: datetime | None) -> str | None:
        return _z(v)


class ComplianceRefreshResponse(BaseModel):
    sku: str
    mode: str
    checked: int
    updated: int
    certificates: list[CertificateOut]
    warnings: list[str] = Field(default_factory=list)


# =====================================================================
# FMEA / control plan
# =====================================================================
class FmeaRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    fmea_type: str
    row_ref: str
    sku: str
    item_function: str
    failure_mode: str
    severity: int
    occurrence: int
    detection: int
    action_priority: str
    recommended_action: str | None = None
    responsibility_function: str | None = None
    owner_username: str | None = None
    target_gate: str | None = None
    status: str
    review_state: str


class FmeaAssignOwner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_username: str
    target_date: date | None = None


class ControlPlanItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    control_ref: str
    sku: str
    station: str | None = None
    characteristic: str
    specification: str | None = None
    classification: str | None = None
    linked_fmea_ref: str | None = None
    review_state: str


# =====================================================================
# Gates — the published contract
# =====================================================================
class BlockingReason(BaseModel):
    """One failed rule, in the shape the specification names."""
    model_config = ConfigDict(extra="forbid")
    rule_id: str
    status: RuleStatus
    detail: str
    # additive: everything below is optional for a consumer of the base contract
    severity: RuleSeverity = RuleSeverity.BLOCKING
    remediation: str | None = None
    object_refs: list[str] = Field(default_factory=list)


class GateEvaluationResponse(BaseModel):
    """The published output. Returned with HTTP 200 when PASSED, 422 when BLOCKED."""
    model_config = ConfigDict(extra="forbid")

    gate: str
    status: EvaluationStatus
    evaluation_timestamp: datetime
    blocking_reasons: list[BlockingReason] = Field(default_factory=list)
    passed_artifacts: list[str] = Field(default_factory=list)

    # ---- additive fields, none required by the base contract ----
    sku: str | None = None
    evaluation_id: int | None = None
    rules_evaluated: int = 0
    rules_passed: int = 0
    rules_failed: int = 0
    rules_not_applicable: int = 0
    advisories: list[BlockingReason] = Field(default_factory=list)
    gate_state: GateState | None = None
    previous_gate_state: GateState | None = None
    transition_applied: bool = False
    evaluated_by: str | None = None

    @field_serializer("evaluation_timestamp")
    def _ser(self, v: datetime) -> str:
        return _z(v) or ""


class GateEvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: PartNumber
    # False runs the rules and reports without touching the gate's state. The
    # default is True because the specification says a failing evaluation must
    # block the transition, which implies the evaluation drives the state.
    apply_transition: bool = True
    include_advisories: bool = True


class GateWaiverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: PartNumber
    reason: str = Field(min_length=20)
    rule_ids: list[str] = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def _substantive(cls, v: str) -> str:
        if len(v.split()) < 5:
            raise ValueError("A waiver reason must be a sentence, not a word. "
                             "This text is the audit record for overriding a gate.")
        return v


class GateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    sku: str
    gate_code: str
    state: str
    planned_date: date | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    closed_by: str | None = None
    waiver_reason: str | None = None

    @field_serializer("opened_at", "closed_at")
    def _ser(self, v: datetime | None) -> str | None:
        return _z(v)


class GateRuleInfo(BaseModel):
    rule_id: str
    gate: str
    title: str
    severity: RuleSeverity
    applies_to: str
    standard_ref: str | None = None


# =====================================================================
# ECN / ECO
# =====================================================================
class ECNAffectedItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part_number: PartNumber
    change_type: ChangeType = ChangeType.SPEC_CHANGE
    replacement_part_number: str | None = None
    to_revision: str | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def _substitute_needs_replacement(self) -> "ECNAffectedItemIn":
        if self.change_type is ChangeType.SUBSTITUTE and not self.replacement_part_number:
            raise ValueError(
                "change_type SUBSTITUTE requires replacement_part_number — a substitution "
                "with no replacement cannot be costed, qualified or ordered."
            )
        return self


class ECNCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=5, max_length=200)
    reason: str = Field(min_length=10)
    category: ECNCategory = ECNCategory.QUALITY
    primary_sku: str | None = None
    affected_items: list[ECNAffectedItemIn] = Field(min_length=1)
    effectivity_type: EffectivityType = EffectivityType.IMMEDIATE
    effectivity_date: date | None = None
    effectivity_serial: str | None = None
    stock_disposition: str | None = None
    target_gate: GateCode | None = None
    # Run the where-used impact analysis on creation. Off only if you intend to
    # call /impact-analysis explicitly.
    analyse_impact: bool = True

    @model_validator(mode="after")
    def _effectivity_consistent(self) -> "ECNCreate":
        if self.effectivity_type is EffectivityType.DATE and not self.effectivity_date:
            raise ValueError("effectivity_type DATE requires effectivity_date.")
        if self.effectivity_type is EffectivityType.SERIAL and not self.effectivity_serial:
            raise ValueError("effectivity_type SERIAL requires effectivity_serial.")
        return self


class ECNImpactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    object_type: str
    object_ref: str
    object_title: str | None = None
    sku: str | None = None
    state: str
    reason: str | None = None
    where_used_path: str | None = None
    reviewed_by: str | None = None


class ECNOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    ecn_number: str
    title: str
    reason: str
    category: str
    state: str
    primary_sku: str | None = None
    raised_by: str
    raised_at: datetime
    approved_by: str | None = None
    effectivity_type: str
    effectivity_date: date | None = None
    target_gate: str | None = None
    impact_analysed_at: datetime | None = None
    affected_part_numbers: list[str] = Field(default_factory=list)
    impacts: list[ECNImpactOut] = Field(default_factory=list)
    impact_summary: dict[str, int] = Field(default_factory=dict)

    @field_serializer("raised_at", "impact_analysed_at")
    def _ser(self, v: datetime | None) -> str | None:
        return _z(v)


class ECNDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern="^(approve|reject)$")
    note: str | None = None


class ImpactReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: ImpactState = ImpactState.RE_REVIEWED
    note: str | None = None


# =====================================================================
# Vault webhooks
# =====================================================================
class VaultRevisionPayload(BaseModel):
    """One revision event from the CAD/EDA vault."""
    model_config = ConfigDict(extra="forbid")

    event_id: str
    event_type: str = Field(pattern="^(revision.released|revision.created|bom.exported)$")
    occurred_at: datetime
    part_number: PartNumber
    revision: str = Field(min_length=1, max_length=8)
    # STEP AP242 for the parametric master, RS-274X for Gerbers, CSV for BOM
    file_format: str
    file_name: str
    checksum_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    artifact_type: ArtifactType
    is_parametric: bool = False
    uri: str | None = None
    author: str | None = None
    change_summary: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _format_matches_type(self) -> "VaultRevisionPayload":
        """A STEP file is not a Gerber. Catching this at the edge stops a
        mislabelled artifact from satisfying a gate rule it should not."""
        expected = {
            ArtifactType.CAD_MASTER_MODEL: ("STEP", "AP242", "JT", "CATPART", "SLDPRT", "PRT"),
            ArtifactType.GERBER: ("RS-274X", "GERBER", "ODB++", "IPC-2581"),
            ArtifactType.BOM_EXPORT: ("CSV", "XLSX", "XML", "JSON"),
        }.get(self.artifact_type)
        if expected and not any(tok in self.file_format.upper() for tok in expected):
            raise ValueError(
                f"artifact_type {self.artifact_type.value} expects a file_format among "
                f"{', '.join(expected)}; got {self.file_format!r}."
            )
        if self.artifact_type is ArtifactType.CAD_MASTER_MODEL and not self.is_parametric:
            # A dumb solid export is a deliverable, but it is not a master model.
            raise ValueError(
                "A CAD_MASTER_MODEL must set is_parametric=true. A non-parametric STEP "
                "export is a derived file — publish it as CAD_DRAWING instead, or the "
                "G2 master-model rule will be satisfied by something that cannot be edited."
            )
        return self


class VaultIngestResponse(BaseModel):
    accepted: bool
    part_number: str
    revision: str
    revision_id: int | None = None
    artifact_id: int | None = None
    created_revision: bool = False
    created_artifact: bool = False
    duplicate: bool = False
    triggered_ecn: str | None = None
    message: str


# =====================================================================
# CAPA
# =====================================================================
class CapaCreate(BaseModel):
    """Open a CAPA from field evidence.

    ``source_ref`` is the idempotency key. Node 6 retries, and a retried
    clustering run must not open a second CAPA for the same cluster — a
    duplicate CAPA is how the same field failure gets investigated twice by two
    people who do not know about each other.
    """
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=5, max_length=200)
    description: str = Field(min_length=10)
    source: CapaSource = CapaSource.INTERNAL
    source_ref: str | None = Field(default=None, max_length=64)
    sku: str | None = None
    component_lot: str | None = Field(default=None, max_length=64)
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "MEDIUM"
    assigned_to: str | None = None
    due_date: date | None = None

    affected_volume_pct: float | None = Field(default=None, ge=0.0, le=1.0)
    affected_unit_count: int | None = Field(default=None, ge=0)
    population_count: int | None = Field(default=None, ge=0)
    evidence: dict[str, Any] = Field(default_factory=dict)

    # The proposal. Never applied by this endpoint whatever the caller's role.
    linked_fmea_row_ref: str | None = Field(default=None, max_length=16)
    proposed_occurrence: int | None = Field(default=None, ge=1, le=10)
    occurrence_rationale: str | None = None

    @model_validator(mode="after")
    def _rerating_needs_a_reason(self) -> "CapaCreate":
        if self.proposed_occurrence is not None:
            if not self.linked_fmea_row_ref:
                raise ValueError(
                    "proposed_occurrence requires linked_fmea_row_ref — an "
                    "occurrence rating belongs to a specific FMEA row.")
            if not self.occurrence_rationale:
                raise ValueError(
                    "proposed_occurrence requires occurrence_rationale. An "
                    "occurrence rating changed without a recorded reason is a "
                    "number nobody can defend at an audit.")
        if (self.affected_unit_count is not None
                and self.population_count is not None
                and self.affected_unit_count > self.population_count):
            raise ValueError(
                f"affected_unit_count ({self.affected_unit_count}) exceeds "
                f"population_count ({self.population_count}).")
        return self


class CapaDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: CapaState
    note: str | None = None


class ReratingDecision(BaseModel):
    """Apply or reject a proposed FMEA occurrence re-rating."""
    model_config = ConfigDict(extra="forbid")
    apply: bool
    reason: str = Field(min_length=10)


class CapaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    capa_number: str
    title: str
    description: str
    state: CapaState
    source: CapaSource
    source_ref: str | None
    sku: str | None
    component_lot: str | None
    severity: str
    raised_by: str
    raised_at: datetime
    assigned_to: str | None
    due_date: date | None
    closed_by: str | None = None
    closed_at: datetime | None = None
    closure_note: str | None = None

    affected_volume_pct: float | None
    affected_unit_count: int | None
    population_count: int | None
    evidence: dict[str, Any]

    linked_fmea_row_id: int | None
    proposed_occurrence: int | None
    occurrence_rationale: str | None
    rerating_state: ReratingState
    rerating_applied_by: str | None = None
    rerating_applied_at: datetime | None = None
    occurrence_before: int | None = None
    action_priority_before: str | None = None
    action_priority_after: str | None = None

    # ---- additive ----
    linked_fmea_row_ref: str | None = None
    current_occurrence: int | None = None
    current_action_priority: str | None = None
    notes: list[str] = Field(default_factory=list)


# =====================================================================
# Meta
# =====================================================================
class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    uptime_seconds: float
    database: dict[str, Any]
    capabilities: dict[str, Any]
    counts: dict[str, int]


class ErrorBody(BaseModel):
    error: str
    detail: str
    hint: str | None = None
    request_id: str | None = None


BomNode.model_rebuild()
