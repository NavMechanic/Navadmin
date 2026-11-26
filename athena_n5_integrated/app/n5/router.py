"""N5 API (mounted on the N0 gateway under ``/v1/n5``). Every refusal is audited by N0's ``guarded``.

Roles (separation of duties):
  procurement_analyst   lock BOM intakes, propose supplier records, record FX assumptions, submit / look
                        up quotations, run sourcing reports, raise ECO proposals
  supplier_quality      verify supplier sites (Open Supply Hub) and qualify suppliers (never ones they
                        submitted)
  procurement_approver  approve a sourcing report against its exact report hash + quote hashes and
                        release the N6 / N7 draft handoffs (never as an analyst, submitter or qualifier
                        of a selected supplier)
  auditor               read
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, FastAPI, File, Form, Response, UploadFile
from starlette.concurrency import run_in_threadpool

from ..errors import N0Error
from ..lineage import register_attr_resolver
from ..security import Principal, principal_dependency
from .config import N5Settings
from .db import N5HandoffRow, N5IntakeRow, N5QuoteRow, N5ReportRow
from .models import (BomIntakeRequest, EcoProposalRequest, FxAssumptionRequest, HandoffRequest, LookupRequest,
                     N5BomIntake, N5EcoProposal, N5FxAssumption, N5Handoff, N5Quote, N5Review, N5ReviewRequest,
                     N5SourcingReport, N5Supplier, QuoteRequest, SourcingReportRequest, SupplierRequest,
                     SupplierReviewRequest)
from .service import N5Service

ANALYST = ("procurement_analyst",)
QUALITY = ("supplier_quality",)
APPROVER = ("procurement_approver",)
READERS = ("procurement_analyst", "supplier_quality", "procurement_approver", "auditor", "commercial_operator",
           "finance_operator")
AUDIT = ("procurement_approver", "supplier_quality", "auditor")


def _register_lineage() -> None:
    def rec(row_cls, keys):
        def fn(repo, tenant, ref):
            row = repo.s.get(row_cls, ref)
            if row is None or row.tenant_id != tenant:
                return {"resolved": False}
            return {k: row.record.get(k) for k in keys}
        return fn

    register_attr_resolver("n5_intake", rec(N5IntakeRow, ("bom_ref", "design_revision", "intake_sha256")))
    register_attr_resolver("n5_quote", rec(N5QuoteRow, ("supplier_id", "channel", "status", "quote_sha256")))
    register_attr_resolver("n5_report", rec(N5ReportRow, ("decision", "report_sha256", "indicative_total")))
    register_attr_resolver("n5_handoff", rec(N5HandoffRow, ("status", "design_revision", "indicative_total")))


async def _read(files: list[UploadFile], cap: int) -> list[tuple[str, Optional[str], bytes]]:
    out = []
    for f in files:
        data = await f.read(cap + 1)
        if len(data) > cap:
            raise N0Error(413, "FILE_TOO_LARGE", f"{f.filename} exceeds {cap} bytes")
        out.append((f.filename or "", f.content_type, data))
    return out


def install(settings: Optional[N5Settings] = None, *, transport=None) -> Callable[[FastAPI], None]:
    settings = settings or N5Settings.from_env()

    def _install(app: FastAPI) -> None:
        svc = N5Service(app.state, settings, transport=transport)
        app.state.n5 = svc
        guarded, auth = app.state.guarded, Depends(principal_dependency)
        cap = app.state.settings.max_upload_bytes
        r = APIRouter(prefix="/v1/n5", tags=["n5"])
        _register_lineage()

        def run(p: Principal, event: str, subject: str, roles: tuple[str, ...], fn: Callable[[], Any]) -> Any:
            def go():
                if not p.has(*roles):
                    raise N0Error(403, "ROLE_REQUIRED", f"requires one of roles {list(roles)}")
                return fn()
            return guarded(p, event, subject, go)

        def quote_status(response: Response, q: N5Quote) -> N5Quote:
            response.status_code = 201 if q.status == "ACCEPTED" else 422
            return q

        @r.get("/status")
        def status(p: Principal = auth) -> dict:
            return run(p, "N5_STATUS", "n5", READERS, svc.overview)

        # ------------------------------------------------------------ N5.1 BOM intake
        @r.post("/bom-intakes", status_code=201, response_model=N5BomIntake)
        def lock_intake(body: BomIntakeRequest, p: Principal = auth) -> N5BomIntake:
            return run(p, "N5_INTAKE_LOCK", f"prd:{body.prd_id}", ANALYST, lambda: svc.lock_intake(p, body))

        @r.get("/bom-intakes/{intake_id}")
        def get_intake(intake_id: str, p: Principal = auth) -> dict:
            return run(p, "N5_READ", f"n5_intake:{intake_id}", READERS, lambda: svc.intake_view(p.tenant_id, intake_id))

        # ------------------------------------------------------------ N5.2 suppliers
        @r.post("/suppliers", status_code=201, response_model=N5Supplier)
        def register_supplier(body: SupplierRequest, p: Principal = auth) -> N5Supplier:
            return run(p, "N5_SUPPLIER_SUBMIT", f"n5_supplier:{body.supplier_id}", ANALYST + QUALITY,
                       lambda: svc.register_supplier(p, body))

        @r.post("/suppliers/{supplier_id}/revisions", status_code=201, response_model=N5Supplier)
        def revise_supplier(supplier_id: str, body: SupplierRequest, p: Principal = auth) -> N5Supplier:
            return run(p, "N5_SUPPLIER_REVISE", f"n5_supplier:{supplier_id}", ANALYST + QUALITY,
                       lambda: svc.revise_supplier(p, supplier_id, body))

        @r.post("/suppliers/{supplier_id}/review", response_model=N5Supplier)
        def review_supplier(supplier_id: str, body: SupplierReviewRequest, p: Principal = auth) -> N5Supplier:
            return run(p, "N5_SUPPLIER_REVIEW", f"n5_supplier:{supplier_id}", QUALITY,
                       lambda: svc.review_supplier(p, supplier_id, body))

        @r.post("/suppliers/{supplier_id}/sites/{site_id}/verify", response_model=N5Supplier)
        async def verify_site(supplier_id: str, site_id: str, osh_csv: Optional[UploadFile] = File(None),
                              p: Principal = auth) -> N5Supplier:
            data = (await _read([osh_csv], cap))[0][2] if osh_csv is not None else None
            return await run_in_threadpool(run, p, "N5_SITE_VERIFY", f"n5_supplier:{supplier_id}", QUALITY,
                                           lambda: svc.verify_site(p, supplier_id, site_id, data))

        @r.get("/suppliers")
        def list_suppliers(p: Principal = auth) -> list[N5Supplier]:
            return run(p, "N5_READ", "n5_suppliers", READERS, lambda: list(svc.suppliers(p.tenant_id).values()))

        @r.get("/suppliers/{supplier_id}", response_model=N5Supplier)
        def get_supplier(supplier_id: str, p: Principal = auth) -> N5Supplier:
            def go():
                sup = svc.suppliers(p.tenant_id).get(supplier_id)
                if sup is None:
                    raise N0Error(404, "SUPPLIER_NOT_FOUND", f"{supplier_id} is not registered")
                return sup
            return run(p, "N5_READ", f"n5_supplier:{supplier_id}", READERS, go)

        # ------------------------------------------------------------ FX
        @r.post("/fx-assumptions", status_code=201, response_model=N5FxAssumption)
        def record_fx(body: FxAssumptionRequest, p: Principal = auth) -> N5FxAssumption:
            return run(p, "N5_FX_RECORD", f"n5_fx:{body.fx_id}", ANALYST, lambda: svc.record_fx(p, body))

        # ------------------------------------------------------------ N5.3 quotations
        @r.post("/quotes", response_model=N5Quote)
        def submit_quote(body: QuoteRequest, response: Response, p: Principal = auth) -> N5Quote:
            return run(p, "N5_QUOTE_SUBMIT", f"n5_intake:{body.bom_intake_id}", ANALYST,
                       lambda: quote_status(response, svc.submit_quote(p, body)))

        @r.post("/quotes/ems", response_model=N5Quote)
        async def submit_ems(response: Response, manifest: str = Form(...), source_id: str = Form(...),
                             files: list[UploadFile] = File(...), p: Principal = auth) -> N5Quote:
            data = await _read(files, cap)
            return await run_in_threadpool(run, p, "N5_QUOTE_EMS", "n5_dropzone", ANALYST,
                                           lambda: quote_status(response, svc.submit_ems_quote(p, manifest, source_id,
                                                                                               data)))

        @r.get("/quotes/{quote_id}", response_model=N5Quote)
        def get_quote(quote_id: str, p: Principal = auth) -> N5Quote:
            return run(p, "N5_READ", f"n5_quote:{quote_id}", READERS, lambda: svc.quote(p.tenant_id, quote_id))

        @r.post("/bom-intakes/{intake_id}/lookups")
        def lookup(intake_id: str, body: LookupRequest, p: Principal = auth) -> dict:
            return run(p, "N5_LOOKUP", f"n5_intake:{intake_id}", ANALYST, lambda: svc.lookup(p, intake_id, body))

        @r.get("/connector-calls")
        def connector_calls(p: Principal = auth) -> list[dict]:
            def go():
                from sqlalchemy import select
                from .db import N5ConnectorCallRow
                with svc.db.session() as s:
                    return [x.record for x in s.scalars(select(N5ConnectorCallRow).where(
                        N5ConnectorCallRow.tenant_id == p.tenant_id).order_by(N5ConnectorCallRow.created_at))]
            return run(p, "N5_READ", "n5_connector_calls", AUDIT, go)

        # ------------------------------------------------------------ N5.4–N5.5 report
        @r.post("/reports", status_code=201, response_model=N5SourcingReport)
        def create_report(body: SourcingReportRequest, p: Principal = auth) -> N5SourcingReport:
            return run(p, "N5_REPORT", f"n5_intake:{body.bom_intake_id}", ANALYST, lambda: svc.create_report(p, body))

        @r.get("/reports/{report_id}", response_model=N5SourcingReport)
        def get_report(report_id: str, p: Principal = auth) -> N5SourcingReport:
            return run(p, "N5_READ", f"n5_report:{report_id}", READERS, lambda: svc.report(p.tenant_id, report_id))

        # ------------------------------------------------------------ N5.6 review and handoff
        @r.post("/reviews", status_code=201, response_model=N5Review)
        def review(body: N5ReviewRequest, p: Principal = auth) -> N5Review:
            return run(p, "N5_REVIEW", f"n5_report:{body.report_id}", APPROVER, lambda: svc.review(p, body))

        @r.post("/reports/{report_id}/handoffs", response_model=N5Handoff)
        def handoff(report_id: str, body: HandoffRequest, response: Response, p: Principal = auth) -> N5Handoff:
            def go():
                out = svc.release(p, report_id, body)
                response.status_code = {"RELEASED": 201, "PARTIAL": 207}.get(out.status, 409)
                return out
            return run(p, "N5_HANDOFF", f"n5_report:{report_id}", APPROVER, go)

        @r.get("/handoffs/{handoff_id}", response_model=N5Handoff)
        def get_handoff(handoff_id: str, p: Principal = auth) -> N5Handoff:
            return run(p, "N5_READ", f"n5_handoff:{handoff_id}", READERS, lambda: svc.handoff(p.tenant_id, handoff_id))

        @r.get("/subjects/{kind}/{subject_id}/status")
        def subject_status(kind: str, subject_id: str, p: Principal = auth) -> dict:
            return run(p, "N5_READ", f"n5_{kind}:{subject_id}", READERS,
                       lambda: svc.status(p.tenant_id, kind, subject_id))

        # ------------------------------------------------------------ ECO proposals
        @r.post("/eco-proposals", status_code=201, response_model=N5EcoProposal)
        def raise_eco(body: EcoProposalRequest, p: Principal = auth) -> N5EcoProposal:
            return run(p, "N5_ECO", f"n5_intake:{body.bom_intake_id}", ANALYST, lambda: svc.raise_eco(p, body))

        @r.get("/eco-proposals")
        def list_ecos(intake_id: Optional[str] = None, p: Principal = auth) -> list[N5EcoProposal]:
            return run(p, "N5_READ", "n5_eco_proposals", READERS + ("design_engineer", "design_authority"),
                       lambda: svc.ecos(p.tenant_id, intake_id))

        app.include_router(r)
    return _install
