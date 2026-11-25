"""N3 service: the seven stages over the shared N0 database, object store, scanner and approval chain.

N3.1 lock_baseline      only an N2 PRDRelease whose N0 artifact, manifest, payload hash and approval
                        chain event all verify; the newest PRD revision only.
N3.2 plan / create_wp   immutable, content-hashed work packages per design scope.
N3.3 route              permitted tool, or onboarded vendor under NDA / IP / budget bounds plus the N0
                        VendorWorkPackage.v1 transmission check.
N3.4 deliver / submit_job   controlled vendor or engineer intake (manifest + SHA-256 + scan + native
                        format detection), and sandboxed KiCad / ngspice jobs.
N3.5 assess             deterministic verification of the current file set → N3Assessment.
N3.6 propose_actions / review   cost-aware proposals (never executed) and the design-authority gate.
N3.7 release            DESIGN_RELEASE → N4 and BOM_RELEASE → N5 through the N0 pipeline + approval.

A new N2 PRD revision invalidates every dependent baseline, work package and release, through the N2
release listener and lazily on each N3 call.
"""
from __future__ import annotations

import posixpath
from typing import Any, Callable, Optional

from sqlalchemy import select

from ..db import ApprovalRow, BlobRow
from ..errors import N0Error
from ..governance import record_approval, release_handoff
from ..models import (NATIVE_EXTENSIONS, ApprovalRequest, Artifact, DesignConstraints, EvaluateRequest,
                      ExpectedOutput, FileRef, HandoffReleaseRequest, Requirement, TestLimit, VendorResult,
                      VendorWorkPackage)
from ..n2.service import N2ReleaseRow
from ..repository import Repository, chain_hash, dump, new_id
from ..rules import canonical_json, normalize_revision, revision_order, sha256_hex
from ..scanning import sniff_mime, validate_upload
from ..security import Principal
from ..storage import make_uri
from ..vendor_contracts import authorize_work_package, ingest_vendor_result, result_checksum
from . import APPROVAL_SCOPE, engine, parsers
from .config import N3Settings
from .db import (N3ActionRow, N3AssessmentRow, N3BaselineRow, N3DeliveryRow, N3EventRow, N3FeedbackRow, N3FileRow,
                 N3JobRow, N3ReleaseRow, N3ReviewRow, N3RouteRow, N3VendorRow, N3WorkPackageRow)
from .models import (KIND_REQUIREMENTS, KIND_SCOPE, SCOPE_PACKAGES, TOOL_KINDS, BaselineRequest, FeedbackRequest,
                     N3Action, N3ActionRequest, N3Assessment, N3DesignBaseline, N3Feedback, N3FileEvidence,
                     N3FileManifest, N3Release, N3ReleasePackage, N3Review, N3ReviewRequest, N3Routing, N3ToolJob,
                     N3VendorDelivery, N3VendorProfile, N3WorkPackage, N3WorkPackageRequest, PlanRequest,
                     ReleaseRequest, RouteRequest, StatusView, ToolJobRequest)
from .tool_runner import OPERATIONS, JobWorker, ToolError, ToolRunner

READ_ONLY_ROLES = ("design_engineer", "design_authority", "auditor")


def _with_hash(model, field: str):
    body = {k: v for k, v in dump(model).items() if k != field}
    return model.model_copy(update={field: sha256_hex(canonical_json(body))})


