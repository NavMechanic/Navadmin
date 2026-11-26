"""N5 contracts (Pydantic v2) → ``schemas/n5_*.schema.json`` (JSON Schema 2020-12).

All money is ``Decimal`` (serialised as a string); no float ever enters a price, quantity or total.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..models import (EXCLUDED_COST_CATEGORIES, INDICATIVE_MATERIAL_LABEL, Availability, FxAssumption, LineCharge,
                      PriceBreak, SupplierKind)
from . import APPROVAL_SCOPE

_ID = r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}$"
_SHA = r"^[0-9a-f]{64}$"
_CCY = r"^[A-Z]{3}$"
Decision = Literal["PASS", "REWORK", "BLOCK"]
Outcome = Literal["BLOCK", "REWORK", "INFO"]
Purpose = Literal["PROTOTYPE_BUILD", "PILOT_BUILD", "PRODUCTION"]
QuoteChannel = Literal["MANUAL", "DIGIKEY_API", "MOUSER_API", "NEXAR_API", "EMS_DROPZONE"]
ConnectorName = Literal["DIGIKEY", "MOUSER", "NEXAR", "OPEN_SUPPLY_HUB"]
ExcludedChargeKind = Literal["FREIGHT", "DUTIES", "GST", "OTHER_TAX", "NRE", "TOOLING", "ASSEMBLY_LABOR",
                             "TEST_LABOR", "WARRANTY", "FINANCING"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ----------------------------------------------------------------------------------------------
# N5.1 BOM ingestion & normalisation
# ----------------------------------------------------------------------------------------------
class CriticalLine(_Strict):
    line: int = Field(ge=1)
    reason: str = Field(min_length=3)


class BomIntakeRequest(_Strict):
    model_config = ConfigDict(extra="forbid", title="N5BomIntakeRequest")
    prd_id: str = Field(pattern=_ID)
    project_id: str = Field(pattern=_ID)
    scope: Literal["schematic_pcb"] = "schematic_pcb"
    build_quantity: int = Field(ge=1, le=10_000_000, description="finished products to build")
    purpose: Purpose = "PROTOTYPE_BUILD"
    target_lead_time_days: int = Field(ge=0, le=730)
    release_source_id: str = Field(description="SourceRecord permitting COMMERCIAL and FINANCE for the handoffs")
    base_currency: Optional[str] = Field(default=None, pattern=_CCY)
    critical_lines: list[CriticalLine] = Field(default_factory=list)


class N5BomLine(_Strict):
    model_config = ConfigDict(extra="forbid", title="N5BomLine")
    line: int = Field(ge=1)
    mpn: str = Field(description="exact-MPN key: whitespace removed, upper-cased; suffixes stay significant")
    mpn_as_released: str
    manufacturer: Optional[str] = None
    refdes: list[str]
    qty_per_unit: int = Field(ge=1)
    uom: Literal["EA"] = "EA"
    q_required: int = Field(ge=1, description="qty_per_unit × build_quantity")
    critical: bool = False
    critical_reason: Optional[str] = None
    value: Optional[str] = None
    footprint: Optional[str] = None
    description: Optional[str] = None


class N5BomIntake(_Strict):
    model_config = ConfigDict(extra="forbid", title="N5BomIntake")
    schema_version: Literal["N5BomIntake.v1"] = "N5BomIntake.v1"
    intake_id: str
    tenant_id: str
    project_id: str
    prd_id: str
    prd_ref: str
    bom_ref: str = Field(description="N3 BOM_RELEASE ARTIFACT_ID@REVISION")
    bom_payload_sha256: str = Field(pattern=_SHA)
    manifest_id: str
    approval_id: str
    n3_release_id: str
    scope: str
    design_revision: str
    pcb_revision: Optional[str] = None
    build_quantity: int = Field(ge=1)
    purpose: Purpose
    target_lead_time_days: int = Field(ge=0)
    base_currency: str = Field(pattern=_CCY)
    lines: list[N5BomLine] = Field(min_length=1)
    dnp_lines: list[int] = Field(default_factory=list)
    release_source_id: str
    synthetic: bool
    verification_ref: Optional[str] = Field(default=None, description="N4 VERIFICATION_RELEASE for this revision")
    verification_status: Literal["VERIFIED", "NOT_VERIFIED"] = "NOT_VERIFIED"
    locked_by: str
    locked_at: datetime
    intake_sha256: Optional[str] = None
    approval_scope: str = APPROVAL_SCOPE


# ----------------------------------------------------------------------------------------------
# N5.2 supplier intelligence
# ----------------------------------------------------------------------------------------------
class SiteRef(_Strict):
    model_config = ConfigDict(extra="forbid", title="N5Site")
    site_id: str = Field(pattern=_ID)
    name: str = Field(min_length=2)
    country: str = Field(pattern=r"^[A-Z]{2}$")
    os_id: Optional[str] = Field(default=None, pattern=r"^[A-Z]{2}[0-9A-Z]{8,20}$",
                                 description="Open Supply Hub facility id")
    capabilities: list[str] = Field(default_factory=list)
    osh_verified: bool = False
    osh_evidence: Optional[dict[str, Any]] = None


class ChannelProof(_Strict):
    model_config = ConfigDict(extra="forbid", title="N5ChannelProof")
    manufacturer: str = Field(min_length=2)
    proof_type: Literal["MANUFACTURER_AUTHORIZED_LIST", "FRANCHISE_AGREEMENT", "MANUFACTURER_LETTER"]
    reference: str = Field(min_length=3, description="URL of the manufacturer's distributor list, or a document id")
    document_sha256: Optional[str] = Field(default=None, pattern=_SHA)
    valid_until: date


class SigningKey(_Strict):
    key_id: str = Field(pattern=_ID)
    algorithm: Literal["ED25519", "RSA_PSS_SHA256", "RSA_PKCS1_SHA256"]
    public_key_pem: str = Field(min_length=40)


class SupplierRequest(_Strict):
    model_config = ConfigDict(extra="forbid", title="N5SupplierRequest")
    supplier_id: str = Field(pattern=_ID)
    kind: SupplierKind
    legal_name: str = Field(min_length=2)
    country: str = Field(pattern=r"^[A-Z]{2}$")
    registration_ids: dict[str, str] = Field(min_length=1, description="e.g. GSTIN, DUNS, LEI, company number")
    manufacturer_names: list[str] = Field(default_factory=list, description="MANUFACTURER: the brands it makes")
    sites: list[SiteRef] = Field(default_factory=list)
    channel_proofs: list[ChannelProof] = Field(default_factory=list)
    signing_keys: list[SigningKey] = Field(default_factory=list, description="keys for signed drop-zone quotes")


class N5Supplier(SupplierRequest):
    model_config = ConfigDict(extra="forbid", title="N5Supplier")
    tenant_id: str
    status: Literal["PENDING", "APPROVED", "REJECTED", "SUSPENDED"]
    version: int = Field(ge=1)
    submitted_by: str
    submitted_at: datetime
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    attestation: Optional[str] = None
    approved_until: Optional[date] = None
    verified_channel_manufacturers: list[str] = Field(default_factory=list)
    supplier_sha256: Optional[str] = None


class SupplierReviewRequest(_Strict):
    model_config = ConfigDict(extra="forbid", title="N5SupplierReviewRequest")
    decision: Literal["APPROVED", "REJECTED", "SUSPENDED"]
    attestation: str = Field(min_length=20, description="what the reviewer checked (identity, sites, channel proofs)")
    approved_until: Optional[date] = None
    verified_channel_manufacturers: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------------------------------------
# N5.3 quotation intake
# ----------------------------------------------------------------------------------------------
class CapacityAllocation(_Strict):
    model_config = ConfigDict(extra="forbid", title="N5CapacityAllocation")
    allocated_qty: int = Field(ge=1, description="finished units (EMS / ODM) or parts (factory order) reserved")
    period_start: date
    period_end: date
    site_id: Optional[str] = None
    document_sha256: Optional[str] = Field(default=None, pattern=_SHA)


class ExcludedCharge(_Strict):
    kind: ExcludedChargeKind
    amount: Decimal = Field(ge=0)
    currency: str = Field(pattern=_CCY)
    note: Optional[str] = None


class OfferIn(_Strict):
    line: Optional[int] = Field(default=None, ge=1, description="the BOM line this offer answers")
    quoted_mpn: str = Field(min_length=1)
    manufacturer: Optional[str] = None
    uom: str = "EA"
    currency: str = Field(pattern=_CCY)
    price_breaks: list[PriceBreak] = Field(min_length=1)
    moq: int = Field(ge=1)
    pack_multiple: int = Field(ge=1)
    availability: Availability = "STOCK"
    stock_qty: Optional[int] = Field(default=None, ge=0)
    lead_time_days: int = Field(ge=0)
    line_charges: list[LineCharge] = Field(default_factory=list)
    excluded_charges: list[ExcludedCharge] = Field(default_factory=list)
    capacity: Optional[CapacityAllocation] = None
    supplier_sku: Optional[str] = None


class QuoteRequest(_Strict):
    """``athena-quote/1`` (manual / e-mail / portal quotes keyed in by procurement)."""
    model_config = ConfigDict(extra="forbid", title="N5QuoteRequest")
    format: Literal["athena-quote/1"] = "athena-quote/1"
    supplier_id: str = Field(pattern=_ID)
    bom_intake_id: str
    design_revision: str = Field(description="the BOM / design revision the supplier quoted against")
    quote_ref: str = Field(min_length=1, description="the supplier's own quotation number")
    issued_at: datetime
    valid_until: datetime
    source_id: str
    offers: list[OfferIn] = Field(min_length=1)


class N5Offer(OfferIn):
    model_config = ConfigDict(extra="forbid", title="N5Offer")
    offer_id: str
    quote_id: str


class QuoteFile(_Strict):
    path: str
    sha256: str = Field(pattern=_SHA)
    role: Literal["QUOTE_DOCUMENT", "MANIFEST", "CAPACITY_EVIDENCE", "SERVICE_TERMS", "EDI", "OTHER"]
    size_bytes: int = Field(ge=0)


class ConnectorProvenance(_Strict):
    connector: ConnectorName
    operation: str
    call_id: str
    retrieved_at: datetime
    response_sha256: str = Field(pattern=_SHA)
    cache_hit: bool = False
    terms: str = Field(description="the licence terms the data is held under")


class N5Quote(_Strict):
    model_config = ConfigDict(extra="forbid", title="N5Quote")
    schema_version: Literal["N5Quote.v1"] = "N5Quote.v1"
    quote_id: str
    tenant_id: str
    bom_intake_id: str
    supplier_id: str
    channel: QuoteChannel
    source_id: str
    quote_ref: str
    design_revision: str
    issued_at: datetime
    valid_until: datetime
    offers: list[N5Offer]
    nre: list[ExcludedCharge] = Field(default_factory=list, description="reported to N7, never in the total")
    service_terms: Optional[str] = None
    files: list[QuoteFile] = Field(default_factory=list)
    signature: Optional[dict[str, Any]] = None
    provenance: Optional[ConnectorProvenance] = None
    data_use: dict[str, Any] = Field(default_factory=lambda: {
        "training_allowed": False, "redistribution": "INTERNAL_PROJECT_ONLY", "retention": "PROJECT_DECISION_RECORD"})
    synthetic: bool = False
    status: Literal["ACCEPTED", "REJECTED"]
    rejection_codes: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    submitted_by: str
    submitted_at: datetime
    quote_sha256: Optional[str] = Field(default=None, pattern=_SHA)


class EmsQuoteManifest(_Strict):
    """``athena-ems-quote/1``: the manifest that travels with a signed EMS / ODM quotation."""
    model_config = ConfigDict(extra="forbid", title="N5EmsQuoteManifest")
    format: Literal["athena-ems-quote/1"] = "athena-ems-quote/1"
    supplier_id: str
    bom_intake_id: str
    design_revision: str
    quote_ref: str
    issued_at: datetime
    valid_until: datetime
    quote_document: str = Field(description="file name of the signed quotation PDF / CSV / EDI")
    quote_document_sha256: str = Field(pattern=_SHA)
    signature: dict[str, str] = Field(description="{key_id, algorithm, value (base64 over the document bytes)}")
    offers: list[OfferIn] = Field(default_factory=list, description="optional when the document is CSV / EDI")
    capacity: Optional[CapacityAllocation] = None
    nre: list[ExcludedCharge] = Field(default_factory=list)
    service_terms: Optional[str] = None
    attachments: list[dict[str, str]] = Field(default_factory=list, description="[{path, sha256, role}]")


class FxAssumptionRequest(FxAssumption):
    model_config = ConfigDict(extra="forbid", title="N5FxAssumption")


class N5FxAssumption(FxAssumption):
    model_config = ConfigDict(extra="forbid", title="N5FxAssumptionRecord")
    tenant_id: str
    recorded_by: str
    recorded_at: datetime


class LookupRequest(_Strict):
    model_config = ConfigDict(extra="forbid", title="N5LookupRequest")
    connectors: list[Literal["DIGIKEY", "MOUSER", "NEXAR"]] = Field(min_length=1)
    lines: Optional[list[int]] = Field(default=None, description="BOM lines to look up (default: all)")
    supplier_ids: dict[str, str] = Field(default_factory=dict,
                                         description="connector → N5 supplier record that sells through it")


class N5ConnectorCall(_Strict):
    model_config = ConfigDict(extra="forbid", title="N5ConnectorCall")
    call_id: str
    tenant_id: str
    connector: ConnectorName
    operation: str
    project_id: Optional[str] = None
    bom_intake_id: Optional[str] = None
    line: Optional[int] = None
    key: str = Field(description="MPN or OS ID looked up (never a bulk query)")
    requested_by: str
    at: datetime
    cache_hit: bool
    http_status: Optional[int] = None
    response_sha256: Optional[str] = None
    outcome: Literal["OK", "NOT_FOUND", "ERROR", "DISABLED", "RATE_LIMITED", "OUT_OF_SCOPE"]
    error: Optional[str] = None


# ----------------------------------------------------------------------------------------------
# N5.4 – N5.5 guardrails and deterministic optimisation
# ----------------------------------------------------------------------------------------------
class SourcingReportRequest(_Strict):
    model_config = ConfigDict(extra="forbid", title="N5SourcingReportRequest")
    bom_intake_id: str
    quote_ids: Optional[list[str]] = Field(default=None, description="default: every accepted quote of the intake")


class GuardrailFinding(_Strict):
    model_config = ConfigDict(extra="forbid", title="N5GuardrailFinding")
    finding_id: str
    code: str
    outcome: Outcome
    level: Literal["OFFER", "LINE", "REPORT"]
    message: str
    line: Optional[int] = None
    offer_id: Optional[str] = None
    quote_id: Optional[str] = None
    supplier_id: Optional[str] = None


class RankedOffer(_Strict):
    offer_id: str
    quote_id: str
    quote_sha256: Optional[str] = None
    supplier_id: str
    supplier_kind: Optional[SupplierKind] = None
    quoted_mpn: str
    eligible: bool
    rank: Optional[int] = Field(default=None, ge=1, description="1 = cheapest eligible by extended cost")
    codes: list[str] = Field(default_factory=list)
    q_order: Optional[int] = None
    price_break_min_qty: Optional[int] = None
    unit_price: Optional[Decimal] = None
    currency: str
    line_charges_total: Optional[Decimal] = None
    extended_cost: Optional[Decimal] = None
    fx_id: Optional[str] = None
    fx_rate: Optional[Decimal] = None
    extended_cost_base: Optional[Decimal] = None
    availability: Availability
    stock_qty: Optional[int] = None
    lead_time_days: int
    step_up: Optional[dict[str, Any]] = Field(default=None, description="advice only, never applied")


class LineResult(_Strict):
    line: int
    mpn: str
    manufacturer: Optional[str] = None
    qty_per_unit: int
    q_required: int
    critical: bool
    status: Literal["SOURCED", "UNSOURCED"]
    selected_offer_id: Optional[str] = None
    offers: list[RankedOffer] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)


class N5SourcingReport(_Strict):
    model_config = ConfigDict(extra="forbid", title="N5SourcingReport")
    schema_version: Literal["N5SourcingReport.v1"] = "N5SourcingReport.v1"
    report_id: str
    tenant_id: str
    bom_intake_id: str
    intake_sha256: str
    bom_ref: str
    design_revision: str
    build_quantity: int
    base_currency: str
    decision: Decision
    findings: list[GuardrailFinding]
    lines: list[LineResult]
    quote_ids: list[str]
    quote_hashes: list[str] = Field(description="SHA-256 of every quote whose offer was selected")
    all_quote_hashes: list[str]
    quote_source_ids: list[str]
    fx_assumptions: list[FxAssumption] = Field(default_factory=list)
    indicative_total: Optional[Decimal] = None
    indicative_unit_cost: Optional[Decimal] = None
    indicative_scope: str = INDICATIVE_MATERIAL_LABEL
    excluded_cost_categories: list[str] = Field(default_factory=lambda: list(EXCLUDED_COST_CATEGORIES))
    excluded_items_reported: list[dict[str, Any]] = Field(default_factory=list)
    synthetic: bool = False
    engine_version: str
    created_by: str
    created_at: datetime
    report_sha256: Optional[str] = None
    approval_scope: str = APPROVAL_SCOPE


# ----------------------------------------------------------------------------------------------
# N5.6 review and digital thread
# ----------------------------------------------------------------------------------------------
class N5ReviewRequest(_Strict):
    model_config = ConfigDict(extra="forbid", title="N5ReviewRequest")
    report_id: str
    report_sha256: str = Field(pattern=_SHA)
    quote_hashes: list[str] = Field(min_length=1, description="the selected quote hashes, exactly as reported")
    decision: Literal["APPROVED", "REJECTED"]
    reason: str = Field(min_length=3)


class N5Review(_Strict):
    model_config = ConfigDict(extra="forbid", title="N5Review")
    review_id: str
    tenant_id: str
    report_id: str
    reviewer: str
    reviewer_role: str
    decision: Literal["APPROVED", "REJECTED"]
    reason: str
    report_sha256: str
    quote_hashes: list[str]
    approval_event_id: str
    reviewed_at: datetime


class HandoffRequest(_Strict):
    targets: list[Literal["N6", "N7"]] = Field(min_length=1)


class N5HandoffPackage(_Strict):
    target: Literal["N6", "N7"]
    artifact_id: str
    revision: Optional[str] = None
    status: Literal["RELEASED", "REFUSED"]
    n0_decision: Optional[str] = None
    n0_decision_id: Optional[str] = None
    violated_codes: list[str] = Field(default_factory=list)
    manifest_id: Optional[str] = None
    approval_id: Optional[str] = None
    reason: str


class N5Handoff(_Strict):
    model_config = ConfigDict(extra="forbid", title="N5Handoff")
    handoff_id: str
    tenant_id: str
    report_id: str
    review_id: str
    bom_intake_id: str
    design_revision: str
    status: Literal["RELEASED", "PARTIAL", "REFUSED"]
    packages: list[N5HandoffPackage]
    quote_source_ids: list[str]
    indicative_total: Decimal
    base_currency: str
    released_by: str
    released_at: datetime
    approval_scope: str = APPROVAL_SCOPE


class EquivalenceEvidence(_Strict):
    kind: Literal["DATASHEET", "PARAMETRIC_COMPARISON", "QUALIFICATION_REPORT", "MANUFACTURER_PCN", "CROSS_REFERENCE"]
    reference: str = Field(min_length=3)
    sha256: Optional[str] = Field(default=None, pattern=_SHA)
    summary: str = Field(min_length=3)


class ParameterComparison(_Strict):
    parameter: str
    approved_value: str
    proposed_value: str
    unit: Optional[str] = None


class EcoProposalRequest(_Strict):
    model_config = ConfigDict(extra="forbid", title="N5EcoProposalRequest")
    bom_intake_id: str
    line: int = Field(ge=1)
    proposed_mpn: str = Field(min_length=1)
    proposed_manufacturer: str = Field(min_length=2)
    offer_id: Optional[str] = None
    reason: str = Field(min_length=10)
    equivalence_evidence: list[EquivalenceEvidence] = Field(min_length=1)
    parametric_comparison: list[ParameterComparison] = Field(min_length=1)


class N5EcoProposal(EcoProposalRequest):
    model_config = ConfigDict(extra="forbid", title="N5EcoProposal")
    eco_id: str
    tenant_id: str
    approved_mpn: str
    design_revision: str
    bom_ref: str
    status: Literal["PENDING_N3_ECO"] = "PENDING_N3_ECO"
    raised_by: str
    raised_at: datetime
    note: str = ("Pending N3 engineering change. N5 never substitutes a part: the alternate becomes eligible only "
                 "after N3 releases a new BOM revision that contains it.")


# ----------------------------------------------------------------------------------------------
N5_SCHEMAS: dict[str, type[BaseModel]] = {
    "n5_bom_intake_request": BomIntakeRequest, "n5_bom_intake": N5BomIntake, "n5_bom_line": N5BomLine,
    "n5_supplier_request": SupplierRequest, "n5_supplier": N5Supplier,
    "n5_supplier_review_request": SupplierReviewRequest, "n5_channel_proof": ChannelProof, "n5_site": SiteRef,
    "n5_quote_request": QuoteRequest, "n5_quote": N5Quote, "n5_offer": N5Offer,
    "n5_capacity_allocation": CapacityAllocation, "n5_fx_assumption": FxAssumptionRequest,
    "n5_sourcing_report_request": SourcingReportRequest, "n5_guardrail_finding": GuardrailFinding,
    "n5_sourcing_report": N5SourcingReport, "n5_review_request": N5ReviewRequest, "n5_handoff": N5Handoff,
    "n5_eco_proposal": N5EcoProposal,
}
N5_EXTRA_SCHEMAS: dict[str, type[BaseModel]] = {
    "n5_ems_quote_manifest": EmsQuoteManifest, "n5_connector_call": N5ConnectorCall, "n5_review": N5Review,
    "n5_lookup_request": LookupRequest, "n5_eco_proposal_request": EcoProposalRequest,
}
assert len(N5_SCHEMAS) == 19
