"""SQLAlchemy 2.0 ORM models — the PLM system of record.

Shape notes that matter:

* A BOM line hangs off a *parent revision*, not a parent part. This is the whole
  point of a PLM: rev B of the neckband may consume a different cell from rev A,
  and both must remain readable. Attaching lines to the part would overwrite
  history on every change.
* ``child_revision_id`` is nullable. NULL means "floating" — the line consumes
  whatever the child's current released revision is. A pinned revision is used
  where a change must not propagate silently, which for a safety-critical
  isolation PCBA is the correct default.
* Every table that a gate reads carries enough provenance (source system,
  ingested_at, checksum) to answer "where did this come from" during an audit,
  because a gate decision nobody can reconstruct is not a governed gate.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey,
                        Index, Integer, Numeric, String, Text, UniqueConstraint,
                        func)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from .db import Base
from .enums import (ActionPriority, ArtifactStatus, ArtifactType, CapaSource,
                    CapaState, CertificateStatus,
                    CertificationScheme, ChangeType, ECNCategory, ECNState,
                    EffectivityType, EvaluationStatus, FmeaType, GateCode, GateState,
                    ImpactObjectType, ImpactState, LifecycleState, PartType, ReratingState,
                    ReviewState, SupplyMode)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# JSON works on both PostgreSQL and SQLite. On PG this maps to JSON; switch to
# JSONB in a migration if you need to index into these documents.
JSONDict = JSON


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=utcnow, nullable=False)


# =====================================================================
# People and access
# =====================================================================
class User(Base, TimestampMixin):
    __tablename__ = "plm_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(128))
    email: Mapped[str | None] = mapped_column(String(200))
    # A list of Role values. Kept as JSON rather than a join table because the
    # set is small, read on every request, and never queried by role.
    roles: Mapped[list[str]] = mapped_column(JSONDict, default=list)
    department: Mapped[str | None] = mapped_column(String(80))
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    def has(self, *roles: str) -> bool:
        return "admin" in self.roles or any(r in self.roles for r in roles)


# =====================================================================
# Parts, revisions and the BOM graph
# =====================================================================
class Part(Base, TimestampMixin):
    __tablename__ = "plm_part"
    __table_args__ = (
        Index("ix_part_type_sku", "part_type", "is_sellable"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    part_number: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    part_type: Mapped[str] = mapped_column(String(24), default=PartType.COMPONENT)
    supply_mode: Mapped[str | None] = mapped_column(String(24))
    supplier: Mapped[str | None] = mapped_column(String(160))
    sourcing_status: Mapped[str | None] = mapped_column(String(40))  # Primary / Alternate
    uom: Mapped[str] = mapped_column(String(12), default="EA")
    is_sellable: Mapped[bool] = mapped_column(Boolean, default=False)
    # Lifecycle risk carried from the component engineering work: NRND parts are
    # the trigger for a supply-continuity ECN, so the flag lives on the part.
    lifecycle_risk: Mapped[str | None] = mapped_column(String(24))   # ACTIVE / NRND / EOL
    safety_critical: Mapped[bool] = mapped_column(Boolean, default=False)

    revisions: Mapped[list["PartRevision"]] = relationship(
        back_populates="part", cascade="all, delete-orphan",
        foreign_keys="PartRevision.part_id", order_by="PartRevision.id")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Part {self.part_number}>"


class PartRevision(Base, TimestampMixin):
    __tablename__ = "plm_part_revision"
    __table_args__ = (
        UniqueConstraint("part_id", "rev", name="uq_part_rev"),
        CheckConstraint("unit_cost_usd >= 0", name="ck_rev_cost_non_negative"),
        Index("ix_rev_state", "lifecycle_state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("plm_part.id", ondelete="CASCADE"),
                                         index=True)
    rev: Mapped[str] = mapped_column(String(8))
    lifecycle_state: Mapped[str] = mapped_column(String(16), default=LifecycleState.WIP)
    unit_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    change_summary: Mapped[str | None] = mapped_column(Text)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_by: Mapped[str | None] = mapped_column(String(64))
    supersedes_id: Mapped[int | None] = mapped_column(
        ForeignKey("plm_part_revision.id", ondelete="SET NULL"))
    # provenance — which vault, which commit, which file hash
    source_system: Mapped[str | None] = mapped_column(String(64))
    source_ref: Mapped[str | None] = mapped_column(String(200))

    part: Mapped[Part] = relationship(back_populates="revisions", foreign_keys=[part_id])
    bom_lines: Mapped[list["BomLine"]] = relationship(
        back_populates="parent_revision", cascade="all, delete-orphan",
        foreign_keys="BomLine.parent_revision_id",
        order_by="BomLine.find_number")

    @property
    def is_released(self) -> bool:
        return self.lifecycle_state == LifecycleState.RELEASED

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Rev {self.part_id}/{self.rev} {self.lifecycle_state}>"


class BomLine(Base, TimestampMixin):
    __tablename__ = "plm_bom_line"
    __table_args__ = (
        UniqueConstraint("parent_revision_id", "find_number", name="uq_bom_find_number"),
        CheckConstraint("quantity > 0", name="ck_bom_qty_positive"),
        Index("ix_bom_child", "child_part_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_revision_id: Mapped[int] = mapped_column(
        ForeignKey("plm_part_revision.id", ondelete="CASCADE"), index=True)
    child_part_id: Mapped[int] = mapped_column(
        ForeignKey("plm_part.id", ondelete="RESTRICT"))
    # NULL = float to the child's current released revision.
    child_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("plm_part_revision.id", ondelete="RESTRICT"))
    find_number: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("1"))
    reference_designator: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)

    parent_revision: Mapped[PartRevision] = relationship(
        back_populates="bom_lines", foreign_keys=[parent_revision_id])
    child_part: Mapped[Part] = relationship(foreign_keys=[child_part_id])


# =====================================================================
# Artifacts and certificates — the evidence a gate reads
# =====================================================================
class Artifact(Base, TimestampMixin):
    __tablename__ = "plm_artifact"
    __table_args__ = (
        UniqueConstraint("name", "revision", name="uq_artifact_name_rev"),
        Index("ix_artifact_lookup", "artifact_type", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    artifact_type: Mapped[str] = mapped_column(String(32), index=True)
    revision: Mapped[str] = mapped_column(String(16), default="1")
    status: Mapped[str] = mapped_column(String(16), default=ArtifactStatus.DRAFT)
    # An artifact may belong to a specific part, to a SKU as a whole, or to both.
    part_id: Mapped[int | None] = mapped_column(ForeignKey("plm_part.id", ondelete="SET NULL"))
    sku: Mapped[str | None] = mapped_column(String(32), index=True)
    gate_code: Mapped[str | None] = mapped_column(String(24))
    uri: Mapped[str | None] = mapped_column(String(400))
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    file_format: Mapped[str | None] = mapped_column(String(32))   # STEP AP242, RS-274X, PDF
    source_system: Mapped[str | None] = mapped_column(String(64))
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A CAD master model is only a *parametric* master if the vault says it is;
    # a dumb STEP export is a different thing and must not satisfy the rule.
    is_parametric: Mapped[bool] = mapped_column(Boolean, default=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)

    part: Mapped[Part | None] = relationship()


class Certificate(Base, TimestampMixin):
    __tablename__ = "plm_certificate"
    __table_args__ = (
        UniqueConstraint("scheme", "sku", "certificate_number", name="uq_cert_scheme_sku"),
        Index("ix_cert_lookup", "sku", "scheme", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scheme: Mapped[str] = mapped_column(String(24), index=True)
    sku: Mapped[str] = mapped_column(String(32), index=True)
    part_id: Mapped[int | None] = mapped_column(ForeignKey("plm_part.id", ondelete="SET NULL"))
    certificate_number: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(16), default=CertificateStatus.NOT_STARTED)
    holder: Mapped[str | None] = mapped_column(String(160))
    lab: Mapped[str | None] = mapped_column(String(160))
    issued_on: Mapped[date | None] = mapped_column(Date)
    expires_on: Mapped[date | None] = mapped_column(Date)
    scope: Mapped[str | None] = mapped_column(Text)
    vault_ref: Mapped[str | None] = mapped_column(String(200))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_source: Mapped[str | None] = mapped_column(String(32))  # live / replay


# =====================================================================
# Risk and control records
# =====================================================================
class FmeaRow(Base, TimestampMixin):
    __tablename__ = "plm_fmea_row"
    __table_args__ = (
        UniqueConstraint("fmea_type", "row_ref", name="uq_fmea_row_ref"),
        CheckConstraint("severity BETWEEN 1 AND 10", name="ck_fmea_severity_range"),
        CheckConstraint("occurrence BETWEEN 1 AND 10", name="ck_fmea_occurrence_range"),
        CheckConstraint("detection BETWEEN 1 AND 10", name="ck_fmea_detection_range"),
        Index("ix_fmea_ap", "sku", "action_priority", "review_state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    fmea_type: Mapped[str] = mapped_column(String(8), index=True)
    row_ref: Mapped[str] = mapped_column(String(16))            # D-01, P-05
    sku: Mapped[str] = mapped_column(String(32), index=True)
    item_function: Mapped[str] = mapped_column(String(200))
    failure_mode: Mapped[str] = mapped_column(Text)
    effect: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[int] = mapped_column(Integer)
    occurrence: Mapped[int] = mapped_column(Integer)
    detection: Mapped[int] = mapped_column(Integer)
    action_priority: Mapped[str] = mapped_column(String(8), index=True)
    prevention_control: Mapped[str | None] = mapped_column(Text)
    detection_control: Mapped[str | None] = mapped_column(Text)
    recommended_action: Mapped[str | None] = mapped_column(Text)
    # The distinction the gate rule turns on: a *function* is not an owner.
    # AIAG-VDA requires a named individual and a target date for every action.
    responsibility_function: Mapped[str | None] = mapped_column(String(80))
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("plm_user.id", ondelete="SET NULL"))
    target_gate: Mapped[str | None] = mapped_column(String(24))
    target_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(24), default="Open")
    review_state: Mapped[str] = mapped_column(String(28), default=ReviewState.CURRENT)
    linked_part_id: Mapped[int | None] = mapped_column(
        ForeignKey("plm_part.id", ondelete="SET NULL"), index=True)

    owner: Mapped[User | None] = relationship()
    linked_part: Mapped[Part | None] = relationship()

    @property
    def rpn(self) -> int:
        return self.severity * self.occurrence * self.detection


class ControlPlanItem(Base, TimestampMixin):
    __tablename__ = "plm_control_plan_item"
    __table_args__ = (
        UniqueConstraint("control_ref", name="uq_control_ref"),
        Index("ix_control_review", "sku", "review_state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    control_ref: Mapped[str] = mapped_column(String(24))
    sku: Mapped[str] = mapped_column(String(32), index=True)
    station: Mapped[str | None] = mapped_column(String(80))
    characteristic: Mapped[str] = mapped_column(String(200))
    specification: Mapped[str | None] = mapped_column(String(200))
    method: Mapped[str | None] = mapped_column(String(200))
    sample_plan: Mapped[str | None] = mapped_column(String(120))
    reaction_plan: Mapped[str | None] = mapped_column(Text)
    classification: Mapped[str | None] = mapped_column(String(24))   # Critical/Major/Minor
    linked_fmea_ref: Mapped[str | None] = mapped_column(String(16))
    linked_part_id: Mapped[int | None] = mapped_column(
        ForeignKey("plm_part.id", ondelete="SET NULL"), index=True)
    review_state: Mapped[str] = mapped_column(String(28), default=ReviewState.CURRENT)


class DesignParameter(Base, TimestampMixin):
    """A verifiable design characteristic a gate can test numerically.

    ``is_placeholder`` exists because the source design package explicitly
    carries provisional creepage, clearance and Hi-Pot values. A service that
    treats a placeholder as a measurement launders an assumption into evidence,
    so the flag is first-class and the gate rules read it.
    """
    __tablename__ = "plm_design_parameter"
    __table_args__ = (
        UniqueConstraint("code", name="uq_design_parameter_code"),
        Index("ix_param_sku", "sku", "verified"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(48))
    sku: Mapped[str] = mapped_column(String(32), index=True)
    part_id: Mapped[int | None] = mapped_column(ForeignKey("plm_part.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(200))
    measured_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    unit: Mapped[str | None] = mapped_column(String(16))
    min_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    max_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    standard_ref: Mapped[str | None] = mapped_column(String(120))
    method: Mapped[str | None] = mapped_column(String(200))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_by: Mapped[str | None] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_artifact_id: Mapped[int | None] = mapped_column(
        ForeignKey("plm_artifact.id", ondelete="SET NULL"))
    is_placeholder: Mapped[bool] = mapped_column(Boolean, default=False)
    review_state: Mapped[str] = mapped_column(String(28), default=ReviewState.CURRENT)

    part: Mapped[Part | None] = relationship()


# =====================================================================
# Gates
# =====================================================================
class GateInstance(Base, TimestampMixin):
    __tablename__ = "plm_gate_instance"
    __table_args__ = (
        UniqueConstraint("sku", "gate_code", name="uq_gate_sku_code"),
        Index("ix_gate_state", "state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(32), index=True)
    gate_code: Mapped[str] = mapped_column(String(24), index=True)
    state: Mapped[str] = mapped_column(String(16), default=GateState.NOT_STARTED)
    planned_date: Mapped[date | None] = mapped_column(Date)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[str | None] = mapped_column(String(64))
    waiver_reason: Mapped[str | None] = mapped_column(Text)
    waived_by: Mapped[str | None] = mapped_column(String(64))

    evaluations: Mapped[list["GateEvaluation"]] = relationship(
        back_populates="gate", cascade="all, delete-orphan",
        order_by="GateEvaluation.id")


class GateEvaluation(Base):
    """An immutable record of one evaluation.

    The full response payload is stored verbatim. Six months later, when someone
    asks why a gate passed, the answer must be the exact evidence the engine saw
    at that moment — not a re-run against today's data.
    """
    __tablename__ = "plm_gate_evaluation"

    id: Mapped[int] = mapped_column(primary_key=True)
    gate_instance_id: Mapped[int] = mapped_column(
        ForeignKey("plm_gate_instance.id", ondelete="CASCADE"), index=True)
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False)
    evaluated_by: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    rules_evaluated: Mapped[int] = mapped_column(Integer, default=0)
    rules_failed: Mapped[int] = mapped_column(Integer, default=0)
    transition_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)

    gate: Mapped[GateInstance] = relationship(back_populates="evaluations")


# =====================================================================
# Engineering change
# =====================================================================
class ECN(Base, TimestampMixin):
    __tablename__ = "plm_ecn"
    __table_args__ = (
        UniqueConstraint("ecn_number", name="uq_ecn_number"),
        Index("ix_ecn_state", "state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ecn_number: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(24), default=ECNCategory.QUALITY)
    state: Mapped[str] = mapped_column(String(16), default=ECNState.DRAFT)
    primary_sku: Mapped[str | None] = mapped_column(String(32), index=True)
    raised_by: Mapped[str] = mapped_column(String(64))
    raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_reason: Mapped[str | None] = mapped_column(Text)
    effectivity_type: Mapped[str] = mapped_column(String(24), default=EffectivityType.IMMEDIATE)
    effectivity_date: Mapped[date | None] = mapped_column(Date)
    effectivity_serial: Mapped[str | None] = mapped_column(String(64))
    stock_disposition: Mapped[str | None] = mapped_column(Text)
    target_gate: Mapped[str | None] = mapped_column(String(24))
    # Set when the impact analysis runs, so a caller can tell "no impacts found"
    # from "impact analysis never ran" — they are very different states.
    impact_analysed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    affected_items: Mapped[list["ECNAffectedItem"]] = relationship(
        back_populates="ecn", cascade="all, delete-orphan")
    impacts: Mapped[list["ECNImpact"]] = relationship(
        back_populates="ecn", cascade="all, delete-orphan")


class ECNAffectedItem(Base):
    __tablename__ = "plm_ecn_affected_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    ecn_id: Mapped[int] = mapped_column(ForeignKey("plm_ecn.id", ondelete="CASCADE"),
                                        index=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("plm_part.id", ondelete="RESTRICT"))
    change_type: Mapped[str] = mapped_column(String(24), default=ChangeType.SPEC_CHANGE)
    from_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("plm_part_revision.id", ondelete="SET NULL"))
    to_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("plm_part_revision.id", ondelete="SET NULL"))
    replacement_part_id: Mapped[int | None] = mapped_column(
        ForeignKey("plm_part.id", ondelete="SET NULL"))
    detail: Mapped[str | None] = mapped_column(Text)

    ecn: Mapped[ECN] = relationship(back_populates="affected_items")
    part: Mapped[Part] = relationship(foreign_keys=[part_id])


class ECNImpact(Base):
    """One record flagged for re-review by an ECN.

    ``where_used_path`` records *why* this row was pulled in — the BOM path from
    the changed part up to the SKU. Without it an engineer confronted with 40
    flagged rows has no way to judge which ones genuinely matter.
    """
    __tablename__ = "plm_ecn_impact"
    __table_args__ = (
        UniqueConstraint("ecn_id", "object_type", "object_ref", name="uq_impact_object"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ecn_id: Mapped[int] = mapped_column(ForeignKey("plm_ecn.id", ondelete="CASCADE"),
                                        index=True)
    object_type: Mapped[str] = mapped_column(String(24))
    object_ref: Mapped[str] = mapped_column(String(64))
    object_title: Mapped[str | None] = mapped_column(String(300))
    sku: Mapped[str | None] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(28), default=ImpactState.PENDING_RE_REVIEW)
    reason: Mapped[str | None] = mapped_column(Text)
    where_used_path: Mapped[str | None] = mapped_column(String(400))
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)

    ecn: Mapped[ECN] = relationship(back_populates="impacts")


# =====================================================================
# Audit
# =====================================================================
class Capa(Base, TimestampMixin):
    """A Corrective and Preventive Action record.

    Opened by evidence, closed by verification. Node 6 opens these automatically
    when a field-failure cluster crosses its action threshold, which is why the
    ``proposed_occurrence`` columns exist and why they are separate from the
    FMEA row's actual ``occurrence``.

    The separation is the whole point. Occurrence feeds Action Priority, and
    Action Priority is what the G2 gate rule blocks on. If an automated field
    service could write occurrence directly it could raise or lower an AP — and
    lowering one unblocks a gate — with nobody having decided anything. So the
    CAPA carries the *proposal* and a named quality engineer applies it through
    a separate, role-gated endpoint that records who did it.
    """
    __tablename__ = "plm_capa"
    __table_args__ = (
        UniqueConstraint("capa_number", name="uq_capa_number"),
        CheckConstraint("proposed_occurrence IS NULL OR "
                        "(proposed_occurrence BETWEEN 1 AND 10)",
                        name="ck_capa_proposed_occurrence_range"),
        Index("ix_capa_state", "state", "sku"),
        Index("ix_capa_source", "source", "source_ref"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    capa_number: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(28), default=CapaState.OPEN)
    source: Mapped[str] = mapped_column(String(28), default=CapaSource.INTERNAL)
    # The upstream evidence, e.g. a Node 6 cluster id. Unique per source so a
    # retried call cannot open a second CAPA for the same cluster.
    source_ref: Mapped[str | None] = mapped_column(String(64), index=True)
    sku: Mapped[str | None] = mapped_column(String(32), index=True)
    component_lot: Mapped[str | None] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="MEDIUM")

    raised_by: Mapped[str] = mapped_column(String(64))
    raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    assigned_to: Mapped[str | None] = mapped_column(String(64))
    due_date: Mapped[date | None] = mapped_column(Date)
    closed_by: Mapped[str | None] = mapped_column(String(64))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closure_note: Mapped[str | None] = mapped_column(Text)

    # ---- the field evidence, kept verbatim ----
    affected_volume_pct: Mapped[float | None] = mapped_column(Float)
    affected_unit_count: Mapped[int | None] = mapped_column(Integer)
    population_count: Mapped[int | None] = mapped_column(Integer)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)

    # ---- the proposed FMEA re-rating (proposed, never applied here) ----
    linked_fmea_row_id: Mapped[int | None] = mapped_column(
        ForeignKey("plm_fmea_row.id", ondelete="SET NULL"), index=True)
    proposed_occurrence: Mapped[int | None] = mapped_column(Integer)
    occurrence_rationale: Mapped[str | None] = mapped_column(Text)
    rerating_state: Mapped[str] = mapped_column(String(16), default=ReratingState.NONE)
    rerating_applied_by: Mapped[str | None] = mapped_column(String(64))
    rerating_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    occurrence_before: Mapped[int | None] = mapped_column(Integer)
    action_priority_before: Mapped[str | None] = mapped_column(String(8))
    action_priority_after: Mapped[str | None] = mapped_column(String(8))

    linked_fmea_row: Mapped["FmeaRow | None"] = relationship()


class AuditEvent(Base):
    """Append-only. Gate decisions and change approvals are regulated records."""
    __tablename__ = "plm_audit_event"
    __table_args__ = (Index("ix_audit_object", "object_type", "object_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                         nullable=False, index=True)
    actor: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    object_type: Mapped[str] = mapped_column(String(32))
    object_ref: Mapped[str] = mapped_column(String(80))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    request_id: Mapped[str | None] = mapped_column(String(32))


__all__ = [
    "Artifact", "AuditEvent", "BomLine", "Certificate", "ControlPlanItem",
    "Capa", "DesignParameter", "ECN", "ECNAffectedItem", "ECNImpact", "FmeaRow",
    "GateEvaluation", "GateInstance", "Part", "PartRevision", "User", "utcnow",
]