class N3Service:
    def __init__(self, state, settings: N3Settings, runner: Optional[ToolRunner] = None) -> None:
        self.db, self.store, self.pipeline, self.clock = state.db, state.store, state.pipeline, state.clock
        self.scanner, self.n0_settings, self.n2 = state.scanner, state.settings, state.n2
        self.settings = settings
        self.runner = runner or ToolRunner(settings)
        self.worker = JobWorker(settings.max_concurrent_jobs, settings.jobs_inline)
        self.n2.release_listeners.append(self.on_prd_release)
        self.release_listeners: list[Callable[[N3Release], None]] = []      # downstream nodes (e.g. N4)

    # ==========================================================================================
    # helpers
    # ==========================================================================================
    @staticmethod
    def _svc(p: Principal) -> Principal:
        return Principal(principal_id=f"svc-n3@{p.tenant_id}", tenant_id=p.tenant_id, roles=["node_service"],
                         assigned_roles=["service"], nodes=["N3"], clearance=p.clearance, auth_method="internal")

    @staticmethod
    def _audit(s, p: Principal, event_type: str, subject: str, details: dict) -> None:
        Repository(s).append_audit(tenant_id=p.tenant_id, event_type=event_type, actor_id=p.principal_id,
                                   actor_roles=p.roles, subject=subject, details=details)

    def _event(self, s, tenant: str, kind: str, subject_id: str, status: str, actor: str, **details: Any) -> None:
        s.add(N3EventRow(event_id=new_id("N3E"), tenant_id=tenant, subject_kind=kind, subject_id=subject_id,
                         status=status, record={"status": status, "actor": actor, "at": self.clock().isoformat(),
                                                **details}))
        s.flush()

    @staticmethod
    def _events(s, tenant: str, kind: str, subject_id: str) -> list[dict]:
        rows = s.scalars(select(N3EventRow).where(N3EventRow.tenant_id == tenant, N3EventRow.subject_kind == kind,
                                                  N3EventRow.subject_id == subject_id)
                         .order_by(N3EventRow.created_at, N3EventRow.event_id))
        return [r.record for r in rows]

    def _invalidated(self, s, tenant: str, kind: str, subject_id: str) -> Optional[dict]:
        return next((e for e in self._events(s, tenant, kind, subject_id) if e["status"] == "INVALIDATED"), None)

    @staticmethod
    def _get(s, row_cls, model, tenant: str, key, code: str):
        row = s.get(row_cls, key)
        if row is None or row.tenant_id != tenant:
            raise N0Error(404, code, f"{key} not found in this tenant")
        return model.model_validate(row.record)

    def _baseline(self, s, tenant: str, baseline_id: str) -> N3DesignBaseline:
        return self._get(s, N3BaselineRow, N3DesignBaseline, tenant, baseline_id, "BASELINE_NOT_FOUND")

    def _wp(self, s, tenant: str, wp_id: str) -> N3WorkPackage:
        return self._get(s, N3WorkPackageRow, N3WorkPackage, tenant, wp_id, "WORK_PACKAGE_NOT_FOUND")

    def _active(self, tenant: str, baseline_id: str) -> N3DesignBaseline:
        """Invariant 2: refuse work on a baseline whose PRD was revised (lazy check, own session)."""
        with self.db.session() as s:
            b = self._baseline(s, tenant, baseline_id)
            inv = self._invalidated(s, tenant, "baseline", baseline_id)
            if inv is None:
                revs = [r.revision for r in s.scalars(select(N2ReleaseRow).where(
                    N2ReleaseRow.tenant_id == tenant, N2ReleaseRow.prd_id == b.prd_id))]
                latest = max(revs, key=revision_order) if revs else b.prd_revision
        if inv is not None:
            raise N0Error(409, "BASELINE_INVALIDATED", f"baseline {baseline_id} was invalidated: {inv.get('reason')}",
                          baseline_id=baseline_id)
        if revision_order(latest) > revision_order(b.prd_revision):
            self.on_prd_release(tenant, b.prd_id, latest)
            raise N0Error(409, "BASELINE_INVALIDATED", f"PRD {b.prd_id} was revised to {latest}; baseline "
                                                       f"{baseline_id} (revision {b.prd_revision}) is no longer valid",
                          baseline_id=baseline_id)
        return b

    def _active_wp(self, tenant: str, wp_id: str) -> tuple[N3WorkPackage, N3DesignBaseline]:
        with self.db.session() as s:
            wp = self._wp(s, tenant, wp_id)
        return wp, self._active(tenant, wp.baseline_id)

    # ==========================================================================================
    # N3.1 requirements intake → baseline lock
    # ==========================================================================================
    def lock_baseline(self, p: Principal, req: BaselineRequest) -> N3DesignBaseline:
        t = p.tenant_id
        rels = self.n2.releases(t, req.prd_id)
        if not rels:
            raise N0Error(409, "PRD_NOT_RELEASED", f"{req.prd_id} has no N2 PRDRelease; N3 ingests only approved PRDs")
        latest = max(rels, key=lambda r: revision_order(r.prd_revision))
        want = normalize_revision(req.prd_revision) or req.prd_revision if req.prd_revision else latest.prd_revision
        rel = next((r for r in reversed(rels) if r.prd_revision == want), None)
        if rel is None:
            raise N0Error(404, "PRD_RELEASE_NOT_FOUND", f"{req.prd_id}@{want} was never released by N2")
        if revision_order(rel.prd_revision) < revision_order(latest.prd_revision):
            raise N0Error(409, "PRD_SUPERSEDED", f"{req.prd_id}@{rel.prd_revision} is superseded by "
                                                 f"@{latest.prd_revision}")
        if "N3" not in rel.target_nodes:
            raise N0Error(409, "PRD_NOT_RELEASED_TO_N3", f"release {rel.release_id} does not target N3")
        with self.db.session() as s:
            repo = Repository(s)
            row = repo.get_artifact(t, rel.n0_artifact_id, rel.prd_revision)
            if row is None or row.artifact_type != "PRD":
                raise N0Error(409, "PRD_NOT_ADMITTED", f"{rel.n0_artifact_id}@{rel.prd_revision} is not an N0 PRD")
            d = repo.latest_decision(t, row.artifact_id, row.revision)
            if d is None or d.decision != "PASS":
                raise N0Error(409, "PRD_NOT_PASS", f"N0 decision is {d.decision if d else 'missing'}")
            top = repo.latest_revision(t, row.artifact_id)
            if top is not None and top.revision_order > row.revision_order:
                raise N0Error(409, "PRD_SUPERSEDED", f"N0 holds a newer PRD revision @{top.revision}")
            problems = self._verify_prd_release(repo, row, rel)
            if problems:
                raise N0Error(409, "PRD_SIGNATURE_INVALID", "; ".join(problems), release_id=rel.release_id)
            src = repo.get_source(t, req.design_source_id)
            if src is None or src.approval_status != "APPROVED":
                raise N0Error(409, "DESIGN_SOURCE_NOT_APPROVED", f"design source {req.design_source_id} is "
                                                                 f"{src.approval_status if src else 'not registered'}")
            if "DESIGN_ENGINEERING" not in src.permitted_use:
                raise N0Error(409, "DESIGN_SOURCE_PURPOSE", f"{req.design_source_id} does not permit "
                                                            "DESIGN_ENGINEERING")
            for brow in s.scalars(select(N3BaselineRow).where(N3BaselineRow.tenant_id == t,
                                                              N3BaselineRow.prd_id == req.prd_id,
                                                              N3BaselineRow.prd_revision == rel.prd_revision)):
                b = N3DesignBaseline.model_validate(brow.record)
                if b.design_source_id == req.design_source_id and b.project_id == req.project_id and \
                        self._invalidated(s, t, "baseline", b.baseline_id) is None:
                    return b                                     # idempotent: one active lock per PRD revision
            canon = row.canonical["payload"]
            baseline = N3DesignBaseline(
                baseline_id=new_id("N3B"), tenant_id=t, prd_id=req.prd_id,
                prd_artifact=f"{row.artifact_id}@{row.revision}", prd_revision=row.revision,
                prd_payload_sha256=row.payload_sha256, prd_release_id=rel.release_id, prd_manifest_id=rel.manifest_id,
                prd_approval_id=rel.approval_id, prd_approval_hash=rel.approval_event_hash,
                project_id=req.project_id, design_source_id=req.design_source_id,
                product_name=canon["product_name"], design_scopes=canon["design_scopes"],
                constraints=DesignConstraints.model_validate(canon["design_constraints"]),
                requirements=[Requirement.model_validate(r) for r in canon["requirements"]],
                test_limits=[TestLimit.model_validate(x) for x in canon.get("test_limits", [])],
                synthetic=row.synthetic, locked_by=p.principal_id, locked_at=self.clock())
            baseline = _with_hash(baseline, "baseline_sha256")
            s.add(N3BaselineRow(baseline_id=baseline.baseline_id, tenant_id=t, prd_id=req.prd_id,
                                prd_revision=row.revision, prd_payload_sha256=row.payload_sha256,
                                record=dump(baseline)))
            repo.add_edge(t, f"artifact:{baseline.prd_artifact}", f"n3_baseline:{baseline.baseline_id}", "LOCKED_BY",
                          prd_release_id=rel.release_id)
            self._event(s, t, "baseline", baseline.baseline_id, "LOCKED", p.principal_id,
                        prd_artifact=baseline.prd_artifact)
            self._audit(s, p, "N3_BASELINE_LOCKED", f"n3_baseline:{baseline.baseline_id}",
                        {"prd_artifact": baseline.prd_artifact, "prd_payload_sha256": baseline.prd_payload_sha256,
                         "baseline_sha256": baseline.baseline_sha256})
        return baseline

    def _verify_prd_release(self, repo: Repository, row, rel) -> list[str]:
        t, problems = row.tenant_id, []
        if row.payload_sha256 != rel.payload_sha256:
            problems.append("PRDRelease payload_sha256 differs from the admitted N0 artifact")
        blob = repo.find_blob(t, row.payload_sha256, "PAYLOAD")
        if blob is None or sha256_hex(self.store.get(blob.bucket, blob.object_key)) != row.payload_sha256:
            problems.append("stored PRD payload bytes do not match their SHA-256")
        m = next((m for m in repo.releases(t, row.artifact_id, row.revision) if m.manifest_id == rel.manifest_id), None)
        if m is None or m.status != "RELEASED" or "N3" not in m.target_nodes:
            problems.append(f"manifest {rel.manifest_id} is not an N0 release of this revision to N3")
        elif m.payload_sha256 != rel.payload_sha256 or m.approval_id != rel.approval_id:
            problems.append("manifest payload hash / approval do not match the PRDRelease")
        arow = repo.s.get(ApprovalRow, rel.approval_id)
        if arow is None or arow.tenant_id != t:
            problems.append(f"approval {rel.approval_id} not found")
        else:
            rec = dict(arow.record)
            stated = rec.pop("event_hash", None)
            if stated != rel.approval_event_hash or chain_hash(rec.get("prev_hash", ""), rec) != stated:
                problems.append("approval event hash does not verify against the approval chain")
            if rec.get("decision") != "APPROVED" or rec.get("scope") != "RELEASE" or \
                    rec.get("artifact_id") != row.artifact_id or rec.get("exact_revision") != row.revision:
                problems.append("approval is not an APPROVED RELEASE of this PRD revision")
            if rec.get("approver_id") == row.submitted_by:
                problems.append("PRD was approved by its submitter")
        return problems

    # ==========================================================================================
    # N3.2 engineering planning
    # ==========================================================================================
    def _new_wp(self, s, p: Principal, b: N3DesignBaseline, kind: str, budget, title: Optional[str] = None,
                due=None, notes: Optional[str] = None) -> N3WorkPackage:
        if budget.amount <= 0:
            raise N0Error(422, "BUDGET_INVALID", "budget_cap must be positive")
        req = KIND_REQUIREMENTS[kind]
        crit = engine.acceptance_criteria(kind, b.constraints) + ([f"notes: {notes}"] if notes else [])
        wp = N3WorkPackage(work_package_id=new_id("N3WP"), tenant_id=b.tenant_id, baseline_id=b.baseline_id,
                           prd_artifact=b.prd_artifact, scope=KIND_SCOPE[kind], kind=kind,
                           title=title or f"{b.product_name}: {kind.replace('_', ' ').lower()}",
                           required_native_roles=req["native"], required_evidence_roles=req["evidence"],
                           acceptance_criteria=crit, budget_cap=budget, due_date=due, created_by=p.principal_id,
                           created_at=self.clock())
        wp = _with_hash(wp, "package_sha256")
        s.add(N3WorkPackageRow(work_package_id=wp.work_package_id, tenant_id=wp.tenant_id, baseline_id=b.baseline_id,
                               kind=kind, package_sha256=wp.package_sha256, record=dump(wp)))
        Repository(s).add_edge(b.tenant_id, f"n3_baseline:{b.baseline_id}", f"n3_work_package:{wp.work_package_id}",
                               "PLANNED", kind=kind)
        self._event(s, b.tenant_id, "work_package", wp.work_package_id, "PLANNED", p.principal_id)
        self._audit(s, p, "N3_WORK_PACKAGE_CREATED", f"n3_work_package:{wp.work_package_id}",
                    {"kind": kind, "package_sha256": wp.package_sha256})
        return wp

    def plan(self, p: Principal, baseline_id: str, req: PlanRequest) -> list[N3WorkPackage]:
        b = self._active(p.tenant_id, baseline_id)
        scopes = req.scopes or b.design_scopes
        bad = [x for x in scopes if x not in b.design_scopes]
        if bad:
            raise N0Error(422, "SCOPE_NOT_IN_PRD", f"{bad} are not design scopes of {b.prd_artifact}")
        out = []
        with self.db.session() as s:
            existing = {r.kind: N3WorkPackage.model_validate(r.record) for r in s.scalars(
                select(N3WorkPackageRow).where(N3WorkPackageRow.tenant_id == p.tenant_id,
                                               N3WorkPackageRow.baseline_id == baseline_id)
                .order_by(N3WorkPackageRow.created_at))}
            for scope in scopes:
                for kind in SCOPE_PACKAGES[scope]:
                    out.append(existing.get(kind) or self._new_wp(s, p, b, kind, req.budget_per_package))
        return out

    def create_work_package(self, p: Principal, req: N3WorkPackageRequest) -> N3WorkPackage:
        b = self._active(p.tenant_id, req.baseline_id)
        if KIND_SCOPE[req.kind] not in b.design_scopes:
            raise N0Error(422, "SCOPE_NOT_IN_PRD", f"{req.kind} belongs to {KIND_SCOPE[req.kind]}, which "
                                                   f"{b.prd_artifact} does not require")
        with self.db.session() as s:
            return self._new_wp(s, p, b, req.kind, req.budget_cap, req.title, req.due_date, req.acceptance_notes)

    def _latest_wps(self, s, tenant: str, baseline_id: str) -> dict[str, N3WorkPackage]:
        out = {}
        for r in s.scalars(select(N3WorkPackageRow).where(N3WorkPackageRow.tenant_id == tenant,
                                                          N3WorkPackageRow.baseline_id == baseline_id)
                           .order_by(N3WorkPackageRow.created_at)):
            out[r.kind] = N3WorkPackage.model_validate(r.record)
        return out

    # ==========================================================================================
    # N3.3 vendor / tool routing
    # ==========================================================================================
    def onboard_vendor(self, p: Principal, profile: N3VendorProfile) -> N3VendorProfile:
        if profile.tenant_id != p.tenant_id:
            raise N0Error(403, "CROSS_TENANT_REFERENCE", "cross-tenant access is not permitted")
        with self.db.session() as s:
            n = len(list(s.scalars(select(N3VendorRow).where(N3VendorRow.tenant_id == p.tenant_id,
                                                             N3VendorRow.vendor_id == profile.vendor_id))))
            prof = profile.model_copy(update={"onboarded_by": p.principal_id, "onboarded_at": self.clock()})
            s.add(N3VendorRow(tenant_id=p.tenant_id, vendor_id=prof.vendor_id, version=n + 1, record=dump(prof)))
            self._audit(s, p, "N3_VENDOR_ONBOARDED", f"n3_vendor:{prof.vendor_id}",
                        {"version": n + 1, "capabilities": prof.capabilities, "nda_on_file": prof.nda_on_file})
        return prof

    @staticmethod
    def _vendor(s, tenant: str, vendor_id: str) -> Optional[N3VendorProfile]:
        row = s.scalars(select(N3VendorRow).where(N3VendorRow.tenant_id == tenant, N3VendorRow.vendor_id == vendor_id)
                        .order_by(N3VendorRow.version.desc())).first()
        return N3VendorProfile.model_validate(row.record) if row else None

    @staticmethod
    def _routes(s, tenant: str, wp_id: str) -> list[N3Routing]:
        return [N3Routing.model_validate(r.record) for r in s.scalars(
            select(N3RouteRow).where(N3RouteRow.tenant_id == tenant, N3RouteRow.work_package_id == wp_id)
            .order_by(N3RouteRow.created_at))]

    def route(self, p: Principal, wp_id: str, req: RouteRequest) -> N3Routing:
        wp, b = self._active_wp(p.tenant_id, wp_id)
        reasons: list[str] = []
        if req.budget.amount <= 0:
            reasons.append("BUDGET_INVALID: budget must be positive")
        if req.budget.currency != wp.budget_cap.currency or req.budget.amount > wp.budget_cap.amount:
            reasons.append(f"BUDGET_EXCEEDS_PACKAGE_CAP: {req.budget.amount} {req.budget.currency} > "
                           f"{wp.budget_cap.amount} {wp.budget_cap.currency}")
        route_id, vwp_id = new_id("N3RT"), None
        with self.db.session() as s:
            if req.executor == "TOOL":
                if req.tool is None:
                    raise N0Error(422, "TOOL_REQUIRED", "executor TOOL needs a tool")
                if wp.kind not in TOOL_KINDS[req.tool]:
                    reasons.append(f"TOOL_CANNOT_EXECUTE: {req.tool} cannot produce {wp.kind}; route it to a vendor")
                if self.runner.paths.get(req.tool) is None:
                    reasons.append(f"TOOL_UNAVAILABLE: {req.tool} is not installed on this N3 worker")
            else:
                if not req.vendor_id:
                    raise N0Error(422, "VENDOR_REQUIRED", "executor VENDOR needs a vendor_id")
                v = self._vendor(s, p.tenant_id, req.vendor_id)
                if v is None:
                    raise N0Error(404, "VENDOR_NOT_ONBOARDED", f"{req.vendor_id} is not an onboarded N3 vendor")
                if wp.kind not in v.capabilities:
                    reasons.append(f"VENDOR_NOT_QUALIFIED: {v.vendor_id} is not qualified for {wp.kind}")
                if not v.nda_on_file:
                    reasons.append(f"NDA_MISSING: no NDA on file for {v.vendor_id}")
                if req.budget.currency != v.max_budget.currency or req.budget.amount > v.max_budget.amount:
                    reasons.append(f"BUDGET_EXCEEDS_VENDOR_LIMIT: {v.vendor_id} limit is {v.max_budget.amount} "
                                   f"{v.max_budget.currency}")
                if not reasons:
                    vwp_id = f"VWP-{route_id}"
                    vwp = VendorWorkPackage(
                        work_package_id=vwp_id, tenant_id=p.tenant_id, project_id=b.project_id, vendor_id=v.vendor_id,
                        connector_type=req.connector_type, approved_input_ids=[b.prd_artifact], budget_cap=req.budget,
                        expected_outputs=[ExpectedOutput(name=r, format="native" if r in wp.required_native_roles
                                                         else "evidence")
                                          for r in wp.required_native_roles + wp.required_evidence_roles])
                    td = authorize_work_package(Repository(s), p, vwp, self.clock())
                    if not td.authorized:
                        reasons += [f"{x.code}: {x.message}" for x in td.violated_rules]
            r = N3Routing(route_id=route_id, tenant_id=p.tenant_id, work_package_id=wp_id, executor=req.executor,
                          tool=req.tool, vendor_id=req.vendor_id, budget=req.budget, vendor_work_package_id=vwp_id,
                          authorized=not reasons, reasons=reasons, routed_by=p.principal_id, routed_at=self.clock())
            s.add(N3RouteRow(route_id=route_id, tenant_id=p.tenant_id, work_package_id=wp_id, authorized=r.authorized,
                             record=dump(r)))
            if r.authorized:
                self._event(s, p.tenant_id, "work_package", wp_id, "ROUTED", p.principal_id, route_id=route_id,
                            executor=req.executor, tool=req.tool, vendor_id=req.vendor_id)
            self._audit(s, p, "N3_ROUTED" if r.authorized else "N3_ROUTE_REFUSED", f"n3_work_package:{wp_id}",
                        {"route_id": route_id, "reasons": reasons, "vendor_work_package_id": vwp_id})
        return r

    # ==========================================================================================
    # N3.4 execution: controlled intake and tool jobs
    # ==========================================================================================
    def _store_blob(self, repo: Repository, tenant: str, source_id: str, name: str, data: bytes, digest: str,
                    declared: Optional[str], mime: str, scan: dict, actor: str) -> BlobRow:
        existing = repo.find_blob(tenant, digest, "UPLOAD")
        if existing is not None:
            return existing
        key = self.store.key_for(tenant, digest)
        self.store.put_immutable("quarantine", key, data)
        row = repo.add_blob(BlobRow(blob_id=new_id("BLB"), tenant_id=tenant, source_id=source_id, kind="UPLOAD",
                                    sha256=digest, size_bytes=len(data), filename=posixpath.basename(name)[:512],
                                    declared_mime=declared, sniffed_mime=mime, status="SCAN_PASSED", scan=scan,
                                    bucket="quarantine", object_key=key, created_by=actor))
        repo.add_edge(tenant, f"source:{source_id}", f"blob:{row.blob_id}", "SUPPLIED", sha256=digest)
        return row

    def deliver(self, p: Principal, wp_id: str, manifest_json: str,
                files: list[tuple[str, Optional[str], bytes]]) -> N3VendorDelivery:
        wp, b = self._active_wp(p.tenant_id, wp_id)
        t = p.tenant_id
        if p.has("vendor_submitter") and not p.has("design_engineer"):
            origin = "VENDOR"
            with self.db.session() as s:
                routes = [r for r in self._routes(s, t, wp_id) if r.authorized and r.executor == "VENDOR"]
            if not p.vendor_id or not any(r.vendor_id == p.vendor_id for r in routes):
                raise N0Error(403, "VENDOR_NOT_ROUTED", f"{p.principal_id} (vendor {p.vendor_id}) is not routed to "
                                                        f"{wp_id}")
            route = next(r for r in reversed(routes) if r.vendor_id == p.vendor_id)
        else:
            origin, route = "ENGINEER", None
        try:
            manifest = N3FileManifest.model_validate_json(manifest_json)
        except ValueError as exc:
            raise N0Error(422, "MANIFEST_INVALID", str(exc)[:2000]) from None
        if len(files) > self.settings.max_delivery_files:
            raise N0Error(413, "TOO_MANY_FILES", f"at most {self.settings.max_delivery_files} files per delivery")
        codes: list[str] = []
        problems: list[str] = []

        def bad(code: str, msg: str) -> None:
            codes.append(code)
            problems.append(msg)

        rev = normalize_revision(manifest.design_revision)
        if rev is None:
            bad("REVISION_FORMAT", f"design_revision {manifest.design_revision!r} is not A..ZZZ or a number")
        by_path: dict[str, Any] = {}
        for e in manifest.entries:
            if e.path in by_path:
                bad("MANIFEST_DUPLICATE_PATH", f"{e.path} appears twice in the manifest")
            by_path[e.path] = e
        accepted, seen = [], set()
        for raw_name, ctype, data in files:
            name = (raw_name or "").replace("\\", "/").lstrip("/")
            if not name or ".." in name.split("/"):
                bad("UPLOAD_REJECTED", f"{raw_name!r} is not an acceptable file name")
                continue
            if name in seen:
                bad("DUPLICATE_FILE", f"{name} uploaded twice")
                continue
            seen.add(name)
            entry = by_path.get(name)
            if entry is None:
                bad("UNDECLARED_FILE", f"{name} is not in the manifest")
                continue
            digest = sha256_hex(data)
            if digest != entry.sha256:
                bad("SHA256_MISMATCH", f"{name}: received {digest[:12]}…, manifest says {entry.sha256[:12]}…")
                continue
            screen = validate_upload(data, posixpath.basename(name), ctype, self.n0_settings.max_upload_bytes)
            if not screen.ok:
                bad("UPLOAD_REJECTED", f"{name}: {'; '.join(screen.problems)}")
                continue
            scan = self.scanner.scan(data)
            if not scan.clean:
                bad("MALWARE_DETECTED", f"{name}: {scan.engine} flagged {scan.signature}")
                continue
            det = parsers.detect(name, data)
            if det.problems:
                bad("INVALID_NATIVE_FILE", "; ".join(det.problems))
                continue
            if entry.role != det.role and entry.role != "OTHER":
                bad("ROLE_MISMATCH", f"{name}: manifest role {entry.role}, content is {det.role}")
                continue
            if det.tool_version and entry.tool_version and not (
                    entry.tool_version.startswith(det.tool_version) or det.tool_version.startswith(entry.tool_version)):
                bad("TOOL_VERSION_MISMATCH", f"{name}: file says {det.tool_version}, manifest {entry.tool_version}")
                continue
            accepted.append((name, ctype, data, digest, det, entry, screen.sniffed_mime, scan))
        for missing in sorted(set(by_path) - seen):
            bad("MISSING_FILE", f"{missing} is in the manifest but was not uploaded")
        native = [x for x in accepted if x[4].native]
        if accepted and all(x[4].role in ("DOCUMENT", "IMAGE") for x in accepted):
            bad("NATIVE_FILES_MISSING", "PDF / screenshot-only deliveries are rejected; deliver native editable "
                                        "sources (.kicad_sch, .kicad_pcb, .step, SPICE netlists)")
        elif manifest.replaces_previous and not native:
            bad("NATIVE_FILES_MISSING", "a complete delivery (replaces_previous=true) must contain native sources")
        delivery_id = new_id("N3DLV")
        evidence: list[N3FileEvidence] = []
        vr_id = None
        with self.db.session() as s:
            repo = Repository(s)
            if not codes:
                for x in accepted:
                    blob = repo.find_blob(t, x[3], "UPLOAD")
                    if blob is not None and blob.source_id != b.design_source_id:
                        bad("SOURCE_MISMATCH", f"{x[0]}: identical bytes were uploaded under source {blob.source_id}")
            status = "REJECTED" if codes else "ACCEPTED"
            if status == "ACCEPTED":
                for name, ctype, data, digest, det, entry, mime, scan in accepted:
                    blob = self._store_blob(repo, t, b.design_source_id, name, data, digest, ctype, mime,
                                            {"engine": scan.engine, "real_engine": scan.real_engine, "clean": True},
                                            p.principal_id)
                    ev = N3FileEvidence(path=name, role=det.role, sha256=digest, blob_id=blob.blob_id,
                                        uri=make_uri(blob.bucket, blob.object_key), size_bytes=len(data),
                                        native=det.native, detected_format=det.fmt, tool=det.tool or entry.tool,
                                        tool_version=det.tool_version or entry.tool_version, revision=det.revision,
                                        origin=origin, delivery_id=delivery_id)
                    evidence.append(ev)
                    s.add(N3FileRow(tenant_id=t, work_package_id=wp_id, sha256=digest, path=name, origin=origin,
                                    record=dump(ev)))
                    repo.add_edge(t, f"n3_work_package:{wp_id}", f"blob:{blob.blob_id}", "DELIVERED",
                                  delivery_id=delivery_id, role=det.role)
                if route is not None and route.vendor_work_package_id:
                    hashes = sorted({e.sha256 for e in evidence})
                    vr = ingest_vendor_result(repo, p, VendorResult(
                        work_package_id=route.vendor_work_package_id, native_file_uris=sorted({e.uri for e in evidence}),
                        result_manifest=manifest.model_dump(mode="json"), checksum_sha256=result_checksum(hashes),
                        tool_version=next((e.tool_version for e in evidence if e.native and e.tool_version), "unknown")))
                    vr_id = vr.result_id
                self._event(s, t, "work_package", wp_id, "DELIVERED", p.principal_id, delivery_id=delivery_id,
                            design_revision=rev)
            d = N3VendorDelivery(delivery_id=delivery_id, tenant_id=t, work_package_id=wp_id,
                                 vendor_id=p.vendor_id if origin == "VENDOR" else None, submitted_by=p.principal_id,
                                 submitted_at=self.clock(), manifest=manifest, files=evidence, status=status,
                                 rejection_codes=sorted(set(codes)), problems=problems, n0_vendor_result_id=vr_id)
            s.add(N3DeliveryRow(delivery_id=delivery_id, tenant_id=t, work_package_id=wp_id, status=status,
                                submitted_by=p.principal_id, record=dump(d)))
            self._audit(s, p, "N3_DELIVERY_" + status, f"n3_work_package:{wp_id}",
                        {"delivery_id": delivery_id, "codes": d.rejection_codes, "files": len(files),
                         "design_revision": manifest.design_revision})
        return d

    # -- current file set -----------------------------------------------------------------------
    def _jobs(self, s, tenant: str, wp_id: Optional[str] = None, job_id: Optional[str] = None) -> dict[str, N3ToolJob]:
        q = select(N3JobRow).where(N3JobRow.tenant_id == tenant)
        q = q.where(N3JobRow.work_package_id == wp_id) if wp_id else q.where(N3JobRow.job_id == job_id)
        out = {}
        for r in s.scalars(q.order_by(N3JobRow.job_id, N3JobRow.seq)):
            out[r.job_id] = N3ToolJob.model_validate(r.record)
        return out

    def _deliveries(self, s, tenant: str, wp_id: str) -> list[N3VendorDelivery]:
        return [N3VendorDelivery.model_validate(r.record) for r in s.scalars(
            select(N3DeliveryRow).where(N3DeliveryRow.tenant_id == tenant, N3DeliveryRow.work_package_id == wp_id)
            .order_by(N3DeliveryRow.created_at))]

    def _current_files(self, s, tenant: str, wp_id: str
                       ) -> tuple[list[N3FileEvidence], Optional[N3VendorDelivery], dict[str, N3ToolJob]]:
        deliveries = {d.delivery_id: d for d in self._deliveries(s, tenant, wp_id)}
        jobs = self._jobs(s, tenant, wp_id=wp_id)
        current: dict[str, N3FileEvidence] = {}
        last, latest = None, None
        for row in s.scalars(select(N3FileRow).where(N3FileRow.tenant_id == tenant, N3FileRow.work_package_id == wp_id)
                             .order_by(N3FileRow.id)):
            ev = N3FileEvidence.model_validate(row.record)
            if ev.origin != "TOOL_JOB" and ev.delivery_id != last:
                last = ev.delivery_id
                latest = deliveries[ev.delivery_id]
                if latest.manifest.replaces_previous:
                    current.clear()
            current[ev.path] = ev
        sources = {e.sha256 for e in current.values() if e.origin != "TOOL_JOB"}
        for path, ev in list(current.items()):                # evidence derived from replaced files is stale
            if ev.origin == "TOOL_JOB" and not set(jobs[ev.job_id].input_sha256) <= sources:
                del current[path]
        return list(current.values()), latest, jobs

    def _read(self, tenant: str, shas: set[str]) -> dict[str, bytes]:
        out = {}
        with self.db.session() as s:
            repo = Repository(s)
            for sha in shas:
                blob = repo.find_blob(tenant, sha, "UPLOAD")
                data = self.store.get(blob.bucket, blob.object_key) if blob else b""
                if blob is None or sha256_hex(data) != sha:
                    raise N0Error(409, "STORED_FILE_CORRUPT", f"stored bytes for {sha[:12]}… do not verify")
                out[sha] = data
        return out

    def submit_job(self, p: Principal, wp_id: str, req: ToolJobRequest) -> N3ToolJob:
        wp, b = self._active_wp(p.tenant_id, wp_id)
        t = p.tenant_id
        if wp.kind not in TOOL_KINDS[req.tool]:
            raise N0Error(409, "TOOL_NOT_APPLICABLE", f"{req.tool} cannot execute a {wp.kind} package")
        if OPERATIONS[req.operation]["tool"] != req.tool:
            raise N0Error(422, "OPERATION_TOOL_MISMATCH", f"{req.operation} is a {OPERATIONS[req.operation]['tool']} "
                                                          "operation")
        with self.db.session() as s:
            if not any(r.authorized and r.executor == "TOOL" and r.tool == req.tool for r in self._routes(s, t, wp_id)):
                raise N0Error(409, "NOT_ROUTED", f"{wp_id} has no authorized route to {req.tool} (N3.3 first)")
            files, _, _ = self._current_files(s, t, wp_id)
        by_sha = {f.sha256: f for f in files if f.origin != "TOOL_JOB"}
        missing = [x for x in req.input_sha256 if x not in by_sha]
        if missing:
            raise N0Error(404, "INPUT_NOT_IN_PACKAGE", f"{[m[:12] for m in missing]} are not current files of {wp_id}")
        corners = list(req.corners or [])
        if req.operation == "spice_corners" and not corners:
            corners, missing_defs = engine.default_corners(b.constraints)
            if missing_defs:
                raise N0Error(409, "CORNER_DEFINITION_INCOMPLETE", f"PRD constraints lack {missing_defs}; worst-case "
                                                                   "corners cannot be derived")
        data = self._read(t, set(req.input_sha256))
        inputs = [(posixpath.basename(by_sha[x].path), data[x]) for x in req.input_sha256]
        try:
            _, steps, _ = self.runner.preflight(req.operation, inputs, corners)
        except ToolError as exc:
            raise N0Error(422 if exc.code not in ("TOOL_UNAVAILABLE", "SANDBOX_UNAVAILABLE") else 503, exc.code,
                          exc.message) from None
        job = N3ToolJob(job_id=new_id("N3JOB"), tenant_id=t, work_package_id=wp_id, tool=req.tool,
                        operation=req.operation, input_sha256=list(req.input_sha256), corners=corners,
                        status="QUEUED", argv=steps,
                        limits=self.runner.limits(), submitted_by=p.principal_id, submitted_at=self.clock())
        with self.db.session() as s:
            s.add(N3JobRow(job_id=job.job_id, seq=0, tenant_id=t, work_package_id=wp_id, status="QUEUED",
                           record=dump(job)))
            self._audit(s, p, "N3_TOOL_JOB_QUEUED", f"n3_job:{job.job_id}",
                        {"tool": req.tool, "operation": req.operation, "inputs": job.input_sha256})
        self.worker.submit(job.job_id, lambda: self._execute(job, inputs, b, p))
        return self.job(t, job.job_id)

    def _execute(self, job: N3ToolJob, inputs: list[tuple[str, bytes]], b: N3DesignBaseline, p: Principal) -> None:
        svc = self._svc(p)
        with self.db.session() as s:
            running = job.model_copy(update={"status": "RUNNING"})
            s.add(N3JobRow(job_id=job.job_id, seq=1, tenant_id=job.tenant_id, work_package_id=job.work_package_id,
                           status="RUNNING", record=dump(running)))
        try:
            res = self.runner.run(job.operation, inputs, job.corners)
            status, version, error = res.status, res.tool_version, res.error
            steps, outputs = res.steps, res.outputs
        except ToolError as exc:
            status, version, error, steps, outputs = "FAILED", None, f"{exc.code}: {exc.message}", [], []
        except Exception as exc:  # pragma: no cover - defensive: a crashed job must not stay RUNNING
            status, version, error, steps, outputs = "FAILED", None, f"{type(exc).__name__}: {exc}", [], []
        evidence, exit_by_path = [], {}
        with self.db.session() as s:
            repo = Repository(s)
            for o in outputs:
                scan = self.scanner.scan(o.data)
                if not scan.clean:
                    error = (error or "") + f" output {o.path} flagged by {scan.engine}; discarded"
                    continue
                digest = sha256_hex(o.data)
                det = parsers.detect(o.path, o.data)
                blob = self._store_blob(repo, job.tenant_id, b.design_source_id, o.path, o.data, digest, None,
                                        sniff_mime(o.data), {"engine": scan.engine, "real_engine": scan.real_engine,
                                                             "clean": True, "produced_by": job.job_id},
                                        svc.principal_id)
                ev = N3FileEvidence(path=o.path, role=det.role, sha256=digest, blob_id=blob.blob_id,
                                    uri=make_uri(blob.bucket, blob.object_key), size_bytes=len(o.data),
                                    native=False, detected_format=det.fmt, tool=job.tool,
                                    tool_version=det.tool_version or version, revision=det.revision,
                                    origin="TOOL_JOB", job_id=job.job_id)
                evidence.append(ev)
                exit_by_path[o.path] = o.exit_code
                s.add(N3FileRow(tenant_id=job.tenant_id, work_package_id=job.work_package_id, sha256=digest,
                                path=o.path, origin="TOOL_JOB", record=dump(ev)))
                repo.add_edge(job.tenant_id, f"n3_job:{job.job_id}", f"blob:{blob.blob_id}", "PRODUCED",
                              tool=job.tool, tool_version=version)
            final = job.model_copy(update={
                "status": status, "tool_version": version, "exit_codes": [x.exit_code for x in steps],
                "outputs": evidence, "output_exit_codes": exit_by_path, "error": (error or None) and error.strip(),
                "finished_at": self.clock(), "limits": {**job.limits, "durations_s": [x.duration_s for x in steps]}})
            s.add(N3JobRow(job_id=job.job_id, seq=2, tenant_id=job.tenant_id, work_package_id=job.work_package_id,
                           status=status, record=dump(final)))
            for inp in job.input_sha256:
                blob = repo.find_blob(job.tenant_id, inp, "UPLOAD")
                if blob is not None:
                    repo.add_edge(job.tenant_id, f"blob:{blob.blob_id}", f"n3_job:{job.job_id}", "INPUT_TO")
            self._audit(s, svc, "N3_TOOL_JOB_" + status, f"n3_job:{job.job_id}",
                        {"tool": job.tool, "tool_version": version, "outputs": len(evidence), "error": final.error})

    def job(self, tenant: str, job_id: str) -> N3ToolJob:
        with self.db.session() as s:
            jobs = self._jobs(s, tenant, job_id=job_id)
        if job_id not in jobs:
            raise N0Error(404, "JOB_NOT_FOUND", f"{job_id} not found in this tenant")
        return jobs[job_id]

    # ==========================================================================================
    # N3.5 verification
    # ==========================================================================================
    def assess(self, p: Principal, wp_id: str) -> N3Assessment:
        wp, b = self._active_wp(p.tenant_id, wp_id)
        t = p.tenant_id
        with self.db.session() as s:
            files, latest, jobs = self._current_files(s, t, wp_id)
        if any(j.status in ("QUEUED", "RUNNING") for j in jobs.values()):
            raise N0Error(409, "JOBS_PENDING", "wait for running tool jobs before assessing")
        data = self._read(t, {f.sha256 for f in files})
        job_meta = {f.sha256: {"exit_code": jobs[f.job_id].output_exit_codes.get(f.path)}
                    for f in files if f.origin == "TOOL_JOB"}
        manifest_rev = normalize_revision(latest.manifest.design_revision) if latest else None
        res = engine.assess(wp, b, files, data.__getitem__, manifest_rev, job_meta)
        a = N3Assessment(assessment_id=new_id("N3A"), tenant_id=t, work_package_id=wp_id, baseline_id=b.baseline_id,
                         scope=wp.scope, kind=wp.kind, design_revision=res.design_revision, files=files,
                         findings=res.findings, decision=engine.decide(res.findings), metrics=res.metrics,
                         engine_version=engine.ENGINE_VERSION, assessed_by=p.principal_id, assessed_at=self.clock())
        a = _with_hash(a, "assessment_sha256")
        with self.db.session() as s:
            s.add(N3AssessmentRow(assessment_id=a.assessment_id, tenant_id=t, work_package_id=wp_id,
                                  decision=a.decision, assessment_sha256=a.assessment_sha256, record=dump(a)))
            repo = Repository(s)
            repo.add_edge(t, f"n3_work_package:{wp_id}", f"n3_assessment:{a.assessment_id}", "ASSESSED_AS",
                          decision=a.decision)
            self._event(s, t, "work_package", wp_id, "ASSESSED", p.principal_id, assessment_id=a.assessment_id,
                        decision=a.decision)
            self._audit(s, p, "N3_ASSESSED", f"n3_assessment:{a.assessment_id}",
                        {"decision": a.decision, "codes": sorted({f.code for f in a.findings if f.outcome != "INFO"}),
                         "assessment_sha256": a.assessment_sha256})
        return a

    def _assessments(self, s, tenant: str, wp_id: str) -> list[N3Assessment]:
        return [N3Assessment.model_validate(r.record) for r in s.scalars(
            select(N3AssessmentRow).where(N3AssessmentRow.tenant_id == tenant, N3AssessmentRow.work_package_id == wp_id)
            .order_by(N3AssessmentRow.created_at, N3AssessmentRow.assessment_id))]

    def _assessment(self, s, tenant: str, assessment_id: str) -> N3Assessment:
        return self._get(s, N3AssessmentRow, N3Assessment, tenant, assessment_id, "ASSESSMENT_NOT_FOUND")

    # ==========================================================================================
    # N3.6 decision support and design-authority gate
    # ==========================================================================================
    def propose_actions(self, p: Principal, assessment_id: str, req: N3ActionRequest) -> N3Action:
        if req.assessment_id != assessment_id:
            raise N0Error(422, "ASSESSMENT_MISMATCH", "body assessment_id differs from the path")
        with self.db.session() as s:
            a = self._assessment(s, p.tenant_id, assessment_id)
        ranked, blocked = engine.rank_actions(a.findings, req.candidates, req.budget_cap)
        act = N3Action(proposal_id=new_id("N3P"), tenant_id=p.tenant_id, assessment_id=assessment_id,
                       assessment_decision=a.decision, ranked=ranked, blocked=blocked, created_by=p.principal_id,
                       created_at=self.clock())
        with self.db.session() as s:
            s.add(N3ActionRow(proposal_id=act.proposal_id, tenant_id=p.tenant_id, assessment_id=assessment_id,
                              record=dump(act)))
            self._audit(s, p, "N3_ACTIONS_PROPOSED", f"n3_assessment:{assessment_id}",
                        {"proposal_id": act.proposal_id, "recommended": [r.action_id for r in ranked if r.recommended],
                         "blocked": [x["action_id"] for x in blocked], "decision_unchanged": True})
        return act

    def _contributors(self, s, tenant: str, a: N3Assessment) -> set[str]:
        deliveries = {d.delivery_id: d for d in self._deliveries(s, tenant, a.work_package_id)}
        jobs = self._jobs(s, tenant, wp_id=a.work_package_id)
        out = set()
        for f in a.files:
            if f.delivery_id and f.delivery_id in deliveries:
                out.add(deliveries[f.delivery_id].submitted_by)
            if f.job_id and f.job_id in jobs:
                out.add(jobs[f.job_id].submitted_by)
        return out

    def review(self, p: Principal, req: N3ReviewRequest) -> N3Review:
        t = p.tenant_id
        with self.db.session() as s:
            a = self._assessment(s, t, req.assessment_id)
        self._active(t, a.baseline_id)
        with self.db.session() as s:
            latest = self._assessments(s, t, a.work_package_id)[-1]
            if latest.assessment_id != a.assessment_id:
                raise N0Error(409, "STALE_ASSESSMENT", f"{a.assessment_id} is superseded by {latest.assessment_id}")
            if req.decision == "APPROVED" and a.decision == "BLOCK":
                raise N0Error(409, "BLOCK_NOT_APPROVABLE", "hard safety / engineering BLOCK findings cannot be "
                                                           "approved; fix them and deliver a new revision",
                              codes=sorted({f.code for f in a.findings if f.outcome == "BLOCK"}))
            if req.decision == "APPROVED" and a.decision == "REWORK":
                raise N0Error(409, "REWORK_OPEN", "the assessment still has REWORK findings",
                              codes=sorted({f.code for f in a.findings if f.outcome == "REWORK"}))
            who = self._contributors(s, t, a) | {a.assessed_by}
            if p.principal_id in who:
                raise N0Error(403, "SEPARATION_OF_DUTIES", "a submitter or assessor cannot approve their own delivery")
            repo = Repository(s)
            ev = repo.append_approval(tenant_id=t, artifact_id=f"n3-assessment:{a.assessment_id}",
                                      exact_revision=a.assessment_sha256[:16], approver_id=p.principal_id,
                                      approver_role="design_authority", decision=req.decision,
                                      scope="EVIDENCE_ATTESTATION", reason=req.reason)
            r = N3Review(review_id=new_id("N3RV"), tenant_id=t, assessment_id=a.assessment_id,
                         work_package_id=a.work_package_id, reviewer=p.principal_id, decision=req.decision,
                         reason=req.reason, approval_event_id=ev.approval_id, reviewed_at=self.clock())
            s.add(N3ReviewRow(review_id=r.review_id, tenant_id=t, assessment_id=a.assessment_id, decision=r.decision,
                              reviewer=p.principal_id, record=dump(r)))
            repo.add_edge(t, f"approval:{ev.approval_id}", f"n3_assessment:{a.assessment_id}",
                          "APPROVES" if req.decision == "APPROVED" else "REJECTS", scope="N3_DESIGN_REVIEW")
            self._event(s, t, "work_package", a.work_package_id, "REVIEW_" + req.decision, p.principal_id,
                        review_id=r.review_id)
            self._audit(s, p, "N3_REVIEW_" + req.decision, f"n3_assessment:{a.assessment_id}",
                        {"review_id": r.review_id, "approval_id": ev.approval_id})
        return r

    def _reviews(self, s, tenant: str, assessment_id: str) -> list[N3Review]:
        return [N3Review.model_validate(r.record) for r in s.scalars(
            select(N3ReviewRow).where(N3ReviewRow.tenant_id == tenant, N3ReviewRow.assessment_id == assessment_id)
            .order_by(N3ReviewRow.created_at))]

    # ==========================================================================================
    # N3.7 digital-thread handoff
    # ==========================================================================================
    def release(self, p: Principal, baseline_id: str, req: ReleaseRequest) -> N3Release:
        t = p.tenant_id
        b = self._active(t, baseline_id)
        if req.scope not in b.design_scopes:
            raise N0Error(422, "SCOPE_NOT_IN_PRD", f"{req.scope} is not a design scope of {b.prd_artifact}")
        with self.db.session() as s:
            wps = self._latest_wps(s, t, baseline_id)
            kinds = SCOPE_PACKAGES[req.scope]
            missing = [k for k in kinds if k not in wps]
            if missing:
                raise N0Error(409, "PACKAGES_MISSING", f"{req.scope} needs work packages {missing}")
            chosen, reviews, problems, who = [], [], [], set()
            for k in kinds:
                hist = self._assessments(s, t, wps[k].work_package_id)
                if not hist:
                    problems.append(f"{k}: not assessed")
                    continue
                a = hist[-1]
                rv = self._reviews(s, t, a.assessment_id)
                if a.decision != "PASS":
                    problems.append(f"{k}: latest assessment {a.assessment_id} is {a.decision}")
                elif not rv or rv[-1].decision != "APPROVED":
                    problems.append(f"{k}: assessment {a.assessment_id} has no design-authority approval")
                else:
                    chosen.append(a)
                    reviews.append(rv[-1])
                    who |= self._contributors(s, t, a)
            if problems:
                raise N0Error(409, "PACKAGE_NOT_APPROVED", "; ".join(problems))
            revs = {a.design_revision for a in chosen}
            if len(revs) != 1 or None in revs:
                raise N0Error(409, "DESIGN_REVISION_INCONSISTENT", f"approved packages carry design revisions "
                                                                   f"{sorted(str(r) for r in revs)}")
            if p.principal_id in who:
                raise N0Error(403, "SEPARATION_OF_DUTIES", "a submitter cannot release their own delivery")
        design_rev = revs.pop()
        review_ids = [r.review_id for r in reviews]
        payload_release_id = "N3R-" + sha256_hex(canonical_json(
            {"baseline": baseline_id, "scope": req.scope, "revision": design_rev,
             "assessments": [a.assessment_id for a in chosen], "reviews": review_ids}))[:24]
        packages = []
        stage = "CONCEPT" if b.synthetic else "PROTOTYPE"
        if "N4" in req.targets:
            payload = engine.design_release_payload(payload_release_id, req.scope, b, chosen, review_ids, design_rev)
            refs = self._file_refs(t, [f for f in payload["files"]
                                       if NATIVE_EXTENSIONS.get(posixpath.splitext(f["path"].lower())[1])])
            art = Artifact(tenant_id=t, project_id=b.project_id, artifact_id=f"DSN-{b.prd_id}-{req.scope}",
                           artifact_type="DESIGN_RELEASE", source_id=b.design_source_id, processing_purpose="VERIFICATION",
                           release_stage=stage, revision=design_rev, synthetic=b.synthetic,
                           source_sha256=refs[0].sha256 if refs else sha256_hex(canonical_json(payload)),
                           file_refs=refs, payload=payload, source_artifact_ids=[b.prd_artifact])
            packages.append(self._submit_and_release(p, art, "N4", req.scope))
        if "N5" in req.targets:
            bom = engine.bom_release_payload(payload_release_id, req.scope, b, chosen, design_rev)
            if bom is None:
                packages.append(N3ReleasePackage(target="N5", artifact_type="BOM_RELEASE",
                                                 artifact_id=f"BOM-{b.prd_id}-{req.scope}", revision=design_rev,
                                                 status="NOT_APPLICABLE",
                                                 reason=f"no engineering BOM was delivered for {req.scope}"))
            else:
                art = Artifact(tenant_id=t, project_id=b.project_id, artifact_id=f"BOM-{b.prd_id}-{req.scope}",
                               artifact_type="BOM_RELEASE", source_id=b.design_source_id, processing_purpose="SOURCING",
                               release_stage=stage, revision=design_rev, synthetic=b.synthetic,
                               source_sha256=sha256_hex(canonical_json(bom)), payload=bom,
                               source_artifact_ids=[b.prd_artifact])
                packages.append(self._submit_and_release(p, art, "N5", req.scope))
        applicable = [x for x in packages if x.status != "NOT_APPLICABLE"]
        released = [x for x in applicable if x.status == "RELEASED"]
        status = "RELEASED" if applicable and len(released) == len(applicable) else \
            ("PARTIAL" if released else "REFUSED")
        rel = N3Release(release_id=new_id("N3REL"), tenant_id=t, baseline_id=baseline_id, scope=req.scope,
                        prd_artifact=b.prd_artifact, design_revision=design_rev, payload_release_id=payload_release_id,
                        status=status, work_package_ids=[wps[k].work_package_id for k in SCOPE_PACKAGES[req.scope]],
                        assessment_ids=[a.assessment_id for a in chosen], review_ids=review_ids, packages=packages,
                        released_by=p.principal_id, released_at=self.clock())
        with self.db.session() as s:
            s.add(N3ReleaseRow(release_id=rel.release_id, tenant_id=t, baseline_id=baseline_id, scope=req.scope,
                               record=dump(rel)))
            self._event(s, t, "release", rel.release_id, status, p.principal_id)
            self._audit(s, p, "N3_RELEASE_" + status, f"n3_release:{rel.release_id}",
                        {"scope": req.scope, "design_revision": design_rev,
                         "packages": [{"target": x.target, "status": x.status, "manifest_id": x.manifest_id,
                                       "codes": x.violated_codes} for x in packages]})
        if rel.status in ("RELEASED", "PARTIAL"):
            for listener in self.release_listeners:
                listener(rel)
        return rel

    def _file_refs(self, tenant: str, files: list[dict]) -> list[FileRef]:
        refs = []
        with self.db.session() as s:
            repo = Repository(s)
            for f in files:
                blob = repo.find_blob(tenant, f["sha256"], "UPLOAD")
                refs.append(FileRef(uri=make_uri("quarantine", blob.object_key), sha256=blob.sha256,
                                    mime=blob.sniffed_mime, size_bytes=blob.size_bytes, blob_id=blob.blob_id))
        return refs

    def _submit_and_release(self, p: Principal, art: Artifact, target: str, scope: str) -> N3ReleasePackage:
        res = self.pipeline.evaluate(self._svc(p), EvaluateRequest(artifact=art, origin_node="N3",
                                                                   requested_destinations=[target]))
        d = res.decision
        rev = res.canonical_artifact.revision if res.canonical_artifact else art.revision
        base = dict(target=target, artifact_type=art.artifact_type, artifact_id=art.artifact_id, revision=rev,
                    n0_decision=d.decision, n0_decision_id=d.decision_id,
                    violated_codes=sorted({v.code for v in d.violated_rules}))
        if d.decision != "PASS":
            return N3ReleasePackage(**base, status="REFUSED", reason=f"N0 decision {d.decision}: {d.explanation}"[:1000])
        try:
            with self.db.session() as s:
                repo = Repository(s)
                ap = record_approval(repo, p, ApprovalRequest(
                    artifact_id=art.artifact_id, exact_revision=rev, scope="RELEASE", decision="APPROVED",
                    reason=f"N3 design-authority release of {scope} revision {rev} to {target}"), self.clock(),
                    self.store)
                m = release_handoff(repo, p, HandoffReleaseRequest(
                    artifact_id=art.artifact_id, revision=rev, source_node="N3", target_nodes=[target],
                    approval_id=ap.approval_event.approval_id), self.clock())
        except N0Error as err:
            return N3ReleasePackage(**base, status="REFUSED", reason=f"{err.code}: {err.message}"[:1000])
        return N3ReleasePackage(**base, status="RELEASED", manifest_id=m.manifest_id,
                                approval_id=ap.approval_event.approval_id,
                                reason=f"released to {target} via N0 manifest {m.manifest_id}. {APPROVAL_SCOPE}")

    # ==========================================================================================
    # revision drift (invariant 2)
    # ==========================================================================================
    def on_prd_release(self, tenant: str, prd_id: str, revision: str) -> list[str]:
        """N2 listener: a newer PRD revision invalidates older baselines and everything built on them."""
        done = []
        with self.db.session() as s:
            for row in s.scalars(select(N3BaselineRow).where(N3BaselineRow.tenant_id == tenant,
                                                             N3BaselineRow.prd_id == prd_id)):
                if revision_order(row.prd_revision) >= revision_order(revision) or \
                        self._invalidated(s, tenant, "baseline", row.baseline_id):
                    continue
                reason = f"PRD {prd_id} revised {row.prd_revision} → {revision}"
                self._event(s, tenant, "baseline", row.baseline_id, "INVALIDATED", "svc-n2", reason=reason,
                            new_prd_revision=revision)
                for wp in s.scalars(select(N3WorkPackageRow).where(N3WorkPackageRow.tenant_id == tenant,
                                                                   N3WorkPackageRow.baseline_id == row.baseline_id)):
                    self._event(s, tenant, "work_package", wp.work_package_id, "INVALIDATED", "svc-n2", reason=reason)
                for rel in s.scalars(select(N3ReleaseRow).where(N3ReleaseRow.tenant_id == tenant,
                                                                N3ReleaseRow.baseline_id == row.baseline_id)):
                    self._event(s, tenant, "release", rel.release_id, "INVALIDATED", "svc-n2", reason=reason)
                Repository(s).append_audit(tenant_id=tenant, event_type="N3_BASELINE_INVALIDATED",
                                           actor_id=f"svc-n3@{tenant}", actor_roles=["node_service"],
                                           subject=f"n3_baseline:{row.baseline_id}",
                                           details={"reason": reason, "new_prd_revision": revision})
                done.append(row.baseline_id)
        return done

    # ==========================================================================================
    # feedback, status and views
    # ==========================================================================================
    def feedback(self, p: Principal, req: FeedbackRequest) -> N3Feedback:
        t = p.tenant_id
        with self.db.session() as s:
            baseline_id = None
            if req.target_kind == "ASSESSMENT":
                baseline_id = self._assessment(s, t, req.target_id).baseline_id
            elif req.target_kind == "FINDING":
                for r in s.scalars(select(N3AssessmentRow).where(N3AssessmentRow.tenant_id == t)):
                    if any(f["finding_id"] == req.target_id for f in r.record["findings"]):
                        baseline_id = r.record["baseline_id"]
                        break
                else:
                    raise N0Error(404, "FINDING_NOT_FOUND", f"{req.target_id} not found in this tenant")
            elif req.target_kind == "RELEASE":
                baseline_id = self._get(s, N3ReleaseRow, N3Release, t, req.target_id, "RELEASE_NOT_FOUND").baseline_id
            else:
                jobs = self._jobs(s, t, job_id=req.target_id)
                if req.target_id not in jobs:
                    raise N0Error(404, "JOB_NOT_FOUND", f"{req.target_id} not found in this tenant")
                baseline_id = self._wp(s, t, jobs[req.target_id].work_package_id).baseline_id
            synthetic = self._baseline(s, t, baseline_id).synthetic
            fb = N3Feedback(feedback_id=new_id("N3FB"), tenant_id=t, target_kind=req.target_kind,
                            target_id=req.target_id, label=req.label, reason=req.reason, created_by=p.principal_id,
                            created_at=self.clock(), training_eligible=req.label != "OTHER" and not synthetic)
            s.add(N3FeedbackRow(feedback_id=fb.feedback_id, tenant_id=t, target_kind=req.target_kind,
                                target_id=req.target_id, record=dump(fb)))
            self._audit(s, p, "N3_FEEDBACK", f"n3_{req.target_kind.lower()}:{req.target_id}",
                        {"label": req.label, "training_eligible": fb.training_eligible})
        return fb

    def status(self, tenant: str, kind: str, subject_id: str) -> StatusView:
        with self.db.session() as s:
            if kind == "job":
                jobs = self._jobs(s, tenant, job_id=subject_id)
                if subject_id not in jobs:
                    raise N0Error(404, "JOB_NOT_FOUND", f"{subject_id} not found in this tenant")
                rows = s.scalars(select(N3JobRow).where(N3JobRow.tenant_id == tenant, N3JobRow.job_id == subject_id)
                                 .order_by(N3JobRow.seq))
                return StatusView(subject=f"job:{subject_id}", status=jobs[subject_id].status,
                                  history=[{"status": r.status, "at": r.record.get("finished_at") or
                                            r.record.get("submitted_at")} for r in rows])
            if kind == "baseline":
                self._baseline(s, tenant, subject_id)
            elif kind == "work_package":
                self._wp(s, tenant, subject_id)
            elif kind == "release":
                rel = self._get(s, N3ReleaseRow, N3Release, tenant, subject_id, "RELEASE_NOT_FOUND")
            else:
                raise N0Error(422, "UNKNOWN_SUBJECT", "kind must be baseline, work_package, job or release")
            history = self._events(s, tenant, kind, subject_id)
            if any(e["status"] == "INVALIDATED" for e in history):
                status = "INVALIDATED"
            elif kind == "release":
                status = rel.status
            elif kind == "baseline":
                status = "ACTIVE"
            else:
                status = history[-1]["status"] if history else "PLANNED"
        return StatusView(subject=f"{kind}:{subject_id}", status=status, history=history)

    def baseline_view(self, tenant: str, baseline_id: str) -> dict:
        with self.db.session() as s:
            b = self._baseline(s, tenant, baseline_id)
            wps = [N3WorkPackage.model_validate(r.record) for r in s.scalars(
                select(N3WorkPackageRow).where(N3WorkPackageRow.tenant_id == tenant,
                                               N3WorkPackageRow.baseline_id == baseline_id)
                .order_by(N3WorkPackageRow.created_at))]
            rels = [N3Release.model_validate(r.record) for r in s.scalars(
                select(N3ReleaseRow).where(N3ReleaseRow.tenant_id == tenant, N3ReleaseRow.baseline_id == baseline_id)
                .order_by(N3ReleaseRow.created_at))]
        return {"baseline": dump(b), "status": self.status(tenant, "baseline", baseline_id).status,
                "work_packages": [dump(w) for w in wps], "releases": [dump(r) for r in rels]}

    def work_package_view(self, p: Principal, wp_id: str) -> dict:
        t = p.tenant_id
        with self.db.session() as s:
            wp = self._wp(s, t, wp_id)
            routes = self._routes(s, t, wp_id)
            if p.has("vendor_submitter") and not p.has("design_engineer", "design_authority", "auditor"):
                if not any(r.authorized and r.vendor_id and r.vendor_id == p.vendor_id for r in routes):
                    raise N0Error(403, "VENDOR_NOT_ROUTED", f"{wp_id} is not routed to vendor {p.vendor_id}")
                return {"work_package": dump(wp), "deliveries": [dump(d) for d in self._deliveries(s, t, wp_id)
                                                                 if d.submitted_by == p.principal_id]}
            files, _, jobs = self._current_files(s, t, wp_id)
            assessments = self._assessments(s, t, wp_id)
            reviews = {a.assessment_id: [dump(r) for r in self._reviews(s, t, a.assessment_id)] for a in assessments}
            deliveries = self._deliveries(s, t, wp_id)
        return {"work_package": dump(wp), "status": self.status(t, "work_package", wp_id).status,
                "routes": [dump(r) for r in routes], "deliveries": [dump(d) for d in deliveries],
                "current_files": [dump(f) for f in files], "jobs": [dump(j) for j in jobs.values()],
                "assessments": [dump(a) for a in assessments], "reviews": reviews}

    def assessment(self, tenant: str, assessment_id: str) -> N3Assessment:
        with self.db.session() as s:
            return self._assessment(s, tenant, assessment_id)

    def release_view(self, tenant: str, release_id: str) -> dict:
        with self.db.session() as s:
            rel = self._get(s, N3ReleaseRow, N3Release, tenant, release_id, "RELEASE_NOT_FOUND")
        return {"release": dump(rel), "status": self.status(tenant, "release", release_id).status}

    def tools(self) -> dict:
        return {**self.runner.describe(), "operations": {k: v["tool"] for k, v in OPERATIONS.items()},
                "jobs_inline": self.settings.jobs_inline, "max_concurrent_jobs": self.settings.max_concurrent_jobs}


__all__ = ["N3Service", "READ_ONLY_ROLES"]
