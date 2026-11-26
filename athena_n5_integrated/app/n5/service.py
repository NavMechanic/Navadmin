"""N5 service: the six-stage sourcing pipeline on the shared N0 database, object store and audit chain.

N5.1 lock_intake      the N3 BOM_RELEASE released to N5 for the newest PRD revision, verified against N0
                      (decision, manifest, stored payload hash, approval chain, rights), normalised to
                      exact-MPN lines with Q_required = qty per unit × build quantity.
N5.2 suppliers        manufacturer / distributor / EMS / ODM records: identity, Open Supply Hub sites,
                      authorized-channel proofs, signing keys; a separate supplier_quality reviewer
                      attests and approves (never the submitter).
N5.3 quotes           manual, signed EMS drop zone and on-demand distributor APIs (``quotes.py``).
N5.4–N5.5 report      deterministic guardrails, then Decimal costing and ranking of eligible offers only.
N5.6 review / handoff a separate procurement_approver approves the exact report hash and quote hashes;
                      N0-checked SOURCING_HANDOFF drafts go to N6 (commercial) and N7 (finance).

Cascades: a new N3 BOM revision or N2 PRD revision supersedes the intake and marks its quotes and
reports STALE and its handoffs BLOCKED; an N0 rights revocation blocks every handoff that used the
source. Downstream N6 / N7 receivers subscribe to ``handoff_listeners`` and re-check lazily.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from sqlalchemy import select

from ..db import ApprovalRow
from ..errors import N0Error
from ..governance import record_approval, release_handoff
from ..models import (EXCLUDED_COST_CATEGORIES, INDICATIVE_MATERIAL_LABEL, ApprovalRequest, Artifact,
                      EvaluateRequest, HandoffReleaseRequest)
from ..n3.db import N3EventRow, N3ReleaseRow
from ..n3.models import N3Release
from ..repository import Repository, chain_hash, dump, new_id
from ..rules import Findings as N0Findings
from ..rules import canonical_json, evaluate_rights, normalize_mpn, revision_order, sha256_hex
from ..security import Principal
from . import APPROVAL_SCOPE, N5_VERSION, engine
from .config import N5Settings
from .connectors import CLASSES, RateLimiter, TTLCache, make_connectors
from .db import N5EcoRow, N5EventRow, N5FxRow, N5HandoffRow, N5IntakeRow, N5QuoteRow, N5ReportRow, N5ReviewRow, \
    N5SupplierRow
from .models import (BomIntakeRequest, EcoProposalRequest, FxAssumptionRequest, HandoffRequest, N5BomIntake,
                     N5BomLine, N5EcoProposal, N5FxAssumption, N5Handoff, N5HandoffPackage, N5Quote, N5Review,
                     N5ReviewRequest, N5SourcingReport, N5Supplier, SourcingReportRequest, SupplierRequest,
                     SupplierReviewRequest)
from .quotes import QuoteIntakeMixin

PURPOSE = {"N6": "COMMERCIAL", "N7": "FINANCE"}


def _with_hash(model, field: str, skip: tuple[str, ...] = ()):
    body = {k: v for k, v in dump(model).items() if k != field and k not in skip}
    return model.model_copy(update={field: sha256_hex(canonical_json(body))})


def _next_revision(top_revision: Optional[str], top_sha: Optional[str], sha: str) -> str:
    if top_revision is None:
        return "A"
    if top_sha == sha:
        return top_revision
    n, rev = revision_order(top_revision) + 1, ""
    while n:
        n, r = divmod(n - 1, 26)
        rev = chr(65 + r) + rev
    return rev


class N5Service(QuoteIntakeMixin):
    def __init__(self, state, settings: N5Settings, transport=None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.db, self.store, self.pipeline, self.clock = state.db, state.store, state.pipeline, state.clock
        self.scanner, self.n0_settings = state.scanner, state.settings
        self.n2, self.n3, self.n4 = state.n2, state.n3, getattr(state, "n4", None)
        self.settings = settings
        self.connectors = make_connectors(settings, transport)
        self.limiters = {n: RateLimiter(settings.rate_per_minute.get(n, 30), settings.daily_quota.get(n, 1000),
                                        monotonic) for n in CLASSES}
        self.cache = TTLCache(monotonic)
        self._line_calls: dict[tuple, list[float]] = {}
        self.handoff_listeners: list[Callable[[str, str, str, str], None]] = []
        state.source_rights_listeners.append(self.on_source_rights)
        self.n2.release_listeners.append(self.on_prd_release)
        self.n3.release_listeners.append(self.on_n3_release)

    # ==========================================================================================
    # helpers
    # ==========================================================================================
    @staticmethod
    def _svc(p: Principal) -> Principal:
        return Principal(principal_id=f"svc-n5@{p.tenant_id}", tenant_id=p.tenant_id, roles=["node_service"],
                         assigned_roles=["service"], nodes=["N5"], clearance=p.clearance, auth_method="internal")

    @staticmethod
    def _audit(s, p: Principal, event_type: str, subject: str, details: dict) -> None:
        Repository(s).append_audit(tenant_id=p.tenant_id, event_type=event_type, actor_id=p.principal_id,
                                   actor_roles=p.roles, subject=subject, details=details)

    @staticmethod
    def _sys_audit(s, tenant: str, event_type: str, subject: str, details: dict) -> None:
        Repository(s).append_audit(tenant_id=tenant, event_type=event_type, actor_id=f"svc-n5@{tenant}",
                                   actor_roles=["node_service"], subject=subject, details=details)

    def _event(self, s, tenant: str, kind: str, subject_id: str, status: str, actor: str, **details: Any) -> None:
        s.add(N5EventRow(event_id=new_id("N5E"), tenant_id=tenant, subject_kind=kind, subject_id=subject_id,
                         status=status, record={"status": status, "actor": actor, "at": self.clock().isoformat(),
                                                **details}))
        s.flush()

    @staticmethod
    def _events(s, tenant: str, kind: str, subject_id: str) -> list[dict]:
        rows = s.scalars(select(N5EventRow).where(N5EventRow.tenant_id == tenant, N5EventRow.subject_kind == kind,
                                                  N5EventRow.subject_id == subject_id)
                         .order_by(N5EventRow.created_at, N5EventRow.event_id))
        return [r.record for r in rows]

    def _flagged(self, s, tenant: str, kind: str, subject_id: str, statuses: tuple[str, ...]) -> Optional[dict]:
        return next((e for e in self._events(s, tenant, kind, subject_id) if e["status"] in statuses), None)

    @staticmethod
    def _get(s, row_cls, model, tenant: str, key, code: str):
        row = s.get(row_cls, key)
        if row is None or row.tenant_id != tenant:
            raise N0Error(404, code, f"{key} not found in this tenant")
        return model.model_validate(row.record)

    def _intake(self, s, tenant: str, intake_id: str) -> N5BomIntake:
        return self._get(s, N5IntakeRow, N5BomIntake, tenant, intake_id, "INTAKE_NOT_FOUND")

    def _store_bytes(self, repo: Repository, tenant: str, source_id: str, name: str, data: bytes, mime: str,
                     actor: str, scan: dict):
        from ..db import BlobRow
        digest = sha256_hex(data)
        existing = repo.find_blob(tenant, digest, "UPLOAD")
        if existing is not None:
            return existing
        key = self.store.key_for(tenant, digest)
        self.store.put_immutable("quarantine", key, data)
        row = repo.add_blob(BlobRow(blob_id=new_id("BLB"), tenant_id=tenant, source_id=source_id, kind="UPLOAD",
                                    sha256=digest, size_bytes=len(data), filename=name.rsplit("/", 1)[-1][:512],
                                    declared_mime=None, sniffed_mime=mime, status="SCAN_PASSED", scan=scan,
                                    bucket="quarantine", object_key=key, created_by=actor))
        repo.add_edge(tenant, f"source:{source_id}", f"blob:{row.blob_id}", "SUPPLIED", sha256=digest)
        return row

    def _source_status(self, tenant: str) -> Callable[[str], Optional[str]]:
        cache: dict[str, Optional[str]] = {}

        def status(sid: str) -> Optional[str]:
            if sid not in cache:
                with self.db.session() as s:
                    src = Repository(s).get_source(tenant, sid)
                cache[sid] = src.approval_status if src else None
            return cache[sid]
        return status

    # ==========================================================================================
    # N5.1 BOM ingestion & normalisation
    # ==========================================================================================
    def _verify_manifest(self, repo: Repository, row, manifest) -> list[str]:
        t, problems = row.tenant_id, []
        blob = repo.find_blob(t, row.payload_sha256, "PAYLOAD")
        if blob is None or sha256_hex(self.store.get(blob.bucket, blob.object_key)) != row.payload_sha256:
            problems.append(f"stored payload bytes of {row.artifact_id}@{row.revision} do not match their SHA-256")
        if manifest.payload_sha256 != row.payload_sha256:
            problems.append(f"manifest {manifest.manifest_id} payload hash differs from the artifact")
        arow = repo.s.get(ApprovalRow, manifest.approval_id) if manifest.approval_id else None
        if arow is None or arow.tenant_id != t:
            problems.append(f"approval {manifest.approval_id} not found")
        else:
            rec = dict(arow.record)
            stated = rec.pop("event_hash", None)
            if chain_hash(rec.get("prev_hash", ""), rec) != stated:
                problems.append(f"approval {manifest.approval_id} does not verify against the approval chain")
            if rec.get("decision") != "APPROVED" or rec.get("artifact_id") != row.artifact_id or \
                    rec.get("exact_revision") != row.revision or rec.get("approver_id") == row.submitted_by:
                problems.append(f"approval {manifest.approval_id} is not a separate APPROVED release of this revision")
        return problems

    def _n3_release_invalidated(self, s, tenant: str, payload_release_id: str) -> bool:
        for r in s.scalars(select(N3ReleaseRow).where(N3ReleaseRow.tenant_id == tenant)):
            if r.record.get("payload_release_id") == payload_release_id:
                if any(e.record["status"] == "INVALIDATED" for e in s.scalars(
                        select(N3EventRow).where(N3EventRow.tenant_id == tenant, N3EventRow.subject_kind == "release",
                                                 N3EventRow.subject_id == r.release_id))):
                    return True
        return False

    def _verification(self, repo: Repository, tenant: str, prd_id: str, design_revision: str
                      ) -> tuple[Optional[str], str]:
        """An active N4 VERIFIED release of exactly this design revision, if any."""
        for target in ("N8", "N6", "N7"):
            row = repo.latest_revision(tenant, f"VER-{prd_id}-{target}")
            if row is None:
                continue
            pl = row.canonical.get("payload") or {}
            d = repo.latest_decision(tenant, row.artifact_id, row.revision)
            status = self.n4.release_status_by_payload(tenant, pl.get("release_id")) if self.n4 else None
            if d is not None and d.decision == "PASS" and pl.get("verification_status") == "VERIFIED" and \
                    pl.get("design_revision") == design_revision and status in ("RELEASED", "PARTIAL"):
                return f"{row.artifact_id}@{row.revision}", "VERIFIED"
        return None, "NOT_VERIFIED"

    def lock_intake(self, p: Principal, req: BomIntakeRequest) -> N5BomIntake:
        t = p.tenant_id
        rel = self.n2.latest_release(t, req.prd_id)
        if rel is None:
            raise N0Error(409, "PRD_NOT_RELEASED", f"{req.prd_id} has no N2 PRDRelease")
        aid = f"BOM-{req.prd_id}-{req.scope}"
        with self.db.session() as s:
            repo = Repository(s)
            prd = repo.get_artifact(t, rel.n0_artifact_id, rel.prd_revision)
            if prd is None:
                raise N0Error(409, "PRD_NOT_PASS", f"{rel.n0_artifact_id}@{rel.prd_revision} is not stored in N0")
            prd_ref = f"{prd.artifact_id}@{prd.revision}"
            row = repo.latest_revision(t, aid)
            if row is None:
                raise N0Error(409, "BOM_NOT_RELEASED_TO_N5", f"no N3 BOM release {aid}")
            d = repo.latest_decision(t, aid, row.revision)
            manifest = next((m for m in reversed(repo.releases(t, aid, row.revision))
                             if m.status == "RELEASED" and "N5" in m.target_nodes), None)
            if d is None or d.decision != "PASS" or manifest is None:
                raise N0Error(409, "BOM_NOT_RELEASED_TO_N5", f"{aid}@{row.revision} is not an N0 PASS BOM released "
                                                             "to N5")
            if prd_ref not in row.canonical.get("source_artifact_ids", []):
                raise N0Error(409, "BOM_PRD_MISMATCH", f"{aid}@{row.revision} was released against "
                                                       f"{row.canonical.get('source_artifact_ids')}, not {prd_ref}")
            problems = self._verify_manifest(repo, row, manifest)
            if problems:
                raise N0Error(409, "BOM_SIGNATURE_INVALID", "; ".join(problems))
            rights = evaluate_rights("SOURCING", row.source_id, repo.get_source(t, row.source_id), self.clock(),
                                     N0Findings())
            if rights.status != "GRANTED":
                raise N0Error(403, "SOURCE_RIGHTS_DENIED", f"{row.source_id} does not permit SOURCING: "
                                                           f"{rights.reasons}")
            bom = row.canonical["payload"]
            if self._n3_release_invalidated(s, t, bom["release_id"]):
                raise N0Error(409, "BOM_INVALIDATED", f"N3 invalidated the release behind {aid}@{row.revision}")
            src = repo.get_source(t, req.release_source_id)
            if src is None or src.approval_status != "APPROVED" or \
                    not {"COMMERCIAL", "FINANCE"} <= set(src.permitted_use):
                raise N0Error(409, "RELEASE_SOURCE_NOT_APPROVED", f"{req.release_source_id} must be APPROVED and permit "
                                                                  "COMMERCIAL and FINANCE")
            critical = {c.line: c.reason for c in req.critical_lines}
            items = {i["line"]: i for i in bom["items"]}
            unknown = sorted(set(critical) - set(items))
            if unknown:
                raise N0Error(422, "UNKNOWN_LINE", f"critical lines {unknown} are not lines of {aid}@{row.revision}")
            lines, dnp = [], []
            for it in sorted(bom["items"], key=lambda i: i["line"]):
                if it.get("dnp"):
                    dnp.append(it["line"])
                    continue
                if not it.get("mpn") or int(it.get("qty") or 0) < 1:
                    raise N0Error(409, "BOM_LINE_INVALID", f"line {it['line']} has no MPN or no quantity")
                lines.append(N5BomLine(line=it["line"], mpn=normalize_mpn(it["mpn"]), mpn_as_released=it["mpn"],
                                       manufacturer=it.get("manufacturer"), refdes=it["refdes"],
                                       qty_per_unit=int(it["qty"]), q_required=int(it["qty"]) * req.build_quantity,
                                       critical=it["line"] in critical, critical_reason=critical.get(it["line"]),
                                       value=it.get("value"), footprint=it.get("footprint"),
                                       description=it.get("description")))
            if not lines:
                raise N0Error(409, "BOM_EMPTY", f"{aid}@{row.revision} has no sourcing lines")
            if critical.keys() & set(dnp):
                raise N0Error(422, "UNKNOWN_LINE", "a DNP line cannot be critical")
            ver_ref, ver_status = self._verification(repo, t, req.prd_id, row.revision)
            intake = N5BomIntake(
                intake_id=new_id("N5I"), tenant_id=t, project_id=req.project_id, prd_id=req.prd_id, prd_ref=prd_ref,
                bom_ref=f"{aid}@{row.revision}", bom_payload_sha256=row.payload_sha256,
                manifest_id=manifest.manifest_id, approval_id=manifest.approval_id, n3_release_id=bom["release_id"],
                scope=req.scope, design_revision=row.revision, pcb_revision=bom.get("pcb_revision"),
                build_quantity=req.build_quantity, purpose=req.purpose,
                target_lead_time_days=req.target_lead_time_days,
                base_currency=req.base_currency or self.settings.base_currency, lines=lines, dnp_lines=dnp,
                release_source_id=req.release_source_id, synthetic=row.synthetic or prd.synthetic,
                verification_ref=ver_ref, verification_status=ver_status, locked_by=p.principal_id,
                locked_at=self.clock())
            intake = _with_hash(intake, "intake_sha256", skip=("intake_id", "locked_at"))
            for irow in s.scalars(select(N5IntakeRow).where(N5IntakeRow.tenant_id == t,
                                                            N5IntakeRow.bom_artifact == aid,
                                                            N5IntakeRow.design_revision == row.revision)):
                old = N5BomIntake.model_validate(irow.record)
                if old.intake_sha256 == intake.intake_sha256 and \
                        self._flagged(s, t, "intake", old.intake_id, ("SUPERSEDED",)) is None:
                    return old
            s.add(N5IntakeRow(intake_id=intake.intake_id, tenant_id=t, prd_id=req.prd_id, bom_artifact=aid,
                              design_revision=row.revision, record=dump(intake)))
            repo.add_edge(t, f"artifact:{intake.bom_ref}", f"n5_intake:{intake.intake_id}", "SOURCED_BY")
            self._event(s, t, "intake", intake.intake_id, "LOCKED", p.principal_id)
            self._audit(s, p, "N5_INTAKE_LOCKED", f"n5_intake:{intake.intake_id}",
                        {"bom_ref": intake.bom_ref, "build_quantity": req.build_quantity, "lines": len(lines),
                         "intake_sha256": intake.intake_sha256, "verification": ver_status})
        return intake

    def _stale_reason(self, tenant: str, intake: N5BomIntake) -> Optional[str]:
        """Lazy revision cascade: N0 latest BOM revision, N3 invalidation and N2 PRD revision."""
        with self.db.session() as s:
            ev = self._flagged(s, tenant, "intake", intake.intake_id, ("SUPERSEDED",))
            if ev:
                return ev.get("reason") or "superseded"
            repo = Repository(s)
            aid = intake.bom_ref.split("@")[0]
            latest = repo.latest_revision(tenant, aid)
            reason = None
            if latest is not None and revision_order(latest.revision) > revision_order(intake.design_revision):
                reason = f"N3 released {aid}@{latest.revision}; this intake is locked to {intake.design_revision}"
            elif self._n3_release_invalidated(s, tenant, intake.n3_release_id):
                reason = f"N3 invalidated the release behind {intake.bom_ref}"
            else:
                rel = self.n2.latest_release(tenant, intake.prd_id)
                if rel is not None and f"{rel.n0_artifact_id}@{rel.prd_revision}" != intake.prd_ref and \
                        revision_order(rel.prd_revision) > revision_order(intake.prd_ref.split("@")[1]):
                    reason = f"N2 released PRD revision {rel.prd_revision} (intake locked to {intake.prd_ref})"
        if reason:
            self._supersede(tenant, intake.intake_id, reason)
        return reason

    def _supersede(self, tenant: str, intake_id: str, reason: str) -> None:
        blocked: list[str] = []
        with self.db.session() as s:
            if self._flagged(s, tenant, "intake", intake_id, ("SUPERSEDED",)):
                return
            self._event(s, tenant, "intake", intake_id, "SUPERSEDED", "svc-n5", reason=reason)
            for q in s.scalars(select(N5QuoteRow).where(N5QuoteRow.tenant_id == tenant,
                                                        N5QuoteRow.bom_intake_id == intake_id)):
                self._event(s, tenant, "quote", q.quote_id, "STALE", "svc-n5", reason=reason)
            for r in s.scalars(select(N5ReportRow).where(N5ReportRow.tenant_id == tenant,
                                                         N5ReportRow.bom_intake_id == intake_id)):
                self._event(s, tenant, "report", r.report_id, "STALE", "svc-n5", reason=reason)
                for h in s.scalars(select(N5HandoffRow).where(N5HandoffRow.tenant_id == tenant,
                                                              N5HandoffRow.report_id == r.report_id)):
                    self._event(s, tenant, "handoff", h.handoff_id, "BLOCKED", "svc-n5", reason=reason,
                                code="DESIGN_REVISION_CHANGED")
                    blocked.append(h.handoff_id)
            self._sys_audit(s, tenant, "N5_INTAKE_SUPERSEDED", f"n5_intake:{intake_id}",
                            {"reason": reason, "blocked_handoffs": blocked})
        for hid in blocked:
            self._notify(tenant, hid, "BLOCKED", reason)

    def on_n3_release(self, rel: N3Release) -> None:
        for pkg in rel.packages:
            if pkg.target != "N5" or pkg.status != "RELEASED" or not pkg.revision:
                continue
            with self.db.session() as s:
                rows = list(s.scalars(select(N5IntakeRow).where(N5IntakeRow.tenant_id == rel.tenant_id,
                                                                N5IntakeRow.bom_artifact == pkg.artifact_id)))
            for row in rows:
                if revision_order(row.design_revision) < revision_order(pkg.revision):
                    self._supersede(rel.tenant_id, row.intake_id,
                                    f"N3 released {pkg.artifact_id}@{pkg.revision} (intake locked to "
                                    f"{row.design_revision})")

    def on_prd_release(self, tenant: str, prd_id: str, revision: str) -> None:
        with self.db.session() as s:
            rows = list(s.scalars(select(N5IntakeRow).where(N5IntakeRow.tenant_id == tenant,
                                                            N5IntakeRow.prd_id == prd_id)))
        for row in rows:
            prd_rev = N5BomIntake.model_validate(row.record).prd_ref.split("@")[1]
            if revision_order(prd_rev) < revision_order(revision):
                self._supersede(tenant, row.intake_id, f"N2 released PRD {prd_id} revision {revision} (intake "
                                                       f"locked to {prd_rev})")

    # ==========================================================================================
    # N5.2 supplier intelligence
    # ==========================================================================================
    def suppliers(self, tenant: str) -> dict[str, N5Supplier]:
        with self.db.session() as s:
            out: dict[str, tuple[int, N5Supplier]] = {}
            for r in s.scalars(select(N5SupplierRow).where(N5SupplierRow.tenant_id == tenant)):
                if r.supplier_id not in out or r.seq > out[r.supplier_id][0]:
                    out[r.supplier_id] = (r.seq, N5Supplier.model_validate(r.record))
        return {k: v[1] for k, v in out.items()}

    def _put_supplier(self, s, p: Principal, sup: N5Supplier, event: str) -> N5Supplier:
        sup = _with_hash(sup, "supplier_sha256")
        s.add(N5SupplierRow(supplier_id=sup.supplier_id, seq=sup.version, tenant_id=p.tenant_id, status=sup.status,
                            record=dump(sup)))
        self._audit(s, p, event, f"n5_supplier:{sup.supplier_id}",
                    {"version": sup.version, "status": sup.status, "kind": sup.kind,
                     "supplier_sha256": sup.supplier_sha256})
        return sup

    def register_supplier(self, p: Principal, req: SupplierRequest) -> N5Supplier:
        cur = self.suppliers(p.tenant_id).get(req.supplier_id)
        if cur is not None:
            raise N0Error(409, "SUPPLIER_EXISTS", f"{req.supplier_id} exists; submit a revision instead")
        return self._new_supplier_version(p, req, 1)

    def revise_supplier(self, p: Principal, supplier_id: str, req: SupplierRequest) -> N5Supplier:
        cur = self.suppliers(p.tenant_id).get(supplier_id)
        if cur is None or req.supplier_id != supplier_id:
            raise N0Error(404, "SUPPLIER_NOT_FOUND", f"{supplier_id} is not registered")
        return self._new_supplier_version(p, req, cur.version + 1)

    def _new_supplier_version(self, p: Principal, req: SupplierRequest, version: int) -> N5Supplier:
        sup = N5Supplier(**req.model_dump(), tenant_id=p.tenant_id, status="PENDING", version=version,
                         submitted_by=p.principal_id, submitted_at=self.clock())
        with self.db.session() as s:
            return self._put_supplier(s, p, sup, "N5_SUPPLIER_SUBMITTED")

    def review_supplier(self, p: Principal, supplier_id: str, req: SupplierReviewRequest) -> N5Supplier:
        cur = self.suppliers(p.tenant_id).get(supplier_id)
        if cur is None:
            raise N0Error(404, "SUPPLIER_NOT_FOUND", f"{supplier_id} is not registered")
        if cur.submitted_by == p.principal_id:
            raise N0Error(403, "SEPARATION_OF_DUTIES", "the submitter of a supplier record cannot qualify it")
        if req.decision == "APPROVED":
            gaps = []
            today = self.clock().date()
            if req.approved_until is None or req.approved_until <= today:
                gaps.append("approved_until must be a future date")
            if cur.kind in ("EMS", "ODM"):
                if not any(x.os_id and x.osh_verified for x in cur.sites):
                    gaps.append("an EMS / ODM needs at least one site verified against Open Supply Hub")
            if cur.kind == "DISTRIBUTOR":
                proofs = {engine.normalize_manufacturer(c.manufacturer) for c in cur.channel_proofs
                          if c.valid_until > today}
                verified = {engine.normalize_manufacturer(m) for m in req.verified_channel_manufacturers}
                if not verified:
                    gaps.append("a distributor needs at least one verified authorized-channel proof")
                elif not verified <= proofs:
                    gaps.append(f"verified manufacturers {sorted(req.verified_channel_manufacturers)} must each have "
                                "a current channel proof on the record")
            if cur.kind == "MANUFACTURER" and not cur.manufacturer_names:
                gaps.append("a manufacturer record must name the brands it makes")
            if gaps:
                raise N0Error(409, "QUALIFICATION_INCOMPLETE", "; ".join(gaps))
        new = cur.model_copy(update={"status": req.decision, "version": cur.version + 1, "reviewed_by": p.principal_id,
                                     "reviewed_at": self.clock(), "attestation": req.attestation,
                                     "approved_until": req.approved_until if req.decision == "APPROVED" else None,
                                     "verified_channel_manufacturers": req.verified_channel_manufacturers
                                     if req.decision == "APPROVED" else []})
        with self.db.session() as s:
            return self._put_supplier(s, p, new, f"N5_SUPPLIER_{req.decision}")

    def verify_site(self, p: Principal, supplier_id: str, site_id: str, osh_csv: Optional[bytes] = None
                    ) -> N5Supplier:
        cur = self.suppliers(p.tenant_id).get(supplier_id)
        if cur is None:
            raise N0Error(404, "SUPPLIER_NOT_FOUND", f"{supplier_id} is not registered")
        site = next((x for x in cur.sites if x.site_id == site_id), None)
        if site is None or not site.os_id:
            raise N0Error(404, "SITE_NOT_FOUND", f"{site_id} is not a site of {supplier_id} with an OS ID")
        conn = self.connectors["OPEN_SUPPLY_HUB"]
        if osh_csv is not None:
            fac = conn.parse_csv(osh_csv).get(site.os_id)
            method, sha, http = "CSV_SNAPSHOT", sha256_hex(osh_csv), None
        else:
            if not self.settings.enable_live_apis:
                raise N0Error(409, "LIVE_APIS_DISABLED", "Open Supply Hub live lookups are disabled; upload an OSH CSV")
            if not conn.configured():
                raise N0Error(409, "CONNECTOR_NOT_CONFIGURED", "OPEN_SUPPLY_HUB token is not configured")
            try:
                self.limiters["OPEN_SUPPLY_HUB"].acquire()
                res = conn.lookup(site.os_id)
            except Exception as exc:  # noqa: BLE001 - connector errors are recorded, never fatal to the record
                self._log_call(p, "OPEN_SUPPLY_HUB", "facility", site.os_id, outcome="ERROR", error=str(exc)[:300])
                raise N0Error(502, "UPSTREAM_ERROR", f"Open Supply Hub lookup failed: {str(exc)[:300]}") from None
            self._log_call(p, "OPEN_SUPPLY_HUB", "facility", site.os_id, http_status=res.http_status,
                           response_sha256=res.response_sha256, outcome="OK" if res.offers else "NOT_FOUND")
            fac, method, sha, http = (res.offers[0] if res.offers else None), "LIVE_API", res.response_sha256, \
                res.http_status
        if fac is None:
            raise N0Error(409, "FACILITY_NOT_FOUND", f"OS ID {site.os_id} was not found ({method})")
        if fac.get("country_code") and fac["country_code"] != site.country:
            raise N0Error(409, "FACILITY_MISMATCH", f"OS ID {site.os_id} is in {fac['country_code']}, the site says "
                                                    f"{site.country}")
        sites = [x.model_copy(update={"osh_verified": True, "osh_evidence": {
            **fac, "method": method, "evidence_sha256": sha, "http_status": http,
            "verified_by": p.principal_id, "verified_at": self.clock().isoformat()}}) if x.site_id == site_id else x
            for x in cur.sites]
        new = cur.model_copy(update={"sites": sites, "version": cur.version + 1})
        with self.db.session() as s:
            return self._put_supplier(s, p, new, "N5_SUPPLIER_SITE_VERIFIED")

    # ==========================================================================================
    # FX assumptions
    # ==========================================================================================
    def record_fx(self, p: Principal, req: FxAssumptionRequest) -> N5FxAssumption:
        with self.db.session() as s:
            if s.get(N5FxRow, (req.fx_id, p.tenant_id)) is not None:
                raise N0Error(409, "FX_EXISTS", f"{req.fx_id} exists; record a new assumption id")
            rec = N5FxAssumption(**req.model_dump(), tenant_id=p.tenant_id, recorded_by=p.principal_id,
                                 recorded_at=self.clock())
            s.add(N5FxRow(fx_id=rec.fx_id, tenant_id=p.tenant_id, pair=f"{rec.from_currency}{rec.to_currency}",
                          record=dump(rec)))
            self._audit(s, p, "N5_FX_RECORDED", f"n5_fx:{rec.fx_id}", {"pair": f"{rec.from_currency}/{rec.to_currency}",
                                                                       "rate": str(rec.rate), "source": rec.source})
        return rec

    def _fx_table(self, tenant: str) -> dict[tuple[str, str], N5FxAssumption]:
        today = self.clock().date()
        best: dict[tuple[str, str], N5FxAssumption] = {}
        with self.db.session() as s:
            for r in s.scalars(select(N5FxRow).where(N5FxRow.tenant_id == tenant)):
                a = N5FxAssumption.model_validate(r.record)
                if a.as_of > today or (a.valid_until is not None and a.valid_until < today):
                    continue
                k = (a.from_currency, a.to_currency)
                if k not in best or (a.as_of, a.recorded_at) > (best[k].as_of, best[k].recorded_at):
                    best[k] = a
        return best

    # ==========================================================================================
    # N5.4 – N5.5 sourcing report
    # ==========================================================================================
    def create_report(self, p: Principal, req: SourcingReportRequest) -> N5SourcingReport:
        t = p.tenant_id
        with self.db.session() as s:
            intake = self._intake(s, t, req.bom_intake_id)
        stale = self._stale_reason(t, intake)
        quotes = self.quotes_for(t, intake.intake_id)
        if req.quote_ids is not None:
            with self.db.session() as s:
                quotes = []
                for qid in req.quote_ids:
                    q = self._get(s, N5QuoteRow, N5Quote, t, qid, "QUOTE_NOT_FOUND")
                    if q.status != "ACCEPTED":
                        raise N0Error(409, "QUOTE_NOT_ACCEPTED", f"{qid} was rejected at intake: {q.rejection_codes}")
                    other = self._intake(s, t, q.bom_intake_id)
                    if other.prd_id != intake.prd_id:
                        raise N0Error(409, "QUOTE_NOT_FOR_THIS_PRD", f"{qid} quotes {other.bom_ref}")
                    quotes.append(q)
        fx = self._fx_table(t)
        with self.db.session() as s:
            refs = {q.quote_id: self._intake(s, t, q.bom_intake_id).bom_ref for q in quotes}
        ctx = engine.Context(intake=intake, now=self.clock(), suppliers=self.suppliers(t), fx=fx,
                             source_status=self._source_status(t), tampered=self._verify_quote_files(t, quotes),
                             quote_bom_refs=refs,
                             lead_time_warn_ratio=self.settings.lead_time_warn_ratio,
                             qualification_warn_days=self.settings.qualification_warn_days)
        out = engine.assess(ctx, quotes, stale)
        by_id = {q.quote_id: q for q in quotes}
        selected = sorted(out["selected_quotes"])
        used_fx = sorted({x.fx_id for ln in out["lines"] for x in ln.offers if x.rank == 1 and x.fx_id})
        report = N5SourcingReport(
            report_id=new_id("N5R"), tenant_id=t, bom_intake_id=intake.intake_id, intake_sha256=intake.intake_sha256,
            bom_ref=intake.bom_ref, design_revision=intake.design_revision, build_quantity=intake.build_quantity,
            base_currency=intake.base_currency, decision=out["decision"], findings=out["findings"], lines=out["lines"],
            quote_ids=[q.quote_id for q in quotes],
            quote_hashes=sorted({by_id[q].quote_sha256 for q in selected}),
            all_quote_hashes=sorted({q.quote_sha256 for q in quotes}),
            quote_source_ids=sorted({by_id[q].source_id for q in selected}),
            fx_assumptions=[x for x in fx.values() if x.fx_id in used_fx],
            indicative_total=out["indicative_total"], indicative_unit_cost=out["indicative_unit_cost"],
            excluded_items_reported=out["excluded"], synthetic=intake.synthetic or any(q.synthetic for q in quotes),
            engine_version=engine.ENGINE_VERSION, created_by=p.principal_id, created_at=self.clock())
        report = _with_hash(report, "report_sha256")
        with self.db.session() as s:
            s.add(N5ReportRow(report_id=report.report_id, tenant_id=t, bom_intake_id=intake.intake_id,
                              decision=report.decision, record=dump(report)))
            for qid in report.quote_ids:
                Repository(s).add_edge(t, f"n5_quote:{qid}", f"n5_report:{report.report_id}", "ASSESSED_IN")
            self._event(s, t, "report", report.report_id, "CREATED", p.principal_id, decision=report.decision)
            self._audit(s, p, f"N5_REPORT_{report.decision}", f"n5_report:{report.report_id}",
                        {"report_sha256": report.report_sha256, "indicative_total": str(report.indicative_total),
                         "codes": sorted({f.code for f in report.findings if f.outcome != "INFO"})})
        return report

    def _reports(self, s, tenant: str, intake_id: str) -> list[N5SourcingReport]:
        rows = s.scalars(select(N5ReportRow).where(N5ReportRow.tenant_id == tenant,
                                                   N5ReportRow.bom_intake_id == intake_id)
                         .order_by(N5ReportRow.created_at, N5ReportRow.report_id))
        return [N5SourcingReport.model_validate(r.record) for r in rows]

    def _current_report(self, tenant: str, report_id: str) -> N5SourcingReport:
        with self.db.session() as s:
            r = self._get(s, N5ReportRow, N5SourcingReport, tenant, report_id, "REPORT_NOT_FOUND")
            latest = self._reports(s, tenant, r.bom_intake_id)[-1]
            intake = self._intake(s, tenant, r.bom_intake_id)
        if latest.report_id != r.report_id:
            raise N0Error(409, "STALE_REPORT", f"{report_id} is superseded by {latest.report_id}")
        reason = self._stale_reason(tenant, intake)
        with self.db.session() as s:
            ev = self._flagged(s, tenant, "report", report_id, ("STALE",))
        if reason or ev:
            raise N0Error(409, "REPORT_STALE", reason or ev.get("reason", "stale"))
        if _with_hash(r.model_copy(update={"report_sha256": None}), "report_sha256").report_sha256 != r.report_sha256:
            raise N0Error(409, "REPORT_HASH_MISMATCH", f"stored report {report_id} no longer matches its hash")
        return r

    def _contributors(self, tenant: str, r: N5SourcingReport) -> set[str]:
        with self.db.session() as s:
            intake = self._intake(s, tenant, r.bom_intake_id)
            quotes = {q: self._get(s, N5QuoteRow, N5Quote, tenant, q, "QUOTE_NOT_FOUND") for q in r.quote_ids}
        sel = [quotes[x.quote_id] for ln in r.lines for x in ln.offers if x.rank == 1]
        sups = self.suppliers(tenant)
        who = {r.created_by, intake.locked_by} | {q.submitted_by for q in sel}
        for q in sel:
            sup = sups.get(q.supplier_id)
            if sup is not None:
                who |= {sup.submitted_by} | ({sup.reviewed_by} if sup.reviewed_by else set())
        return who

    # ==========================================================================================
    # N5.6 review and handoff
    # ==========================================================================================
    def review(self, p: Principal, req: N5ReviewRequest) -> N5Review:
        t = p.tenant_id
        r = self._current_report(t, req.report_id)
        if req.report_sha256 != r.report_sha256:
            raise N0Error(409, "REPORT_HASH_MISMATCH", "the review must quote the report's exact SHA-256")
        if sorted(req.quote_hashes) != r.quote_hashes:
            raise N0Error(409, "QUOTE_HASH_MISMATCH", "the review must quote exactly the selected quote hashes")
        if req.decision == "APPROVED":
            if r.decision == "BLOCK":
                raise N0Error(409, "BLOCK_NOT_APPROVABLE", "a BLOCK sourcing report can never be approved")
            if r.decision == "REWORK":
                raise N0Error(409, "REWORK_OPEN", "resolve the REWORK findings and re-run the report first")
        if p.principal_id in self._contributors(t, r):
            raise N0Error(403, "SEPARATION_OF_DUTIES", "the approver cannot be the analyst, a quote submitter or the "
                                                       "supplier-quality reviewer of a selected supplier")
        with self.db.session() as s:
            quotes = [self._get(s, N5QuoteRow, N5Quote, t, q, "QUOTE_NOT_FOUND") for q in r.quote_ids]
        bad = self._verify_quote_files(t, [q for q in quotes if q.quote_sha256 in r.quote_hashes])
        if bad:
            raise N0Error(409, "QUOTE_HASH_MISMATCH", "; ".join(bad.values()))
        role = "procurement_approver"
        with self.db.session() as s:
            repo = Repository(s)
            ev = repo.append_approval(tenant_id=t, artifact_id=f"n5-report:{r.report_id}",
                                      exact_revision=r.report_sha256[:32], approver_id=p.principal_id,
                                      approver_role=role, decision=req.decision, scope="EVIDENCE_ATTESTATION",
                                      reason=f"{req.reason[:400]} | quotes {','.join(h[:12] for h in r.quote_hashes)}")
            rv = N5Review(review_id=new_id("N5V"), tenant_id=t, report_id=r.report_id, reviewer=p.principal_id,
                          reviewer_role=role, decision=req.decision, reason=req.reason,
                          report_sha256=r.report_sha256, quote_hashes=r.quote_hashes,
                          approval_event_id=ev.approval_id, reviewed_at=self.clock())
            s.add(N5ReviewRow(review_id=rv.review_id, tenant_id=t, report_id=r.report_id, record=dump(rv)))
            repo.add_edge(t, f"approval:{ev.approval_id}", f"n5_report:{r.report_id}",
                          "APPROVES" if req.decision == "APPROVED" else "REJECTS", scope="N5_SOURCING_REVIEW")
            self._event(s, t, "report", r.report_id, f"REVIEW_{req.decision}", p.principal_id, review_id=rv.review_id)
            self._audit(s, p, f"N5_REVIEW_{req.decision}", f"n5_report:{r.report_id}",
                        {"review_id": rv.review_id, "approval_id": ev.approval_id, "report_sha256": r.report_sha256})
        return rv

    def _payload(self, target: str, r: N5SourcingReport, rv: N5Review, intake: N5BomIntake,
                 quotes: dict[str, N5Quote], submitters: list[str]) -> dict:
        sups = self.suppliers(intake.tenant_id)
        lines = []
        for ln in r.lines:
            top = ln.offers[0]
            q = quotes[top.quote_id]
            o = next(x for x in q.offers if x.offer_id == top.offer_id)
            sup = sups.get(q.supplier_id)
            lines.append({
                "line": ln.line, "mpn": ln.mpn, "manufacturer": ln.manufacturer, "qty_per_unit": ln.qty_per_unit,
                "q_required": ln.q_required, "critical": ln.critical, "supplier_id": q.supplier_id,
                "supplier_kind": sup.kind if sup else "DISTRIBUTOR",
                "supplier_qualification": sup.status if sup else "NOT_REGISTERED", "quote_id": q.quote_id,
                "quote_sha256": q.quote_sha256, "quote_source_id": q.source_id, "quoted_mpn": o.quoted_mpn,
                "moq": o.moq, "pack_multiple": o.pack_multiple, "q_order": top.q_order,
                "price_breaks": [b.model_dump(mode="json") for b in o.price_breaks],
                "unit_price": str(top.unit_price), "currency": o.currency,
                "line_charges": [c.model_dump(mode="json") for c in o.line_charges],
                "extended_cost": str(top.extended_cost), "fx_id": top.fx_id,
                "fx_rate": str(top.fx_rate) if top.fx_rate is not None else None,
                "extended_cost_base": str(top.extended_cost_base), "availability": o.availability,
                "stock_qty": o.stock_qty, "lead_time_days": o.lead_time_days,
                "valid_until": q.valid_until.isoformat().replace("+00:00", "Z"),
                "eligible_offers": sum(1 for x in ln.offers if x.eligible), "risk_flags": ln.risk_flags})
        common = {
            "handoff_id": "N5H-" + sha256_hex(canonical_json({"report": r.report_id, "review": rv.review_id}))[:24],
            "target": target, "prd_ref": intake.prd_ref, "bom_ref": intake.bom_ref,
            "design_revision": intake.design_revision, "pcb_revision": intake.pcb_revision,
            "build_quantity": intake.build_quantity, "report_id": r.report_id, "report_sha256": r.report_sha256,
            "quote_hashes": r.quote_hashes, "review_id": rv.review_id, "approved_by": rv.reviewer,
            "submitters": submitters, "quote_source_ids": r.quote_source_ids, "base_currency": r.base_currency,
            "lines": lines, "fx_assumptions": [x.model_dump(mode="json") for x in r.fx_assumptions],
            "indicative_total": str(r.indicative_total), "indicative_unit_cost": str(r.indicative_unit_cost),
            "indicative_scope": INDICATIVE_MATERIAL_LABEL, "excluded_cost_categories": list(EXCLUDED_COST_CATEGORIES),
            "excluded_items_reported": r.excluded_items_reported, "verification_ref": intake.verification_ref,
            "approval_scope": APPROVAL_SCOPE}
        if target == "N6":
            common["commercial"] = {
                "bidding_inputs": {"indicative_unit_material_cost": str(r.indicative_unit_cost),
                                   "currency": r.base_currency, "build_quantity": intake.build_quantity,
                                   "label": INDICATIVE_MATERIAL_LABEL},
                "supplier_risk_flags": [{"line": x["line"], "mpn": x["mpn"], "supplier_id": x["supplier_id"],
                                         "flags": x["risk_flags"]} for x in lines if x["risk_flags"]],
                "lead_time_constraints": {"target_lead_time_days": intake.target_lead_time_days,
                                          "max_quoted_lead_time_days": max(x["lead_time_days"] for x in lines),
                                          "per_line": [{"line": x["line"], "lead_time_days": x["lead_time_days"]}
                                                       for x in lines]},
                "design_verification": intake.verification_status}
        else:
            common["finance"] = {
                "quote_validity": {"earliest_expiry": min(x["valid_until"] for x in lines),
                                   "per_line": [{"line": x["line"], "quote_id": x["quote_id"],
                                                 "valid_until": x["valid_until"]} for x in lines]},
                "currencies": sorted({x["currency"] for x in lines}),
                "note": "Indicative material quote only; GST, tooling / NRE, financing, freight and duties, labour "
                        "and warranty are N7's to add. Quoted excluded items are listed for reference."}
        return common

    def release(self, p: Principal, report_id: str, req: HandoffRequest) -> N5Handoff:
        t = p.tenant_id
        r = self._current_report(t, report_id)
        with self.db.session() as s:
            reviews = [N5Review.model_validate(x.record) for x in s.scalars(
                select(N5ReviewRow).where(N5ReviewRow.tenant_id == t, N5ReviewRow.report_id == report_id)
                .order_by(N5ReviewRow.created_at))]
            intake = self._intake(s, t, r.bom_intake_id)
            quotes = {q: self._get(s, N5QuoteRow, N5Quote, t, q, "QUOTE_NOT_FOUND") for q in r.quote_ids}
        if not reviews or reviews[-1].decision != "APPROVED":
            raise N0Error(409, "APPROVAL_REQUIRED", f"{report_id} has no procurement_approver approval")
        rv = reviews[-1]
        if rv.report_sha256 != r.report_sha256 or rv.quote_hashes != r.quote_hashes:
            raise N0Error(409, "REPORT_CHANGED", "the approval does not match the current report / quote hashes")
        submitters = sorted(self._contributors(t, r))
        if p.principal_id in submitters:
            raise N0Error(403, "SEPARATION_OF_DUTIES", "a contributor to the report cannot release it")
        sel = {x.quote_id for ln in r.lines for x in ln.offers if x.rank == 1}
        now = self.clock()
        expired = sorted(quotes[q].quote_ref for q in sel if quotes[q].valid_until <= now)
        if expired:
            raise N0Error(409, "QUOTE_EXPIRED", f"selected quotes expired since approval: {expired}")
        status = self._source_status(t)
        revoked = sorted(sid for sid in r.quote_source_ids if status(sid) != "APPROVED")
        if revoked:
            raise N0Error(409, "SOURCE_RIGHTS_REVOKED", f"quotation sources {revoked} are no longer APPROVED")
        sups = self.suppliers(t)
        lapsed = sorted({quotes[q].supplier_id for q in sel if (sups.get(quotes[q].supplier_id) is None or
                                                                sups[quotes[q].supplier_id].status != "APPROVED")})
        if lapsed:
            raise N0Error(409, "SUPPLIER_NOT_APPROVED", f"suppliers {lapsed} are no longer APPROVED")
        bad = self._verify_quote_files(t, [quotes[q] for q in sel])
        if bad:
            raise N0Error(409, "QUOTE_HASH_MISMATCH", "; ".join(bad.values()))
        packages = [self._submit_and_release(p, intake, target, self._payload(target, r, rv, intake, quotes,
                                                                                submitters), r)
                    for target in dict.fromkeys(req.targets)]
        released = [x for x in packages if x.status == "RELEASED"]
        h = N5Handoff(handoff_id=new_id("N5H"), tenant_id=t, report_id=r.report_id, review_id=rv.review_id,
                      bom_intake_id=intake.intake_id, design_revision=intake.design_revision,
                      status="RELEASED" if len(released) == len(packages) else ("PARTIAL" if released else "REFUSED"),
                      packages=packages, quote_source_ids=r.quote_source_ids, indicative_total=r.indicative_total,
                      base_currency=r.base_currency, released_by=p.principal_id, released_at=self.clock())
        with self.db.session() as s:
            s.add(N5HandoffRow(handoff_id=h.handoff_id, tenant_id=t, report_id=r.report_id, record=dump(h)))
            for x in released:
                Repository(s).add_edge(t, f"n5_report:{r.report_id}", f"artifact:{x.artifact_id}@{x.revision}",
                                       "HANDED_OFF_AS", node=x.target)
            self._event(s, t, "handoff", h.handoff_id, h.status, p.principal_id)
            self._audit(s, p, f"N5_HANDOFF_{h.status}", f"n5_handoff:{h.handoff_id}",
                        {"packages": [{"target": x.target, "status": x.status, "manifest_id": x.manifest_id,
                                       "codes": x.violated_codes} for x in packages]})
        return h

    def _submit_and_release(self, p: Principal, intake: N5BomIntake, target: str, payload: dict,
                            r: N5SourcingReport) -> N5HandoffPackage:
        aid = f"PRC-{intake.prd_id}-{target}"
        sha = sha256_hex(canonical_json(payload))
        with self.db.session() as s:
            top = Repository(s).latest_revision(p.tenant_id, aid)
            rev = _next_revision(top.revision if top else None, top.payload_sha256 if top else None, sha)
        art = Artifact(tenant_id=p.tenant_id, project_id=intake.project_id, artifact_id=aid,
                       artifact_type="SOURCING_HANDOFF", source_id=intake.release_source_id,
                       processing_purpose=PURPOSE[target], release_stage="CONCEPT" if r.synthetic else "PROTOTYPE",
                       revision=rev, synthetic=r.synthetic, source_sha256=sha, payload=payload,
                       source_artifact_ids=[intake.bom_ref, intake.prd_ref])
        res = self.pipeline.evaluate(self._svc(p), EvaluateRequest(artifact=art, origin_node="N5",
                                                                   requested_destinations=[target]))
        d = res.decision
        rev = res.canonical_artifact.revision if res.canonical_artifact else rev
        base = dict(target=target, artifact_id=aid, revision=rev, n0_decision=d.decision, n0_decision_id=d.decision_id,
                    violated_codes=sorted({v.code for v in d.violated_rules}))
        if d.decision != "PASS":
            return N5HandoffPackage(**base, status="REFUSED", reason=f"N0 decision {d.decision}: {d.explanation}"[:1000])
        try:
            with self.db.session() as s:
                repo = Repository(s)
                ap = record_approval(repo, p, ApprovalRequest(
                    artifact_id=aid, exact_revision=rev, scope="RELEASE", decision="APPROVED",
                    reason=f"N5 sourcing handoff to {target} for BOM {intake.bom_ref}"), self.clock(), self.store)
                m = release_handoff(repo, p, HandoffReleaseRequest(artifact_id=aid, revision=rev, source_node="N5",
                                                                   target_nodes=[target],
                                                                   approval_id=ap.approval_event.approval_id),
                                    self.clock())
        except N0Error as err:
            return N5HandoffPackage(**base, status="REFUSED", reason=f"{err.code}: {err.message}"[:1000])
        return N5HandoffPackage(**base, status="RELEASED", manifest_id=m.manifest_id,
                                approval_id=ap.approval_event.approval_id,
                                reason=f"draft handoff released to {target} via N0 manifest {m.manifest_id}")

    # ==========================================================================================
    # dynamic rights revocation
    # ==========================================================================================
    def on_source_rights(self, tenant: str, source_id: str, status: str) -> None:
        if status == "APPROVED":
            return
        reason = f"N0 source {source_id} is now {status}"
        blocked: list[str] = []
        with self.db.session() as s:
            qids = [q.quote_id for q in s.scalars(select(N5QuoteRow).where(N5QuoteRow.tenant_id == tenant,
                                                                           N5QuoteRow.source_id == source_id))]
            for qid in qids:
                self._event(s, tenant, "quote", qid, "RIGHTS_REVOKED", "svc-n5", reason=reason)
            for row in s.scalars(select(N5ReportRow).where(N5ReportRow.tenant_id == tenant)):
                if set(row.record.get("quote_ids") or []) & set(qids):
                    self._event(s, tenant, "report", row.report_id, "STALE", "svc-n5", reason=reason)
            for row in s.scalars(select(N5HandoffRow).where(N5HandoffRow.tenant_id == tenant)):
                if source_id in (row.record.get("quote_source_ids") or []) and \
                        not self._flagged(s, tenant, "handoff", row.handoff_id, ("BLOCKED",)):
                    self._event(s, tenant, "handoff", row.handoff_id, "BLOCKED", "svc-n5", reason=reason,
                                code="SOURCE_RIGHTS_REVOKED")
                    blocked.append(row.handoff_id)
            self._sys_audit(s, tenant, "N5_RIGHTS_REVOKED", f"source:{source_id}",
                            {"quotes": qids, "blocked_handoffs": blocked})
        for hid in blocked:
            self._notify(tenant, hid, "BLOCKED", reason)

    def _notify(self, tenant: str, handoff_id: str, status: str, reason: str) -> None:
        for listener in self.handoff_listeners:
            listener(tenant, handoff_id, status, reason)

    # ==========================================================================================
    # ECO proposals (never substitutions)
    # ==========================================================================================
    def raise_eco(self, p: Principal, req: EcoProposalRequest) -> N5EcoProposal:
        t = p.tenant_id
        with self.db.session() as s:
            intake = self._intake(s, t, req.bom_intake_id)
        line = next((x for x in intake.lines if x.line == req.line), None)
        if line is None:
            raise N0Error(422, "UNKNOWN_LINE", f"line {req.line} is not a sourcing line of {intake.bom_ref}")
        if normalize_mpn(req.proposed_mpn) == line.mpn:
            raise N0Error(422, "NOT_AN_ALTERNATE", "the proposed MPN is the approved MPN")
        if req.offer_id:
            q = next((q for q in self.quotes_for(t, intake.intake_id, accepted_only=False)
                      for o in q.offers if o.offer_id == req.offer_id), None)
            o = next((o for o in q.offers if o.offer_id == req.offer_id), None) if q else None
            if o is None or normalize_mpn(o.quoted_mpn) != normalize_mpn(req.proposed_mpn):
                raise N0Error(422, "OFFER_MISMATCH", f"offer {req.offer_id} does not quote {req.proposed_mpn}")
        eco = N5EcoProposal(**req.model_dump(), eco_id=new_id("N5ECO"), tenant_id=t, approved_mpn=line.mpn_as_released,
                            design_revision=intake.design_revision, bom_ref=intake.bom_ref, raised_by=p.principal_id,
                            raised_at=self.clock())
        with self.db.session() as s:
            s.add(N5EcoRow(eco_id=eco.eco_id, tenant_id=t, bom_intake_id=intake.intake_id, record=dump(eco)))
            Repository(s).add_edge(t, f"n5_eco:{eco.eco_id}", f"artifact:{intake.bom_ref}", "ECO_PROPOSED_FOR",
                                   line=req.line)
            self._event(s, t, "eco", eco.eco_id, "PENDING_N3_ECO", p.principal_id)
            self._audit(s, p, "N5_ECO_PROPOSED", f"n5_eco:{eco.eco_id}",
                        {"line": req.line, "approved": line.mpn_as_released, "proposed": req.proposed_mpn,
                         "evidence": [e.kind for e in req.equivalence_evidence]})
        return eco

    def ecos(self, tenant: str, intake_id: Optional[str] = None) -> list[N5EcoProposal]:
        with self.db.session() as s:
            q = select(N5EcoRow).where(N5EcoRow.tenant_id == tenant)
            if intake_id:
                q = q.where(N5EcoRow.bom_intake_id == intake_id)
            return [N5EcoProposal.model_validate(r.record) for r in s.scalars(q.order_by(N5EcoRow.created_at))]

    # ==========================================================================================
    # views
    # ==========================================================================================
    def intake_view(self, tenant: str, intake_id: str) -> dict:
        with self.db.session() as s:
            intake = self._intake(s, tenant, intake_id)
        stale = self._stale_reason(tenant, intake)
        return {"intake": dump(intake), "status": "SUPERSEDED" if stale else "ACTIVE", "reason": stale,
                "quotes": [{"quote_id": q.quote_id, "supplier_id": q.supplier_id, "channel": q.channel,
                            "status": q.status, "codes": q.rejection_codes, "quote_sha256": q.quote_sha256}
                           for q in self.quotes_for(tenant, intake_id, accepted_only=False)],
                "reports": [r.report_id for r in self._reports_for(tenant, intake_id)]}

    def _reports_for(self, tenant: str, intake_id: str) -> list[N5SourcingReport]:
        with self.db.session() as s:
            return self._reports(s, tenant, intake_id)

    def quote(self, tenant: str, quote_id: str) -> N5Quote:
        with self.db.session() as s:
            return self._get(s, N5QuoteRow, N5Quote, tenant, quote_id, "QUOTE_NOT_FOUND")

    def report(self, tenant: str, report_id: str) -> N5SourcingReport:
        with self.db.session() as s:
            return self._get(s, N5ReportRow, N5SourcingReport, tenant, report_id, "REPORT_NOT_FOUND")

    def handoff(self, tenant: str, handoff_id: str) -> N5Handoff:
        with self.db.session() as s:
            return self._get(s, N5HandoffRow, N5Handoff, tenant, handoff_id, "HANDOFF_NOT_FOUND")

    def handoff_status(self, tenant: str, handoff_id: str) -> dict:
        """Live status for downstream nodes: BLOCKED if a cascade blocked it, if a quote source is no longer
        APPROVED or if the BOM intake was superseded; otherwise the release status."""
        h = self.handoff(tenant, handoff_id)
        with self.db.session() as s:
            hist = self._events(s, tenant, "handoff", handoff_id)
            intake = self._intake(s, tenant, h.bom_intake_id)
        reasons = [e.get("reason") for e in hist if e["status"] == "BLOCKED"]
        status = self._source_status(tenant)
        reasons += [f"source {sid} is {status(sid) or 'not registered'}" for sid in h.quote_source_ids
                    if status(sid) != "APPROVED" and not any(sid in (x or "") for x in reasons)]
        stale = self._stale_reason(tenant, intake)
        if stale and stale not in reasons:
            reasons.append(stale)
        return {"handoff_id": handoff_id, "status": "BLOCKED" if reasons else h.status, "reasons": reasons,
                "history": hist}

    def handoff_for_artifact(self, tenant: str, artifact_id: str, revision: str) -> Optional[N5Handoff]:
        with self.db.session() as s:
            for row in s.scalars(select(N5HandoffRow).where(N5HandoffRow.tenant_id == tenant)
                                 .order_by(N5HandoffRow.created_at.desc())):
                h = N5Handoff.model_validate(row.record)
                if any(x.artifact_id == artifact_id and x.revision == revision and x.status == "RELEASED"
                       for x in h.packages):
                    return h
        return None

    def status(self, tenant: str, kind: str, subject_id: str) -> dict:
        if kind not in ("intake", "quote", "report", "handoff", "eco"):
            raise N0Error(422, "UNKNOWN_SUBJECT", "kind must be intake, quote, report, handoff or eco")
        if kind == "handoff":
            return self.handoff_status(tenant, subject_id)
        with self.db.session() as s:
            hist = self._events(s, tenant, kind, subject_id)
        if not hist:
            raise N0Error(404, "NOT_FOUND", f"{kind} {subject_id} not found")
        return {"subject": f"{kind}:{subject_id}", "status": hist[-1]["status"], "history": hist}

    def overview(self) -> dict:
        return {"n5_version": N5_VERSION, "engine_version": engine.ENGINE_VERSION,
                "live_apis_enabled": self.settings.enable_live_apis,
                "connectors": {n: {"configured": c.configured(), "terms": c.policy.terms,
                                   "max_cache_ttl_s": min(self.settings.cache_ttl_s, c.policy.max_ttl_s),
                                   "persist_raw": c.policy.persist_raw, "training_allowed": c.policy.training_allowed}
                               for n, c in self.connectors.items()},
                "cache_entries": len(self.cache), "indicative_scope": INDICATIVE_MATERIAL_LABEL,
                "approval_scope": APPROVAL_SCOPE}


__all__ = ["N5Service"]
