"""N1 contracts (Pydantic v2 → JSON Schema 2020-12, exported as ``schemas/n1_*.schema.json``).

Market evidence (``MarketObservation``) and the N0 payloads for market artifacts live in
``app.models`` so N0 validates exactly what N1 later reads. This module adds N1's own records:
batches as ingested, buyer accounts, features, opportunity assessments, reports, attestations,
the action optimizer, feedback, connectors, SLM extraction and the N2/N6/N7 handoff.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..models import (BuyerRecord, BuyerRecordPayload, Decision, GatewayDecision, MarketAmount,  # noqa: F401
                      MarketBatchPayload, MarketObservation, ModelRun, ObservationType, RelationshipStage,
                      RuleViolation, SourceConnector, SpendType, TenderDeadline)

_ID = r"^[A-Za-z0-9][A-Za-z0-9._:\-]{1,127}$"
_SHA = r"^[0-9a-f]{64}$"
_CCY = r"^[A-Z]{3}$"

SCORE_DISCLAIMER = ("heuristic_priority_score is a transparent 0–100 prioritization heuristic built from buyer "
                    "access, requirements fit, procurement evidence strength, timing and missing-information "
                    "penalties. It is NOT a calibrated probability of winning; no win-probability model is used "
                    "until enough real labeled won/lost outcomes exist.")
SAM_DISCLAIMER = ("verified_real_sam counts only non-synthetic, VERIFIED_BUDGET, reviewer-attested, fresh, "
                  "in-category, de-duplicated evidence. Synthetic records, TAM proxies and unattested estimates are "
                  "reported separately and never counted as verified real SAM.")
OPTIMIZER_NOTE = ("N1.5 ranks permissible actions for a human to decide on. It never sends outreach, submits bids "
                  "or commits spend.")
CountsAs = Literal["VERIFIED", "INDICATIVE", "HYPOTHETICAL", "EXCLUDED"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ----------------------------------------------------------------------------------------------
# intake
# ----------------------------------------------------------------------------------------------
class BatchSubmission(_Strict):
    """Submit raw market evidence: N1 wraps it as an N0 MARKET_BATCH artifact, N0 decides, and only
    a PASS is ingested."""
    tenant_id: str = Field(pattern=_ID)
    project_id: str = Field(pattern=_ID)
    source_id: str = Field(pattern=_ID)
    batch: MarketBatchPayload
    synthetic: bool = False
    artifact_id: Optional[str] = Field(default=None, pattern=_ID)
    revision: str = "A"


class BuyerSubmission(_Strict):
    tenant_id: str = Field(pattern=_ID)
    project_id: str = Field(pattern=_ID)
    source_id: str = Field(pattern=_ID)
    records: BuyerRecordPayload
    synthetic: bool = False
    artifact_id: Optional[str] = Field(default=None, pattern=_ID)
    revision: str = "A"


class IntakeRequest(_Strict):
    """Ingest an artifact that already passed N0 (e.g. submitted through /v1/gateway/evaluate)."""
    artifact_id: str
    revision: str


class IntakeResult(_Strict):
    kind: Literal["MARKET_BATCH", "BUYER_RECORD"]
    batch_id: str
    n0_artifact_id: str
    n0_revision: str
    n0_decision_id: str
    payload_sha256: str
    synthetic: bool
    observations_ingested: int
    new_evidence_revisions: list[str]
    unchanged_evidence: list[str]
    accounts: list[str]
    already_ingested: bool = False


class SubmissionResult(_Strict):
    n0_decision: GatewayDecision
    intake: Optional[IntakeResult] = None


class MarketBatch(_Strict):
    """A batch as N1 ingested it, pinned to the N0 artifact revision and decision that admitted it."""
    model_config = ConfigDict(extra="forbid", title="MarketBatch.v1")
    schema_version: Literal["MarketBatch.v1"] = "MarketBatch.v1"
    tenant_id: str
    kind: Literal["MARKET_BATCH", "BUYER_RECORD"]
    batch_id: str
    connector: str
    source_id: str
    retrieved_at: datetime
    synthetic: bool
    n0_artifact_id: str
    n0_revision: str
    n0_decision_id: str
    payload_sha256: str = Field(pattern=_SHA)
    raw_payload_sha256: Optional[str] = None
    evidence_ids: list[str]
    ingested_by: str
    ingested_at: datetime


# ----------------------------------------------------------------------------------------------
# accounts & features (N1.2)
# ----------------------------------------------------------------------------------------------
class BuyerAccount(_Strict):
    model_config = ConfigDict(extra="forbid", title="BuyerAccount.v1")
    schema_version: Literal["BuyerAccount.v1"] = "BuyerAccount.v1"
    account_id: str
    tenant_id: str
    revision: int = Field(ge=1)
    legal_name: str
    normalized_name: str
    aliases: list[str] = Field(default_factory=list)
    country: Optional[str] = None
    buyer_type: Optional[Literal["PUBLIC", "PRIVATE"]] = None
    identifiers: dict[str, str] = Field(default_factory=dict)
    relationship_stage: RelationshipStage = "NONE"
    do_not_contact: bool = False
    named_buyer_validated: bool = Field(False, description="confirmed by an N0-approved CRM record or interview")
    service_categories: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    synthetic_only: bool = True
    match_method: Literal["NEW", "IDENTIFIER", "EXACT_NAME", "FUZZY_NAME"] = "NEW"
    updated_at: datetime


class FxRate(_Strict):
    currency: str = Field(pattern=_CCY)
    rate_to_base: Decimal = Field(gt=0, description="1 unit of currency = rate_to_base units of base currency")
    as_of: date
    source: str = Field(min_length=2, description="e.g. 'ECB reference rate'")


class FxConversion(_Strict):
    original: MarketAmount
    base_currency: str
    rate: Decimal
    fx_as_of: date
    fx_source: str
    amount_base: Decimal
    fx_stale: bool


class EvidenceFeature(_Strict):
    evidence_id: str
    evidence_revision: int
    evidence_sha256: str
    account_id: Optional[str]
    observation_type: ObservationType
    source_connector: SourceConnector
    source_url: Optional[str]
    n0_artifact: str = Field(description="ARTIFACT_ID@REVISION of the N0 artifact that admitted this evidence")
    service_category: Optional[str]
    category_source: Literal["GIVEN", "CODE", "KEYWORD", "NONE"]
    spend_type: SpendType
    spend_reason: str
    fx: Optional[FxConversion] = None
    age_days: int
    sla_days: int
    freshness_score: float = Field(ge=0, le=1)
    attested: bool
    attestation_id: Optional[str] = None
    synthetic: bool
    exclusions: list[str] = Field(default_factory=list)
    exclusion_detail: list[str] = Field(default_factory=list)
    counts_as: CountsAs


# ----------------------------------------------------------------------------------------------
# opportunity scoring (N1.3)
# ----------------------------------------------------------------------------------------------
class ScoreComponent(_Strict):
    name: str
    points: float
    max_points: float
    reason: str


class OpportunityAssessment(_Strict):
    model_config = ConfigDict(extra="forbid", title="OpportunityAssessment.v1")
    schema_version: Literal["OpportunityAssessment.v1"] = "OpportunityAssessment.v1"
    opportunity_id: str
    account_id: str
    legal_name: str
    country: Optional[str] = None
    service_categories: list[str]
    heuristic_priority_score: float = Field(ge=0, le=100)
    score_kind: Literal["HEURISTIC_PRIORITIZATION"] = "HEURISTIC_PRIORITIZATION"
    calibrated: Literal[False] = False
    win_probability: None = Field(default=None, description="always null: no calibrated model exists yet")
    components: list[ScoreComponent]
    penalties: list[ScoreComponent]
    risk_flags: list[str]
    missing_information: list[str]
    verified_spend_base: Decimal
    indicative_spend_base: Decimal
    hypothetical_spend_base: Decimal
    evidence_ids: list[str]
    tender_deadlines: list[TenderDeadline] = Field(default_factory=list)
    synthetic_only: bool
    rfq_seed_evidence_id: Optional[str] = None
    rfq_seed_blockers: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------------------------------------
# report (N1.6)
# ----------------------------------------------------------------------------------------------
class ReportRequest(_Strict):
    tenant_id: str = Field(pattern=_ID)
    scope_name: str = Field(default="default", pattern=_ID)
    serviceable_categories: list[str] = Field(min_length=1)
    base_currency: str = Field(default="USD", pattern=_CCY)
    fx_rates: list[FxRate] = Field(default_factory=list)
    as_of: Optional[datetime] = None
    account_ids: Optional[list[str]] = None
    top_n: int = Field(default=25, ge=1, le=500)
    freshness_sla_days: dict[ObservationType, int] = Field(default_factory=dict)


class SamSummary(_Strict):
    base_currency: str
    verified_real_sam: Decimal
    indicative_spend: Decimal
    hypothetical_addressable_spend: Decimal
    excluded_spend: Decimal
    verified_by_category: dict[str, Decimal]
    verified_evidence_ids: list[str]
    indicative_evidence_ids: list[str]
    hypothetical_evidence_ids: list[str]
    synthetic_evidence_count: int
    disclaimer: str = SAM_DISCLAIMER


class Exclusion(_Strict):
    evidence_id: str
    codes: list[str]
    detail: list[str]


class RiskFlag(_Strict):
    code: str
    account_id: Optional[str] = None
    evidence_id: Optional[str] = None
    detail: str


class Citation(_Strict):
    evidence_id: str
    evidence_revision: int
    source_url: Optional[str]
    source_connector: str
    observation_type: str
    published_at: datetime
    n0_artifact: str


class MlReadiness(_Strict):
    labeled_outcomes: int
    required: int
    ready: bool
    note: str


class MarketReport(_Strict):
    model_config = ConfigDict(extra="forbid", title="MarketReport.v1")
    schema_version: Literal["MarketReport.v1"] = "MarketReport.v1"
    report_id: str
    tenant_id: str
    scope_name: str
    scope_key: str
    revision: int = Field(ge=1)
    supersedes_report_id: Optional[str] = None
    as_of: datetime
    generated_at: datetime
    generated_by: str
    engine_version: str
    base_currency: str
    fx_table: list[FxRate]
    serviceable_categories: list[str]
    sam: SamSummary
    opportunities: list[OpportunityAssessment]
    exclusions: list[Exclusion]
    risk_flags: list[RiskFlag]
    pending_attestations: list[str]
    missing_information: list[str]
    citations: list[Citation]
    evidence_features: list[EvidenceFeature]
    ml_readiness: MlReadiness
    disclaimers: list[str]
    report_sha256: Optional[str] = None


class ReportSummary(_Strict):
    report_id: str
    scope_name: str
    revision: int
    as_of: datetime
    verified_real_sam: Decimal
    hypothetical_addressable_spend: Decimal
    base_currency: str
    superseded: bool


# ----------------------------------------------------------------------------------------------
# attestation & feedback (N1.6)
# ----------------------------------------------------------------------------------------------
AttestationMethod = Literal["DOCUMENT_REVIEW", "OFFICIAL_PORTAL_CHECK", "BUYER_CONFIRMATION", "CONTRACT_ON_FILE"]


class AttestationRequest(_Strict):
    evidence_id: str
    status: Literal["ATTESTED", "REJECTED", "NEEDS_INFO"]
    method: AttestationMethod
    notes: str = Field(min_length=5)


class EvidenceAttestation(_Strict):
    model_config = ConfigDict(extra="forbid", title="EvidenceAttestation.v1")
    schema_version: Literal["EvidenceAttestation.v1"] = "EvidenceAttestation.v1"
    attestation_id: str
    tenant_id: str
    evidence_id: str
    evidence_revision: int
    evidence_sha256: str = Field(pattern=_SHA, description="attestation covers exactly this content")
    status: Literal["ATTESTED", "REJECTED", "NEEDS_INFO"]
    method: AttestationMethod
    notes: str
    attested_by: str
    attested_at: datetime
    approval_event_id: str


FeedbackLabel = Literal["OUTCOME_WON", "OUTCOME_LOST", "OUTCOME_NO_BID", "CORRECT_ENTITY_MATCH",
                        "WRONG_ENTITY_MATCH", "WRONG_CATEGORY", "WRONG_SPEND_TYPE", "EXTRACTION_ERROR", "OTHER"]


class FeedbackRequest(_Strict):
    target_kind: Literal["REPORT", "EVIDENCE", "ACCOUNT", "OPPORTUNITY", "EXTRACTION"]
    target_id: str
    label: FeedbackLabel
    corrected_value: Optional[Any] = None
    reason: str = Field(min_length=3)


class FeedbackCase(_Strict):
    model_config = ConfigDict(extra="forbid", title="FeedbackCase.v1")
    schema_version: Literal["FeedbackCase.v1"] = "FeedbackCase.v1"
    case_id: str
    tenant_id: str
    target_kind: str
    target_id: str
    label: FeedbackLabel
    corrected_value: Optional[Any] = None
    reason: str
    created_by: str
    created_at: datetime
    synthetic_target: bool
    training_eligible: bool = Field(description="only labels on real (non-synthetic) targets feed SLM/ML evaluation")


# ----------------------------------------------------------------------------------------------
# cost-weighted decisions (N1.5)
# ----------------------------------------------------------------------------------------------
ActionType = Literal["DESK_RESEARCH", "REQUEST_ATTESTATION", "PURCHASE_MARKET_DATA", "REQUEST_BUYER_INTERVIEW",
                     "EMAIL_OUTREACH", "ATTEND_PREBID_MEETING", "SUBMIT_CLARIFICATION_QUESTION", "SUBMIT_BID",
                     "PORTAL_SCRAPE", "AUTO_OUTREACH_CAMPAIGN"]


class CandidateAction(_Strict):
    action_id: str = Field(pattern=_ID)
    action_type: ActionType
    target_account_id: Optional[str] = None
    target_evidence_id: Optional[str] = None
    currency: str = Field(pattern=_CCY)
    execution_cost: Decimal = Field(ge=0)
    delay_cost_per_day: Decimal = Field(default=Decimal(0), ge=0)
    expected_delay_days: int = Field(default=0, ge=0)
    expected_loss_avoided: Decimal = Field(ge=0, description="user-supplied; N1 does not invent probabilities")
    description: Optional[str] = None


class ActionOptimizationRequest(_Strict):
    model_config = ConfigDict(extra="forbid", title="ActionOptimizationRequest.v1")
    tenant_id: str = Field(pattern=_ID)
    report_id: Optional[str] = None
    currency: str = Field(pattern=_CCY)
    budget_cap: Optional[Decimal] = Field(default=None, ge=0)
    actions: list[CandidateAction] = Field(min_length=1)


class RankedAction(_Strict):
    rank: int
    action_id: str
    action_type: str
    target_account_id: Optional[str]
    execution_cost: Decimal
    delay_cost: Decimal
    expected_loss_avoided: Decimal
    net_value: Decimal
    recommended: bool
    within_budget: bool
    rationale: str
    status: Literal["RECOMMENDED_NOT_EXECUTED"] = "RECOMMENDED_NOT_EXECUTED"


class BlockedAction(_Strict):
    action_id: str
    action_type: str
    policy_codes: list[str]
    reason: str


class ActionOptimizationResult(_Strict):
    model_config = ConfigDict(extra="forbid", title="ActionOptimizationResult.v1")
    schema_version: Literal["ActionOptimizationResult.v1"] = "ActionOptimizationResult.v1"
    optimization_id: str
    tenant_id: str
    report_id: Optional[str]
    currency: str
    ranked: list[RankedAction]
    blocked: list[BlockedAction]
    total_recommended_cost: Decimal
    executed: Literal[False] = False
    execution_note: str = OPTIMIZER_NOTE
    created_by: str
    created_at: datetime


# ----------------------------------------------------------------------------------------------
# connectors (N1.1)
# ----------------------------------------------------------------------------------------------
class ConnectorPreviewRequest(_Strict):
    tenant_id: str = Field(pattern=_ID)
    mode: Literal["offline", "live", "drop_zone"] = "offline"
    query: dict[str, Any] = Field(default_factory=dict)
    raw_payload: Optional[Any] = Field(default=None, description="offline: a raw API response to normalize")


class ConnectorPreview(_Strict):
    model_config = ConfigDict(extra="forbid", title="ConnectorPreview.v1")
    schema_version: Literal["ConnectorPreview.v1"] = "ConnectorPreview.v1"
    preview_id: str
    tenant_id: str
    connector: str
    mode: str
    request: dict[str, Any] = Field(description="sanitized: credentials are never recorded")
    retrieved_at: datetime
    raw_sha256: str = Field(pattern=_SHA)
    raw_uri: str
    record_count: int
    observations: list[MarketObservation] = Field(default_factory=list)
    buyer_records: list[BuyerRecord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_by: str


class ConnectorImport(_Strict):
    model_config = ConfigDict(extra="forbid", title="ConnectorImport.v1")
    tenant_id: str = Field(pattern=_ID)
    preview_id: str
    project_id: str = Field(pattern=_ID)
    source_id: str = Field(pattern=_ID)
    synthetic: bool = False
    evidence_ids: Optional[list[str]] = Field(default=None, description="import only these previewed records")


# ----------------------------------------------------------------------------------------------
# SLM extraction
# ----------------------------------------------------------------------------------------------
class SlmExtractionRequest(_Strict):
    tenant_id: str = Field(pattern=_ID)
    source_id: str = Field(pattern=_ID)
    evidence_id: str = Field(pattern=_ID)
    text: str = Field(min_length=20, max_length=50000)
    observation_type: ObservationType = "TENDER_NOTICE"
    source_connector: SourceConnector = "MANUAL"
    source_url: Optional[str] = None
    published_at: datetime


class SlmEvaluation(_Strict):
    model_config = ConfigDict(extra="forbid", title="SlmEvaluation.v1")
    evaluation_id: str
    tenant_id: str
    provider: str
    model: str
    prompt_revision: str
    gold_set: str
    gold_set_sha256: str
    n_examples: int
    metrics: dict[str, float]
    thresholds: dict[str, float]
    failures: list[str]
    passed: bool
    evaluated_by: str
    evaluated_at: datetime


class SlmExtractionResult(_Strict):
    observation: MarketObservation
    model_run: ModelRun
    evaluation_id: str
    missing_fields: list[str]
    warnings: list[str]


# ----------------------------------------------------------------------------------------------
# explainer
# ----------------------------------------------------------------------------------------------
class ExplainedStatement(_Strict):
    text: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1)


class ReportExplanation(_Strict):
    report_id: str
    provider: str
    model: str
    summary: list[ExplainedStatement]
    missing_information: list[ExplainedStatement]
    next_steps: list[ExplainedStatement]
    model_run_id: Optional[str] = None


# ----------------------------------------------------------------------------------------------
# N1 → N2 / N6 / N7 handoff
# ----------------------------------------------------------------------------------------------
class HandoffRequest(_Strict):
    targets: list[Literal["N2", "N6", "N7"]] = Field(min_length=1)
    derived_source_id: str = Field(pattern=_ID, description="SourceRecord for N1-derived intelligence")
    project_id: str = Field(pattern=_ID)
    opportunity_ids: Optional[list[str]] = Field(default=None, description="N2 seeds only for these")
    optimization_id: Optional[str] = Field(default=None, description="costed actions to include for N7")


class HandoffPackage(_Strict):
    target: Literal["N2", "N6", "N7"]
    artifact_type: str
    artifact_id: str
    revision: str
    status: Literal["RELEASED", "REFUSED"]
    n0_decision: Optional[Decision] = None
    n0_decision_id: Optional[str] = None
    violated_codes: list[str] = Field(default_factory=list)
    manifest_id: Optional[str] = None
    approval_id: Optional[str] = None
    reason: str
    excluded_evidence: list[str] = Field(default_factory=list)


class HandoffResult(_Strict):
    model_config = ConfigDict(extra="forbid", title="N1Handoff.v1")
    handoff_id: str
    tenant_id: str
    report_id: str
    report_sha256: str
    requested_by: str
    created_at: datetime
    packages: list[HandoffPackage]


N1_SCHEMAS: dict[str, type[BaseModel]] = {
    "n1_market_batch": MarketBatch, "n1_market_observation": MarketObservation, "n1_buyer_account": BuyerAccount,
    "n1_opportunity_assessment": OpportunityAssessment, "n1_market_report": MarketReport,
    "n1_action_optimization_request": ActionOptimizationRequest,
    "n1_action_optimization_result": ActionOptimizationResult, "n1_evidence_attestation": EvidenceAttestation,
    "n1_feedback_case": FeedbackCase, "n1_connector_import": ConnectorImport, "n1_connector_preview": ConnectorPreview,
    "n1_handoff_result": HandoffResult, "n1_slm_evaluation": SlmEvaluation,
}
