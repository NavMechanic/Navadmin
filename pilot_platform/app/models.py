"""ORM models for every layer of the pilot workflow.

Immutable tables (artifact revisions, audit events) reject UPDATE/DELETE at the
ORM level; corrections are made by appending a new row.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow


class ImmutableRowError(RuntimeError):
    pass


# --- Platform (activity D) ---------------------------------------------------

class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    llm_budget_usd: Mapped[float] = mapped_column(Float, default=25.0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    username: Mapped[str] = mapped_column(String(120), unique=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    roles: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (UniqueConstraint("tenant_id", "seq"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(120))
    entity_type: Mapped[str] = mapped_column(String(60))
    entity_id: Mapped[str] = mapped_column(String(120))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


# --- Programme / CPM network --------------------------------------------------

class Programme(Base):
    __tablename__ = "programmes"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    activities: Mapped[list["Activity"]] = relationship(back_populates="programme", order_by="Activity.id")


class Activity(Base):
    __tablename__ = "activities"
    __table_args__ = (UniqueConstraint("programme_id", "code"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    programme_id: Mapped[int] = mapped_column(ForeignKey("programmes.id"), index=True)
    code: Mapped[str] = mapped_column(String(16))
    layer: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(200))
    predecessors: Mapped[list] = mapped_column(JSON, default=list)
    optimistic: Mapped[float] = mapped_column(Float)
    most_likely: Mapped[float] = mapped_column(Float)
    pessimistic: Mapped[float] = mapped_column(Float)
    owner: Mapped[str] = mapped_column(String(120))
    owner_role: Mapped[str] = mapped_column(String(60))
    technical_execution: Mapped[str] = mapped_column(Text, default="")
    output_artifact: Mapped[str] = mapped_column(Text, default="")
    gate_criteria: Mapped[str] = mapped_column(Text, default="")
    gate_spec: Mapped[dict] = mapped_column(JSON, default=dict)
    planned_start_week: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    planned_finish_week: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    risk_note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="NOT_STARTED")
    actual_duration_days: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    programme: Mapped[Programme] = relationship(back_populates="activities")


class GateReview(Base):
    __tablename__ = "gate_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    activity_id: Mapped[int] = mapped_column(ForeignKey("activities.id"), index=True)
    submitted_by: Mapped[str] = mapped_column(String(120))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    check_results: Mapped[dict] = mapped_column(JSON, default=dict)
    decision: Mapped[str] = mapped_column(String(10), default="pending")
    decided_by: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    decision_notes: Mapped[str] = mapped_column(Text, default="")
    submitted_at: Mapped[datetime] = mapped_column(default=utcnow)
    decided_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


# --- Digital thread (activity E) ---------------------------------------------

class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("tenant_id", "kind", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    key: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ArtifactRevision(Base):
    __tablename__ = "artifact_revisions"
    __table_args__ = (UniqueConstraint("artifact_id", "rev"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("artifacts.id"), index=True)
    rev: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(120))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    artifact: Mapped[Artifact] = relationship()


class RevisionLink(Base):
    __tablename__ = "revision_links"
    child_id: Mapped[int] = mapped_column(ForeignKey("artifact_revisions.id"), primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("artifact_revisions.id"), primary_key=True, index=True)


# --- LLM gateway & retrieval (activity G) ------------------------------------

class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    text: Mapped[str] = mapped_column(Text)
    acl_roles: Mapped[list] = mapped_column(JSON, default=list)  # empty = every user in the tenant
    created_by: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class LLMTrace(Base):
    __tablename__ = "llm_traces"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(120))
    task: Mapped[str] = mapped_column(String(80))
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30))  # ok | error | rejected_budget | refused
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    retrieved_doc_ids: Mapped[list] = mapped_column(JSON, default=list)
    flags: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


# --- Orchestration (activity I) ----------------------------------------------

class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    definition: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), index=True)
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120))
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    steps: Mapped[list["WorkflowStep"]] = relationship(back_populates="run", order_by="WorkflowStep.id")


class WorkflowStep(Base):
    __tablename__ = "workflow_steps"
    __table_args__ = (UniqueConstraint("run_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(10))  # task | approval
    handler: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    compensate_handler: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    depends_on: Mapped[list] = mapped_column(JSON, default=list)
    approver_roles: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    completion_seq: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    compensated: Mapped[bool] = mapped_column(Boolean, default=False)
    run: Mapped[WorkflowRun] = relationship(back_populates="steps")


# --- Vendor layer (activities L, T) ------------------------------------------

class Vendor(Base):
    __tablename__ = "vendors"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    capability: Mapped[str] = mapped_column(String(60))
    mode: Mapped[str] = mapped_column(String(20))  # api | file_handoff
    endpoint: Mapped[str] = mapped_column(String(300), default="")
    handoff_format: Mapped[str] = mapped_column(String(60), default="")
    license_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    data_terms_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="candidate")
    scorecard: Mapped[dict] = mapped_column(JSON, default=dict)
    atlas_vendor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("atlas_vendors.id"), nullable=True, index=True)
    connector_id: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


# --- Vendor atlas (candidate discovery database) -------------------------------

class AtlasImport(Base):
    __tablename__ = "atlas_imports"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    filename: Mapped[str] = mapped_column(String(300))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    source_label: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(20))  # applied | rejected
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    imported_by: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AtlasVendor(Base):
    """One atlas row. Source columns are replaced on re-import; outreach state is ATHENA-managed and kept."""
    __tablename__ = "atlas_vendors"
    __table_args__ = (UniqueConstraint("tenant_id", "atlas_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    atlas_id: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(300), index=True)
    parent_entity: Mapped[str] = mapped_column(String(300), default="")
    lifecycle_segment: Mapped[str] = mapped_column(String(120), index=True)
    nodes: Mapped[list] = mapped_column(JSON, default=list)
    selection: Mapped[str] = mapped_column(String(20), index=True)  # Research | Pilot
    research_category: Mapped[str] = mapped_column(String(80), default="")
    ai_evidence: Mapped[str] = mapped_column(String(200), default="")
    price_evidence: Mapped[str] = mapped_column(String(80), default="")
    source: Mapped[dict] = mapped_column(JSON, default=dict)  # all atlas columns, verbatim
    content_hash: Mapped[str] = mapped_column(String(64))
    source_row: Mapped[int] = mapped_column(Integer)
    import_id: Mapped[int] = mapped_column(ForeignKey("atlas_imports.id"))
    in_latest_import: Mapped[bool] = mapped_column(Boolean, default=True)
    outreach_status: Mapped[str] = mapped_column(String(20), default="Uncontacted")
    outreach_notes: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class VendorConnector(Base):
    """Reference connector (Vendor Connector Registry) plus this tenant's activation state."""
    __tablename__ = "vendor_connectors"
    __table_args__ = (UniqueConstraint("tenant_id", "connector_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    connector_id: Mapped[str] = mapped_column(String(16))
    reference: Mapped[dict] = mapped_column(JSON)  # the registry row + crosswalk, verbatim
    state: Mapped[str] = mapped_column(String(24), default="reference")  # reference|entitlement_verified|active|suspended
    entitlement_evidence: Mapped[list] = mapped_column(JSON, default=list)
    webhook_secret: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    changed_by: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class DueDiligence(Base):
    __tablename__ = "vendor_due_diligence"
    __table_args__ = (UniqueConstraint("tenant_id", "atlas_vendor_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    atlas_vendor_id: Mapped[int] = mapped_column(ForeignKey("atlas_vendors.id"))
    assessments: Mapped[dict] = mapped_column(JSON, default=dict)  # criterion key -> assessment
    decision: Mapped[str] = mapped_column(String(20), default="open")  # open | qualified | rejected
    decided_by: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    decision_notes: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class VendorJob(Base):
    __tablename__ = "vendor_jobs"
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    connector_id: Mapped[str] = mapped_column(String(16), index=True)
    vendor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("vendors.id"), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(24), index=True)
    work_package: Mapped[dict] = mapped_column(JSON)
    input_revision_ids: Mapped[list] = mapped_column(JSON, default=list)
    budget_reserved: Mapped[str] = mapped_column(String(40), default="0")
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    dispatch: Mapped[dict] = mapped_column(JSON, default=dict)
    events: Mapped[list] = mapped_column(JSON, default=list)       # received callback event ids
    charges: Mapped[list] = mapped_column(JSON, default=list)
    exceptions: Mapped[list] = mapped_column(JSON, default=list)
    result_files: Mapped[list] = mapped_column(JSON, default=list)
    result_revision_id: Mapped[Optional[int]] = mapped_column(ForeignKey("artifact_revisions.id"), nullable=True)
    blocked_reason: Mapped[str] = mapped_column(Text, default="")
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class WebhookNonce(Base):
    __tablename__ = "webhook_nonces"
    __table_args__ = (UniqueConstraint("tenant_id", "connector_id", "nonce"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    connector_id: Mapped[str] = mapped_column(String(16))
    nonce: Mapped[str] = mapped_column(String(100))
    received_at: Mapped[datetime] = mapped_column(default=utcnow)


# --- N6 commercial bids -------------------------------------------------------

class Bid(Base):
    __tablename__ = "bids"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    rfq_ref: Mapped[str] = mapped_column(String(200), index=True)
    bidder: Mapped[str] = mapped_column(String(200))
    scope: Mapped[str] = mapped_column(String(20))  # single_node | bundle | full_chain
    nodes: Mapped[list] = mapped_column(JSON, default=list)
    amount: Mapped[str] = mapped_column(String(40))  # Decimal as string: exact money
    currency: Mapped[str] = mapped_column(String(3))
    lead_time_days: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="submitted")
    submitted_by: Mapped[str] = mapped_column(String(120))
    decided_by: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    decided_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


# --- N8 lifecycle telemetry ----------------------------------------------------

class TelemetryEvent(Base):
    __tablename__ = "telemetry_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(20))
    serial: Mapped[str] = mapped_column(String(120))
    lot: Mapped[str] = mapped_column(String(120), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    is_synthetic: Mapped[bool] = mapped_column(Boolean)
    source: Mapped[str] = mapped_column(String(40))
    occurred_at: Mapped[datetime] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


def _forbid_mutation(mapper, connection, target):
    raise ImmutableRowError(f"{type(target).__name__} rows are append-only")


for _model in (ArtifactRevision, AuditEvent, RevisionLink):
    event.listen(_model, "before_update", _forbid_mutation)
    event.listen(_model, "before_delete", _forbid_mutation)
