"""Typed payload schemas (``*.v1``) referenced by ``CanonicalIOManifest.output_schema``.

Each ``SchemaSpec`` declares:
  * ``critical``      — fields that must be explicitly supplied and non-empty (DQ03; Pydantic
                        defaults never count, so no silent default can satisfy them)
  * ``numeric_maps``  — dict fields whose numeric values must carry SI units (DQ04)
  * ``data_class``    — freshness SLA class (DQ07)
  * ``facts``         — what the registry keeps for cross-artifact reconciliation
All models forbid unknown fields, so typos fail validation (DQ02) instead of being ignored.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .manifest import SHA256, NodeId


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FileRef(_Strict):
    name: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256)
    format: str = Field(min_length=1)
    size_bytes: Optional[int] = Field(default=None, ge=0)


# --- customer / market --------------------------------------------------------------

class CustomerRFQ(_Strict):
    rfq_id: str
    customer_id: str
    service_mode: Literal["SINGLE_NODE", "MULTI_NODE", "FULL_PIPELINE"]
    requested_nodes: list[NodeId]
    product_category: str
    application_use: str
    delivery_country: str = Field(pattern=r"^[A-Z]{2}$")
    consent_scope: Literal["INTERNAL_ONLY", "VENDOR_ALLOWED"]
    specs: dict[str, float] = {}
    compliance_requirements: list[str] = []
    files: list[FileRef] = []


class MarketOpportunity(_Strict):
    opportunity_id: str
    account_id: str
    account_name: str
    segment: str
    basis: Literal["SAM", "SOM", "TAM"]
    annual_spend_inr: Optional[Decimal] = Field(default=None, ge=0)
    spend_provenance: list[str]


# --- product / design / quality -------------------------------------------------------

class Requirement(_Strict):
    req_id: str
    text: str
    critical: bool
    verification_ids: list[str] = []


class ProductPRD(_Strict):
    product_id: str
    requirements: list[Requirement]
    specs: dict[str, float] = {}


class TraceLink(_Strict):
    req_id: str
    verification_id: str


class SpecTrace(_Strict):
    product_id: str
    trace: list[TraceLink]


class EDAFinding(_Strict):
    check: Literal["ERC", "DRC"]
    rule: str
    severity: Literal["INFO", "WARNING", "ERROR", "CRITICAL"]
    status: Literal["OPEN", "RESOLVED", "WAIVED"]
    waiver_evidence_id: Optional[str] = None


class EDAReport(_Strict):
    tool: str
    findings: list[EDAFinding]


class DesignPackage(_Strict):
    product_id: str
    design_revision: str
    files: list[FileRef]
    eda_report: EDAReport
    specs: dict[str, float] = {}


class Instrument(_Strict):
    instrument_id: str
    calibration_id: str


class VerificationTest(_Strict):
    test_id: str
    req_ids: list[str] = []
    safety_critical: bool
    result: Literal["PASS", "FAIL", "NOT_RUN"]
    instrument_id: Optional[str] = None
    signed_evidence_id: Optional[str] = None


class VerificationResult(_Strict):
    product_id: str
    instruments: list[Instrument]
    tests: list[VerificationTest]
    specs: dict[str, float] = {}


# --- sourcing / commercial / finance ------------------------------------------------------

class BOMLine(_Strict):
    mpn: str
    manufacturer: str
    qty_per_unit: int = Field(gt=0)
    refdes: list[str] = []


class BOMItem(_Strict):
    product_id: str
    bom_revision: str
    lines: list[BOMLine]
    avl_mpns: list[str]


class SupplierQuote(_Strict):
    supplier_quote_id: str
    supplier_id: str
    bom_revision: str
    mpn: str
    quoted_quantity: int = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    fx_rate_to_inr: Optional[Decimal] = Field(default=None, gt=0)
    gst_included: bool
    valid_until: date
    min_order_qty: int = Field(gt=0)
    lead_time_days: int = Field(ge=0)
    substitution_eco_id: Optional[str] = None


class BidPackage(_Strict):
    bid_id: str
    tender_id: str
    eligibility: Literal["PASS", "FAIL", "UNKNOWN"]
    price_ex_gst: Decimal = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class CostLines(_Strict):
    bom: Decimal = Field(ge=0)
    nre: Decimal = Field(ge=0)
    tooling: Decimal = Field(ge=0)
    lab: Decimal = Field(ge=0)
    finance: Decimal = Field(ge=0)


class DeclaredTotals(_Strict):
    subtotal_ex_gst: Decimal
    gst: Decimal
    total_inc_gst: Decimal


class Funding(_Strict):
    available_working_capital: Decimal = Field(ge=0)
    peak_cash_gap: Decimal = Field(ge=0)


class ProjectPnL(_Strict):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    lines: CostLines
    gst_rate: Decimal = Field(ge=0, lt=1)
    declared: DeclaredTotals
    commitment: Literal["NONE", "PO", "BID"]
    funding: Optional[Funding] = None


# --- lifecycle / AI / documents -------------------------------------------------------------

class SensorStatus(_Strict):
    sensor_id: str
    status: Literal["CALIBRATED", "UNCALIBRATED", "SIMULATED"]
    calibration_id: Optional[str] = None


class LifecycleEvent(_Strict):
    event_kind: Literal["station", "build", "test", "warranty"]
    unit_id: str      # format validated by DQ25, not here, so a bad ID is a telemetry failure (BLOCK)
    lot_id: str
    station_id: str
    event_time: datetime
    readings: dict[str, Optional[float]]
    sensor: SensorStatus
    fixture_id: Optional[str] = None


class Claim(_Strict):
    text: str
    kind: Literal["factual", "engineering", "opinion"]
    source_refs: list[str] = []


class AIOutput(_Strict):
    task: str
    claims: list[Claim]
    prompt_revision: str
    input_hashes: list[str]
    output_text: str = ""


class CorpusDoc(_Strict):
    content: str
    metadata: dict[str, Any] = {}


class DocumentCorpus(_Strict):
    source_kind: Literal["document", "pdf", "telemetry_log", "fea_mesh", "cad"]
    files: list[FileRef]
    documents: list[CorpusDoc]
    document_count: int = Field(ge=0)


# --- registry ---------------------------------------------------------------------------------

FRESHNESS_SECONDS = {
    "telemetry": 86_400, "quote": 30 * 86_400, "commercial": 30 * 86_400, "ai_output": 7 * 86_400,
    "rfq": 90 * 86_400, "market": 180 * 86_400, "engineering": 365 * 86_400, "document": 3650 * 86_400,
}


@dataclass(frozen=True)
class SchemaSpec:
    name: str
    model: type[BaseModel]
    critical: tuple[str, ...]
    data_class: str
    numeric_maps: tuple[str, ...] = ()
    node: Optional[str] = None
    facts: Callable[[Any], dict] = field(default=lambda m: {})


def _product(m) -> dict:
    return {"product_id": getattr(m, "product_id", None)}


SCHEMAS: dict[str, SchemaSpec] = {s.name: s for s in (
    SchemaSpec("customer_rfq.v1", CustomerRFQ, ("rfq_id", "customer_id", "consent_scope", "requested_nodes"), "rfq",
               ("specs",), None, lambda m: {"consent_scope": m.consent_scope}),
    SchemaSpec("market_opportunity.v1", MarketOpportunity, ("account_id", "basis", "spend_provenance"), "market", (), "N1"),
    SchemaSpec("product_prd.v1", ProductPRD, ("product_id", "requirements"), "engineering", ("specs",), "N2",
               lambda m: {**_product(m), "critical_reqs": sorted(r.req_id for r in m.requirements if r.critical)}),
    SchemaSpec("spec_trace.v1", SpecTrace, ("product_id", "trace"), "engineering", (), "N2",
               lambda m: {**_product(m), "trace": sorted({f"{t.req_id}->{t.verification_id}" for t in m.trace})}),
    SchemaSpec("design_package.v1", DesignPackage, ("product_id", "design_revision", "files", "eda_report"),
               "engineering", ("specs",), "N3", _product),
    SchemaSpec("verification_result.v1", VerificationResult, ("product_id", "instruments", "tests"), "engineering",
               ("specs",), "N4", _product),
    SchemaSpec("bom_item.v1", BOMItem, ("product_id", "bom_revision", "lines", "avl_mpns"), "engineering", (), "N5",
               lambda m: {**_product(m), "bom_revision": m.bom_revision, "avl_mpns": sorted(set(m.avl_mpns)),
                          "mpns": sorted({line.mpn for line in m.lines})}),
    SchemaSpec("supplier_quote.v1", SupplierQuote,
               ("supplier_quote_id", "supplier_id", "bom_revision", "mpn", "unit_price", "currency",
                "gst_included", "valid_until"), "quote", (), "N5",
               lambda m: {"mpns": [m.mpn], "bom_revision": m.bom_revision}),
    SchemaSpec("bid_package.v1", BidPackage, ("bid_id", "tender_id", "eligibility"), "commercial", (), "N6"),
    SchemaSpec("project_pnl.v1", ProjectPnL, ("lines", "gst_rate", "declared", "commitment"), "commercial", (), "N7"),
    SchemaSpec("lifecycle_event.v1", LifecycleEvent,
               ("unit_id", "lot_id", "station_id", "event_time", "readings", "sensor"), "telemetry", ("readings",), "N8"),
    SchemaSpec("ai_output.v1", AIOutput, ("task", "claims", "prompt_revision", "input_hashes"), "ai_output", (), "X"),
    SchemaSpec("document_corpus.v1", DocumentCorpus, ("source_kind", "files", "documents"), "document"),
)}
