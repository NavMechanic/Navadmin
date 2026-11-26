"""ATHENA N0 — Data Intelligence, Quality & Governance Gateway (FastAPI).

Run:  uvicorn app.main:app            (configuration from N0_* environment variables, see config.py)
Test: create_app(Settings(...))       (tests inject settings, an LLM provider and a clock)

Every state-changing endpoint writes a hash-chained audit event; refusals are audited too
(``<EVENT>_REFUSED``) in their own transaction so a rollback never erases the record of an attempt.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Callable, Optional, Sequence

from fastapi import Depends, FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings
from .db import BlobRow, Database
from .errors import N0Error
from .extraction import ExtractionFailure, ExtractionService
from .governance import load_current, record_approval, record_feedback, release_handoff
from .lineage import build_lineage
from .llm_adapter import LLMAdapter, LLMBoundaryError, LLMDisabledError, LLMError, Provider, make_provider
from .models import (ApprovalRequest, ApprovalResponse, ArtifactView, BlobRecord, EvaluateRequest, EvaluationResult,
                     ExtractionRequest, ExtractionResponse, FeedbackRecord, FeedbackRequest, HandoffReleaseRequest,
                     LineageGraph, ReleaseManifest, SourceApprovalRequest, SourceApprovalResponse, SourceRecord,
                     SourceRegisterRequest, TransmissionDecision, VendorResult, VendorResultDecision,
                     VendorWorkPackage, utcnow)
from .parsing import DoclingAdapter, PresidioAdapter
from .pipeline import N0Pipeline
from .repository import Repository, dump, new_id
from .rules import RULES, VALIDATOR_VERSION, normalize_revision, sha256_hex
from .scanning import make_scanner, validate_upload
from .security import Principal, build_authenticator, principal_dependency
from .storage import make_store
from .vendor_contracts import authorize_work_package, ingest_vendor_result

ANY_ROLE = ("producer", "source_admin", "rights_approver", "gateway_reviewer", "release_manager", "node_service",
            "auditor")
_SOURCE_TRANSITIONS = {"PENDING": {"APPROVED", "REJECTED"}, "APPROVED": {"REVOKED"}, "REJECTED": {"APPROVED"},
                       "REVOKED": set()}

DESCRIPTION = """Policy gate in front of every ATHENA node (N1–N8).

* **PASS is not engineering qualification**: it means N0's deterministic checks for the declared
  `artifact_type` and `release_stage` passed.
