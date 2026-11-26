"""N8 intake of N4 verification releases (genealogy + monitors) and its append-only tables."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, Integer, String, event, select
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import DDL

from ..db import Base, JSONType, append_only_ddl
from ..errors import N0Error
from ..models import utcnow
from ..repository import Repository, dump, new_id
from ..rules import sha256_hex
from ..security import Principal


class N8IntakeRow(Base):
    __tablename__ = "n8_intakes"
    intake_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_ref: Mapped[str] = mapped_column(String(160), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class N8UnitRow(Base):
    __tablename__ = "n8_units"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    unit_serial: Mapped[str] = mapped_column(String(128), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class N8MonitorRow(Base):
    __tablename__ = "n8_monitors"
    monitor_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


N8_APPEND_ONLY_TABLES = ("n8_intakes", "n8_units", "n8_monitors")
for _name in N8_APPEND_ONLY_TABLES:
    for _dialect in ("postgresql", "sqlite"):
        for _stmt in append_only_ddl(_name, _dialect):
            event.listen(Base.metadata.tables[_name], "after_create", DDL(_stmt).execute_if(dialect=_dialect))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class N8IntakeRequest(_Strict):
    artifact_id: str
    revision: str


class N8Monitor(_Strict):
    model_config = ConfigDict(extra="forbid", title="N8Monitor")
    monitor_id: str
    tenant_id: str
    characteristic: str
    unit: str
    lower_limit: Optional[str] = None
    upper_limit: Optional[str] = None
    guard_band: Optional[str] = Field(default=None, description="expanded uncertainty used at verification")
    source_test_id: str
    design_revision: str
    verification_artifact: str
    reaction_plan: Optional[str] = None


class N8UnitGenealogy(_Strict):
    model_config = ConfigDict(extra="forbid", title="N8UnitGenealogy")
    unit_serial: str
    tenant_id: str
    design_revision: str
    pcb_revision: Optional[str] = None
    design_refs: list[str]
    prd_ref: str
    verification_artifact: str
    verification_evidence_sha256: str
    tests_passed: list[str]
    status: Literal["VERIFIED_FOR_LIFECYCLE"] = "VERIFIED_FOR_LIFECYCLE"
    registered_at: datetime


class N8Intake(_Strict):
    model_config = ConfigDict(extra="forbid", title="N8Intake")
    intake_id: str
    tenant_id: str
    artifact_ref: str
    manifest_id: str
    payload_sha256: str
    design_revision: str
    units: list[str]
    monitors: list[str]
    accepted_by: str
    accepted_at: datetime


class N8Service:
    def __init__(self, state) -> None:
        self.db, self.store, self.clock = state.db, state.store, state.clock
        self.n4 = state.n4

    def ingest(self, p: Principal, req: N8IntakeRequest) -> N8Intake:
        t = p.tenant_id
        with self.db.session() as s:
            repo = Repository(s)
            row = repo.get_artifact(t, req.artifact_id, req.revision)
            if row is None or row.artifact_type != "VERIFICATION_RELEASE":
                raise N0Error(404, "NOT_A_VERIFICATION_RELEASE", f"{req.artifact_id}@{req.revision} is not an N0 "
                                                                 "VERIFICATION_RELEASE")
            top = repo.latest_revision(t, row.artifact_id)
            if top is not None and top.revision_order > row.revision_order:
                raise N0Error(409, "SUPERSEDED_REVISION", f"{row.artifact_id}@{row.revision} is superseded")
            m = next((m for m in reversed(repo.releases(t, row.artifact_id, row.revision))
                      if m.status == "RELEASED" and "N8" in m.target_nodes), None)
            if m is None:
                raise N0Error(409, "NOT_RELEASED_TO_N8", "only an N0 manifest RELEASED to N8 may enter N8")
            blob = repo.find_blob(t, row.payload_sha256, "PAYLOAD")
            if blob is None or sha256_hex(self.store.get(blob.bucket, blob.object_key)) != m.payload_sha256:
                raise N0Error(409, "PAYLOAD_HASH_MISMATCH", "stored payload bytes do not match the released SHA-256")
            p_ = row.canonical["payload"]
            if p_.get("verification_status") != "VERIFIED" or p_.get("target") != "N8":
                raise N0Error(409, "NOT_VERIFIED", "the package is not a VERIFIED N8 verification release")
            if row.synthetic:
                raise N0Error(409, "SYNTHETIC_NOT_ACCEPTED", "synthetic packages never enter N8 genealogy")
            if any(x.get("provenance") in ("SYNTHETIC", "MOCK_FIXTURE") for x in p_.get("measurements", [])):
                raise N0Error(409, "SYNTHETIC_NOT_ACCEPTED", "the package contains non-physical measurements")
            ref = f"{row.artifact_id}@{row.revision}"
            if any(r.artifact_ref == ref for r in s.scalars(select(N8IntakeRow).where(N8IntakeRow.tenant_id == t))):
                raise N0Error(409, "ALREADY_INGESTED", f"{ref} is already in N8")
        status = self.n4.release_status_by_payload(t, p_["release_id"])
        if status in (None, "INVALIDATED", "REFUSED"):
            raise N0Error(409, "N4_RELEASE_NOT_ACTIVE", f"the N4 release behind {ref} is {status or 'unknown'}")
        now = self.clock()
        cp = {i.get("test_id"): i for i in p_.get("control_plan") or []}
        monitors: dict[tuple, N8Monitor] = {}
        for x in p_["measurements"]:
            if x.get("method") == "SIM":
                continue
            key = (x["characteristic"], x["unit"])
            monitors.setdefault(key, N8Monitor(
                monitor_id=new_id("N8MON"), tenant_id=t, characteristic=x["characteristic"], unit=x["unit"],
                lower_limit=None if x.get("lower_limit") is None else str(x["lower_limit"]),
                upper_limit=None if x.get("upper_limit") is None else str(x["upper_limit"]),
                guard_band=None if x.get("expanded_uncertainty") is None else str(x["expanded_uncertainty"]),
                source_test_id=x["test_id"], design_revision=p_["design_revision"], verification_artifact=ref,
                reaction_plan=(cp.get(x["test_id"]) or {}).get("reaction_plan")))
        units = []
        with self.db.session() as s:
            for serial in p_.get("unit_serials") or []:
                passed = sorted({x["test_id"] for x in p_["measurements"] if x.get("unit_serial") == serial}
                                | {a["test_id"] for a in p_.get("attribute_inspections") or []
                                   if a.get("unit_serial") == serial})
                g = N8UnitGenealogy(unit_serial=serial, tenant_id=t, design_revision=p_["design_revision"],
                                    pcb_revision=p_.get("pcb_revision"), design_refs=p_["design_refs"],
                                    prd_ref=p_["prd_ref"], verification_artifact=ref,
                                    verification_evidence_sha256=p_["evidence_sha256"], tests_passed=passed,
                                    registered_at=now)
                n = len(list(s.scalars(select(N8UnitRow).where(N8UnitRow.tenant_id == t,
                                                               N8UnitRow.unit_serial == serial))))
                s.add(N8UnitRow(tenant_id=t, unit_serial=serial, seq=n + 1, record=dump(g)))
                Repository(s).add_edge(t, f"artifact:{ref}", f"unit:{serial}", "GENEALOGY",
                                       design_revision=p_["design_revision"])
                units.append(serial)
            for mon in monitors.values():
                s.add(N8MonitorRow(monitor_id=mon.monitor_id, tenant_id=t, record=dump(mon)))
            rec = N8Intake(intake_id=new_id("N8IN"), tenant_id=t, artifact_ref=ref, manifest_id=m.manifest_id,
                           payload_sha256=m.payload_sha256, design_revision=p_["design_revision"], units=units,
                           monitors=[x.monitor_id for x in monitors.values()], accepted_by=p.principal_id,
                           accepted_at=now)
            s.add(N8IntakeRow(intake_id=rec.intake_id, tenant_id=t, artifact_ref=ref, record=dump(rec)))
            Repository(s).append_audit(tenant_id=t, event_type="N8_INTAKE", actor_id=p.principal_id,
                                       actor_roles=p.roles, subject=f"artifact:{ref}",
                                       details={"units": units, "monitors": len(monitors)})
        return rec

    def unit(self, tenant: str, serial: str) -> dict[str, Any]:
        with self.db.session() as s:
            rows = list(s.scalars(select(N8UnitRow).where(N8UnitRow.tenant_id == tenant,
                                                          N8UnitRow.unit_serial == serial)
                                  .order_by(N8UnitRow.seq)))
        if not rows:
            raise N0Error(404, "UNIT_NOT_FOUND", f"{serial} has no N8 genealogy")
        return {"unit_serial": serial, "history": [r.record for r in rows]}

    def monitors(self, tenant: str) -> list[dict]:
        with self.db.session() as s:
            return [r.record for r in s.scalars(select(N8MonitorRow).where(N8MonitorRow.tenant_id == tenant)
                                                .order_by(N8MonitorRow.created_at))]


N8_SCHEMAS = {"n8_unit_genealogy": N8UnitGenealogy, "n8_monitor": N8Monitor, "n8_intake": N8Intake}