* **Hard-policy BLOCK is final** for its revision: no LLM output, score, approval or feedback can override it.
* Every value carries `value_origin` (CUSTOMER | MEASURED | DERIVED | MODEL_EXTRACTED) with evidence.
* No data reaches an LLM or a vendor unless the SourceRecord.v1 rights and the tenant ACLs allow it.
* Uploads are quarantined, MIME-sniffed, malware-scanned and SHA-256 addressed before anything reads them.
"""


def _need(p: Principal, *roles: str) -> None:
    if not p.has(*roles):
        raise N0Error(403, "ROLE_REQUIRED", f"requires one of roles {list(roles)}")


def _tenant(p: Principal, tenant_id: str) -> None:
    if p.tenant_id != tenant_id:
        raise N0Error(403, "CROSS_TENANT_REFERENCE", "cross-tenant access is not permitted")


def _safe_filename(name: Optional[str]) -> str:
    base = PureWindowsPath(PurePosixPath(name or "upload.bin").name).name
    return ("".join(ch for ch in base if ch.isprintable()) or "upload.bin")[:255]


def create_app(settings: Optional[Settings] = None, *, provider: Optional[Provider] = None,
               clock: Callable[[], datetime] = utcnow,
               extensions: Sequence[Callable[[FastAPI], None]] = ()) -> FastAPI:
    """Build the N0 gateway. ``extensions`` are called with the finished app (sharing its database,
    object store, pipeline and authenticator on ``app.state``) to mount further routers, e.g. N1."""
    settings = settings or Settings.from_env()
    db = Database(settings.database_url)
    if settings.auto_create_schema:
        db.create_all()
    store = make_store(settings)
    scanner = make_scanner(settings)
    docling, presidio = DoclingAdapter(), PresidioAdapter()
    llm = LLMAdapter(provider or make_provider(settings))
    pipeline = N0Pipeline(db, store, docling, presidio, clock)
    extraction = ExtractionService(store, docling, presidio, llm, clock)

    app = FastAPI(title="ATHENA N0 — Data Intelligence, Quality & Governance Gateway", version=__version__,
                  description=DESCRIPTION)
    for k, v in dict(settings=settings, db=db, store=store, scanner=scanner, docling=docling, presidio=presidio,
                     llm=llm, pipeline=pipeline, extraction=extraction, tokens=build_authenticator(settings),
                     clock=clock, source_rights_listeners=[]).items():
        setattr(app.state, k, v)

    @app.exception_handler(N0Error)
    async def _n0_error(_: Request, exc: N0Error) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail()})

    def audit(p: Principal, event_type: str, subject: str, details: dict, **kw: Any) -> None:
        with db.session() as s:
            Repository(s).append_audit(tenant_id=p.tenant_id, event_type=event_type, actor_id=p.principal_id,
                                       actor_roles=p.roles, subject=subject, details=details, **kw)

    def guarded(p: Principal, event_type: str, subject: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except N0Error as err:
            audit(p, f"{event_type}_REFUSED", subject, err.detail())
            raise

    auth = Depends(principal_dependency)

    # ------------------------------------------------------------------ health
    @app.get("/health", tags=["ops"])
    def health() -> JSONResponse:
        db_h, st_h, av_h = db.ping(), store.health(), scanner.health()
        av_ok = av_h.get("ok", False) and (av_h.get("real_engine") or not settings.require_av)
        ok = db_h["ok"] and st_h["ok"] and av_ok
        body = {"status": "ok" if ok else "degraded", "service": "athena-n0-gateway", "version": __version__,
                "components": {
                    "database": db_h, "object_storage": st_h, "malware_scanner": {**av_h, "required": settings.require_av},
                    "policy_engine": {"ok": True, "validator_version": VALIDATOR_VERSION, "rules": len(RULES)},
                    "parsers": {"document": docling.mode, "pii": presidio.mode},
                    "llm": llm.describe()}}
        return JSONResponse(status_code=200 if ok else 503, content=body)

    @app.get("/v1/rules", tags=["ops"])
    def rules_catalog(p: Principal = auth) -> dict:
        return {"validator_version": VALIDATOR_VERSION,
                "rules": [{"rule_id": r.rule_id, "code": r.code, "severity": r.severity, "category": r.category,
                           "stage": r.stage, "description": r.description} for r in RULES.values()]}

    # ------------------------------------------------------------------ N0.1 sources
    @app.post("/v1/sources/register", status_code=201, response_model=SourceRecord, tags=["sources"])
    def register_source(body: SourceRegisterRequest, p: Principal = auth) -> SourceRecord:
        def go() -> SourceRecord:
            _need(p, "source_admin")
            _tenant(p, body.tenant_id)
            with db.session() as s:
                repo = Repository(s)
                if repo.source_row(body.tenant_id, body.source_id) is not None:
                    raise N0Error(409, "SOURCE_EXISTS", f"{body.source_id} is already registered")
                rec = SourceRecord(**body.model_dump(), approval_status="PENDING", registered_by=p.principal_id)
                repo.add_source(rec)
                repo.append_audit(tenant_id=p.tenant_id, event_type="SOURCE_REGISTERED", actor_id=p.principal_id,
                                  actor_roles=p.roles, subject=f"source:{rec.source_id}", details=dump(rec))
                return rec
        return guarded(p, "SOURCE_REGISTER", f"source:{body.source_id}", go)

    @app.get("/v1/sources/{tenant_id}/{source_id}", response_model=SourceRecord, tags=["sources"])
    def get_source(tenant_id: str, source_id: str, p: Principal = auth) -> SourceRecord:
        _tenant(p, tenant_id)
        with db.session() as s:
            rec = Repository(s).get_source(tenant_id, source_id)
        if rec is None:
            raise N0Error(404, "SOURCE_NOT_REGISTERED", f"{source_id} is not registered")
        return rec

    @app.post("/v1/sources/{tenant_id}/{source_id}/approve", response_model=SourceApprovalResponse, tags=["sources"])
    def approve_source(tenant_id: str, source_id: str, body: SourceApprovalRequest,
                       p: Principal = auth) -> SourceApprovalResponse:
        def go() -> SourceApprovalResponse:
            _need(p, "rights_approver")
            _tenant(p, tenant_id)
            with db.session() as s:
                repo = Repository(s)
                rec = repo.get_source(tenant_id, source_id)
                if rec is None:
                    raise N0Error(404, "SOURCE_NOT_REGISTERED", f"{source_id} is not registered")
                if rec.registered_by == p.principal_id:
                    raise N0Error(403, "FOUR_EYES", "the registrant of a source cannot approve its rights")
                if body.decision not in _SOURCE_TRANSITIONS[rec.approval_status]:
                    raise N0Error(409, "INVALID_TRANSITION", f"{rec.approval_status} → {body.decision} is not "
                                                             "allowed")
                new = SourceRecord.model_validate({**dump(rec), "approval_status": body.decision,
                                                   "approved_by": p.principal_id,
                                                   "approved_at": clock().isoformat()})
                version = repo.update_source(new)
                event = repo.append_approval(
                    tenant_id=tenant_id, artifact_id=f"source:{source_id}", exact_revision=f"v{version}",
                    approver_id=p.principal_id, approver_role="rights_approver",
                    decision="APPROVED" if body.decision == "APPROVED" else "REJECTED", scope="SOURCE_RIGHTS",
                    reason=body.reason if body.decision != "REVOKED" else f"REVOKED: {body.reason}")
                repo.add_edge(tenant_id, f"approval:{event.approval_id}", f"source:{source_id}", "RIGHTS_DECISION",
                              status=body.decision)
                repo.append_audit(tenant_id=tenant_id, event_type="SOURCE_RIGHTS_DECIDED", actor_id=p.principal_id,
                                  actor_roles=p.roles, subject=f"source:{source_id}",
                                  details={"decision": body.decision, "approval_id": event.approval_id,
                                           "version": version})
                return SourceApprovalResponse(source=new, approval_event=event)
        out = guarded(p, "SOURCE_APPROVE", f"source:{source_id}", go)
        notify_rights_listeners(p, tenant_id, source_id, out.source.approval_status)
        return out

    def notify_rights_listeners(p: Principal, tenant_id: str, source_id: str, status: str) -> None:
        """Nodes register ``(tenant_id, source_id, status)`` callbacks on ``app.state.source_rights_listeners``
        to react to a committed rights decision, e.g. N5 blocking handoffs that used a revoked source. A
        failing listener is audited; the decision itself stands, and nodes re-check rights lazily too."""
        for listener in app.state.source_rights_listeners:
            try:
                listener(tenant_id, source_id, status)
            except Exception as exc:  # noqa: BLE001 - a listener must never undo a committed decision
                audit(p, "SOURCE_RIGHTS_LISTENER_FAILED", f"source:{source_id}",
                      {"status": status, "error": f"{type(exc).__name__}: {exc}"[:500]})

    # ------------------------------------------------------------------ N0.1 upload (quarantine-first)
    @app.post("/v1/artifacts/upload", status_code=201, response_model=BlobRecord, tags=["artifacts"])
    def upload(response: Response, file: UploadFile = File(...), tenant_id: str = Form(...),
               source_id: str = Form(...), sha256: Optional[str] = Form(None), p: Principal = auth) -> BlobRecord:
        filename = _safe_filename(file.filename)

        def go() -> BlobRecord:
            _need(p, "producer", "node_service")
            _tenant(p, tenant_id)
            data = file.file.read(settings.max_upload_bytes + 1)
            if len(data) > settings.max_upload_bytes:
                raise N0Error(413, "FILE_TOO_LARGE", f"upload exceeds {settings.max_upload_bytes} bytes")
            digest = sha256_hex(data)
            if sha256 is not None and sha256.lower() != digest:
                raise N0Error(422, "SHA256_MISMATCH", f"declared SHA-256 {sha256[:12]}… does not match the "
                                                      f"received bytes ({digest[:12]}…)")
            screen = validate_upload(data, filename, file.content_type, settings.max_upload_bytes)
            if not screen.ok:
                typed = any(("not allowed" in x or "extension" in x or "Content-Type" in x) for x in screen.problems)
                raise N0Error(415 if typed else 422, "UPLOAD_REJECTED", "; ".join(screen.problems),
                              sniffed_mime=screen.sniffed_mime, problems=screen.problems)
            if settings.require_av and not scanner.real_engine:
                raise N0Error(503, "AV_REQUIRED", "N0_REQUIRE_AV is set but no real malware engine is configured")
            result = scanner.scan(data)
            if not result.clean:
                raise N0Error(422, "MALWARE_DETECTED", f"{result.engine} flagged the upload: {result.signature}",
                              sha256=digest)
            with db.session() as s:
                repo = Repository(s)
                src = repo.get_source(tenant_id, source_id)
                if src is None:
                    raise N0Error(403, "SOURCE_NOT_REGISTERED", f"{source_id} is not registered in {tenant_id}")
                if src.approval_status in ("REJECTED", "REVOKED"):
                    raise N0Error(403, "SOURCE_NOT_APPROVED", f"{source_id} is {src.approval_status}")
                existing = repo.find_blob(tenant_id, digest, "UPLOAD")
                if existing is not None:
                    if existing.source_id != source_id:
                        raise N0Error(409, "SOURCE_MISMATCH", f"identical bytes were already uploaded under "
                                                              f"{existing.source_id}")
                    response.status_code = 200
                    return repo.blob_record(existing)
                key = store.key_for(tenant_id, digest)
                store.put_immutable("quarantine", key, data)
                row = repo.add_blob(BlobRow(
                    blob_id=new_id("BLB"), tenant_id=tenant_id, source_id=source_id, kind="UPLOAD", sha256=digest,
                    size_bytes=len(data), filename=filename, declared_mime=file.content_type,
                    sniffed_mime=screen.sniffed_mime, status="SCAN_PASSED",
                    scan={"engine": result.engine, "real_engine": result.real_engine, "clean": True},
                    bucket="quarantine", object_key=key, created_by=p.principal_id))
                repo.add_edge(tenant_id, f"source:{source_id}", f"blob:{row.blob_id}", "SUPPLIED", sha256=digest)
                repo.append_audit(tenant_id=tenant_id, event_type="UPLOAD_QUARANTINED", actor_id=p.principal_id,
                                  actor_roles=p.roles, subject=f"blob:{row.blob_id}",
                                  details={"sha256": digest, "size_bytes": len(data), "mime": screen.sniffed_mime,
                                           "source_id": source_id, "scanner": result.engine})
                return repo.blob_record(row)
        return guarded(p, "UPLOAD", f"upload:{filename}", go)

    # ------------------------------------------------------------------ N0.1–N0.6 evaluate
    @app.post("/v1/gateway/evaluate", response_model=EvaluationResult, tags=["gateway"])
    def evaluate(body: EvaluateRequest, p: Principal = auth) -> EvaluationResult:
        def go() -> EvaluationResult:
            _need(p, "producer", "node_service")
            return pipeline.evaluate(p, body)
        return guarded(p, "GATEWAY_EVALUATION", f"{body.artifact.artifact_id}@{body.artifact.revision}", go)

    # ------------------------------------------------------------------ extraction
    @app.post("/v1/extraction/run", response_model=ExtractionResponse, tags=["extraction"])
    def run_extraction(body: ExtractionRequest, p: Principal = auth) -> ExtractionResponse:
        def go() -> ExtractionResponse:
            _need(p, "producer")
            _tenant(p, body.tenant_id)
            try:
                with db.session() as s:
                    repo = Repository(s)
                    resp = extraction.run(repo, body, repo.get_source(body.tenant_id, body.source_id))
                    run = resp.model_run
                    repo.append_audit(
                        tenant_id=p.tenant_id, event_type="EXTRACTION_RUN", actor_id=p.principal_id,
                        actor_roles=p.roles, subject=f"blob:{body.blob_id}",
                        details={"draft_id": resp.draft.draft_id, "decision_preview": resp.decision_preview,
                                 "error_codes": sorted({v.code for v in resp.violated_rules}),
                                 "missing_fields": resp.missing_fields, "pii_entities": len(resp.pii_findings),
                                 "parser": resp.parser},
                        model_runs=[{"run_id": run.run_id, "model_name": run.model_name,
                                     "model_version": run.model_version, "provider": run.provider,
                                     "prompt_revision": run.prompt_revision,
                                     "estimated_cost_usd": run.estimated_cost_usd}])
                    return resp
            except ExtractionFailure as err:
                if err.model_run is not None:
                    with db.session() as s:
                        Repository(s).add_model_run(err.model_run)
                raise
            except LLMDisabledError as err:
                raise N0Error(503, "LLM_DISABLED", str(err)) from err
            except LLMBoundaryError as err:
                raise N0Error(403, err.code, str(err)) from err
            except LLMError as err:
                raise N0Error(502, err.code, str(err)) from err
        return guarded(p, "EXTRACTION_RUN", f"blob:{body.blob_id}", go)

    # ------------------------------------------------------------------ read
    @app.get("/v1/artifacts/{artifact_id}", response_model=ArtifactView, tags=["artifacts"])
    def get_artifact(artifact_id: str, revision: Optional[str] = Query(None), p: Principal = auth) -> ArtifactView:
        _need(p, *ANY_ROLE)
        with db.session() as s:
            repo = Repository(s)
            revs = repo.revisions(p.tenant_id, artifact_id)
            if not revs:
                raise N0Error(404, "ARTIFACT_NOT_FOUND", f"{artifact_id} was not admitted in this tenant")
            rev, row, decision = load_current(repo, p.tenant_id, artifact_id, revision or revs[-1].revision)
            return ArtifactView(artifact_id=artifact_id, revision=rev, latest_revision=revs[-1].revision,
                                superseded=revs[-1].revision_order > row.revision_order,
                                revisions=[r.revision for r in revs], canonical_artifact=row.canonical,
                                decision=decision, releases=repo.releases(p.tenant_id, artifact_id, rev))

    @app.get("/v1/artifacts/{artifact_id}/lineage", response_model=LineageGraph, tags=["artifacts"])
    def lineage(artifact_id: str, revision: Optional[str] = Query(None), p: Principal = auth) -> LineageGraph:
        _need(p, *ANY_ROLE)
        rev = normalize_revision(revision) if revision else None
        with db.session() as s:
            graph = build_lineage(Repository(s), p.tenant_id, artifact_id, rev)
        if graph is None:
            raise N0Error(404, "ARTIFACT_NOT_FOUND", f"{artifact_id} was not admitted in this tenant")
        return graph

    # ------------------------------------------------------------------ governance
    @app.post("/v1/approvals", response_model=ApprovalResponse, tags=["governance"])
    def approvals(body: ApprovalRequest, p: Principal = auth) -> ApprovalResponse:
        def go() -> ApprovalResponse:
            with db.session() as s:
                repo = Repository(s)
                out = record_approval(repo, p, body, clock(), store)
                repo.append_audit(tenant_id=p.tenant_id, event_type=f"APPROVAL_{body.scope}", actor_id=p.principal_id,
                                  actor_roles=p.roles, subject=f"{body.artifact_id}@{out.approval_event.exact_revision}",
                                  details={"approval_id": out.approval_event.approval_id, "decision": body.decision,
                                           "resulting_decision": out.resulting_decision.decision
                                           if out.resulting_decision else None})
                return out
        return guarded(p, f"APPROVAL_{body.scope}", f"{body.artifact_id}@{body.exact_revision}", go)

    @app.post("/v1/handoffs/release", response_model=ReleaseManifest, tags=["governance"])
    def handoff(body: HandoffReleaseRequest, p: Principal = auth) -> ReleaseManifest:
        def go() -> ReleaseManifest:
            with db.session() as s:
                repo = Repository(s)
                m = release_handoff(repo, p, body, clock())
                repo.append_audit(tenant_id=p.tenant_id, event_type="HANDOFF_RELEASED", actor_id=p.principal_id,
                                  actor_roles=p.roles, subject=f"{m.artifact_id}@{m.revision}",
                                  details={"manifest_id": m.manifest_id, "manifest_sha256": m.manifest_sha256,
                                           "target_nodes": m.target_nodes, "approval_id": m.approval_id,
                                           "decision_id": m.decision_id})
                return m
        return guarded(p, "HANDOFF_RELEASE", f"{body.artifact_id}@{body.revision}", go)

    @app.post("/v1/gateway/feedback", response_model=FeedbackRecord, tags=["governance"])
    def feedback(body: FeedbackRequest, p: Principal = auth) -> FeedbackRecord:
        def go() -> FeedbackRecord:
            with db.session() as s:
                repo = Repository(s)
                rec = record_feedback(repo, p, body, clock())
                repo.append_audit(tenant_id=p.tenant_id, event_type="FEEDBACK_RECORDED", actor_id=p.principal_id,
                                  actor_roles=p.roles, subject=f"{rec.artifact_id}@{rec.revision}",
                                  details={"feedback_id": rec.feedback_id, "training_eligible": rec.training_eligible,
                                           "corrections": len(rec.corrections),
                                           "decision_changed": False})
                return rec
        return guarded(p, "FEEDBACK", f"{body.artifact_id}@{body.revision}", go)

    # ------------------------------------------------------------------ vendor boundary
    @app.post("/v1/vendor/work-packages", response_model=TransmissionDecision, tags=["vendor"])
    def work_package(body: VendorWorkPackage, p: Principal = auth) -> TransmissionDecision:
        def go() -> TransmissionDecision:
            _need(p, "release_manager")
            with db.session() as s:
                repo = Repository(s)
                d = authorize_work_package(repo, p, body, clock())
                repo.append_audit(tenant_id=p.tenant_id, event_type="VENDOR_TRANSMISSION_DECIDED",
                                  actor_id=p.principal_id, actor_roles=p.roles,
                                  subject=f"work_package:{body.work_package_id}",
                                  details={"vendor_id": body.vendor_id, "authorized": d.authorized,
                                           "authorized_inputs": d.authorized_inputs,
                                           "error_codes": sorted({v.code for v in d.violated_rules})})
                return d
        return guarded(p, "VENDOR_TRANSMISSION", f"work_package:{body.work_package_id}", go)

    @app.post("/v1/vendor/results", response_model=VendorResultDecision, tags=["vendor"])
    def vendor_result(body: VendorResult, p: Principal = auth) -> VendorResultDecision:
        def go() -> VendorResultDecision:
            _need(p, "node_service", "producer")
            with db.session() as s:
                repo = Repository(s)
                d = ingest_vendor_result(repo, p, body)
                repo.append_audit(tenant_id=p.tenant_id, event_type="VENDOR_RESULT_INGESTED", actor_id=p.principal_id,
                                  actor_roles=p.roles, subject=f"vendor_result:{d.result_id}",
                                  details={"work_package_id": body.work_package_id, "status": d.status,
                                           "checksum_verified": d.checksum_verified,
                                           "tool_version": body.tool_version})
                return d
        return guarded(p, "VENDOR_RESULT", f"work_package:{body.work_package_id}", go)

    # ------------------------------------------------------------------ audit
    @app.get("/v1/audit/events", tags=["audit"])
    def audit_events(limit: int = Query(100, ge=1, le=1000), p: Principal = auth) -> list[dict]:
        _need(p, "auditor")
        with db.session() as s:
            return [dump(e) for e in Repository(s).audit_events(p.tenant_id, limit)]

    @app.get("/v1/audit/verify", tags=["audit"])
    def audit_verify(p: Principal = auth) -> dict:
        _need(p, "auditor")
        with db.session() as s:
            repo = Repository(s)
            return {"tenant_id": p.tenant_id, "audit": repo.verify_chain(p.tenant_id, "audit"),
                    "approvals": repo.verify_chain(p.tenant_id, "approval")}

    app.state.audit, app.state.guarded = audit, guarded
    for extension in extensions:
        extension(app)
    return app


_app: Optional[FastAPI] = None


def __getattr__(name: str):   # lazy so importing app.main (tests, schema export) creates no database
    global _app
    if name == "app":
        if _app is None:
            _app = create_app(Settings.from_env())
        return _app
    raise AttributeError(name)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    uvicorn.run("app.main:app", host=os.environ.get("N0_HOST", "127.0.0.1"), port=int(os.environ.get("N0_PORT", "8000")))
