"""POST /v1/extraction/run — schema-constrained RFQ extraction behind the deterministic gate.

Order of operations (each step can stop the run):

1. tenant + source checks, blob must belong to the tenant and the source and be SCAN_PASSED;
2. bytes are re-hashed against the stored SHA-256;
3. Docling parses the document (fallback parsers otherwise);
4. Presidio redacts PII — the model only ever sees the redacted text;
5. ``LLMAdapter`` enforces the transmission boundary, calls the provider, revalidates the JSON;
6. a ``ModelRun.v1`` is persisted (prompt revision, input hash, usage, estimated cost);
7. the output becomes an ``RFQDraft.v1``: every value is ``MODEL_EXTRACTED`` with its evidence
   quote and character offsets; values the document does not state stay absent and are listed in
   ``missing_fields``; N0 adds its own ``DERIVED`` values (e.g. V × I) with formula and inputs;
8. ``rules.py`` re-runs units, physics, provenance, evidence-in-source and completeness checks.
   ``decision_preview`` is therefore never PASS for model output — at best REVIEW.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from .db import BlobRow
from .errors import N0Error
from .llm_adapter import (PROMPT_REVISION, RFQ_PARAMETERS, LLMAdapter, LLMOutputInvalid, LLMRefusalError,
                          LLMResult, estimate_cost_usd)
from .models import (EvidenceSnippet, ExtractionRequest, ExtractionResponse, ModelRun, RFQDraft, Requirement,
                     SourceRecord, TokenUsage, utcnow)
from .parsing import DoclingAdapter, ParseError, PresidioAdapter
from .repository import Repository, new_id
from .rules import (Findings, consistency_checks, decide, derive_values, evidence_checks, missing_specs,
                    normalize_requirements, physics_checks, provenance_checks, sha256_hex)
from .storage import ObjectStore

_TOP_LEVEL = ("customer_name", "product_category", "quantity")


class ExtractionFailure(N0Error):
    def __init__(self, status: int, code: str, message: str, model_run: Optional[ModelRun] = None) -> None:
        super().__init__(status, code, message, **({"model_run_id": model_run.run_id} if model_run else {}))
        self.model_run = model_run


def _snippet(blob_id: str, text: str, quote: Optional[str]) -> list[EvidenceSnippet]:
    if not quote:
        return []
    start = text.find(quote)
    return [EvidenceSnippet(source_ref=blob_id, text=quote[:2000],
                            char_start=start if start >= 0 else None,
                            char_end=start + len(quote) if start >= 0 else None)]


class ExtractionService:
    def __init__(self, store: ObjectStore, docling: DoclingAdapter, presidio: PresidioAdapter, llm: LLMAdapter,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.store, self.docling, self.presidio, self.llm, self.clock = store, docling, presidio, llm, clock

    def _load_blob(self, repo: Repository, req: ExtractionRequest) -> tuple[BlobRow, bytes]:
        blob = repo.get_blob(req.blob_id)
        if blob is None or blob.tenant_id != req.tenant_id:
            raise ExtractionFailure(404, "BLOB_NOT_FOUND", f"blob {req.blob_id} not found in tenant {req.tenant_id}")
        if blob.source_id != req.source_id:
            raise ExtractionFailure(409, "SOURCE_MISMATCH", f"blob {req.blob_id} was uploaded under source "
                                                            f"{blob.source_id}, not {req.source_id}")
        if blob.kind != "UPLOAD" or blob.status not in ("SCAN_PASSED", "RELEASED"):
            raise ExtractionFailure(409, "BLOB_NOT_CLEARED", f"blob {req.blob_id} is {blob.status}")
        data = self.store.get(blob.bucket, blob.object_key)
        if sha256_hex(data) != blob.sha256:
            raise ExtractionFailure(409, "SHA256_MISMATCH", f"stored bytes of {req.blob_id} do not match their "
                                                            "recorded SHA-256")
        return blob, data

    def run(self, repo: Repository, req: ExtractionRequest, source: Optional[SourceRecord]) -> ExtractionResponse:
        # fail fast on the transmission boundary; extract_rfq re-checks it right before the provider call
        self.llm.check_boundary(tenant_id=req.tenant_id, source=source, pii_redacted=True)
        blob, data = self._load_blob(repo, req)
        try:
            parsed = self.docling.parse(data, blob.sniffed_mime, blob.filename)
        except ParseError as exc:
            raise ExtractionFailure(422, "PARSE_FAILED", str(exc)) from exc
        findings = self.presidio.analyze(parsed.text)
        redacted, _ = self.presidio.redact(parsed.text, findings)
        input_hash = sha256_hex(f"{PROMPT_REVISION}\n{redacted}".encode())
        started = self.clock()
        # boundary violations (disabled / unapproved / cross-tenant / no LLM rights) propagate as LLM*Error
        try:
            result = self.llm.extract_rfq(tenant_id=req.tenant_id, source=source, document_text=redacted,
                                          pii_redacted=True)
        except (LLMRefusalError, LLMOutputInvalid) as exc:
            # persisted by the caller in its own transaction, so a failed run is never lost to a rollback
            run = self._model_run(req, blob, input_hash, None, started,
                                  "REFUSED" if isinstance(exc, LLMRefusalError) else "FAILED", {"error": str(exc)})
            raise ExtractionFailure(422, exc.code, str(exc), run) from exc
        run = self._model_run(req, blob, input_hash, result, started, "SUCCEEDED", result.output)
        repo.add_model_run(run)
        repo.add_edge(req.tenant_id, f"blob:{blob.blob_id}", f"model_run:{run.run_id}", "INPUT_TO",
                      prompt_revision=PROMPT_REVISION)
        return self._draft(req, blob, run, result, redacted, [x.to_dict() for x in findings], parsed.parser)

    def _model_run(self, req: ExtractionRequest, blob: BlobRow, input_hash: str, result: Optional[LLMResult],
                   started: datetime, status: str, output: dict) -> ModelRun:
        usage = result.usage if result else TokenUsage(input_tokens=0, output_tokens=0)
        name = result.model_name if result else self.llm.provider.model
        version = result.model_version if result else "n/a"
        cost = estimate_cost_usd(version, usage) or estimate_cost_usd(name, usage)
        return ModelRun(run_id=new_id("MR"), tenant_id=req.tenant_id, model_name=name, model_version=version,
                        provider=result.provider if result else self.llm.provider.name,
                        prompt_revision=PROMPT_REVISION, input_hash=input_hash, output_payload=output,
                        token_usage=usage, estimated_cost_usd=cost, status=status, source_id=req.source_id,
                        blob_id=blob.blob_id, started_at=started, completed_at=self.clock())

    def _draft(self, req: ExtractionRequest, blob: BlobRow, run: ModelRun, result: LLMResult, text: str,
               pii: list[dict], parser: str) -> ExtractionResponse:
        out = result.output
        reqs: list[Requirement] = []
        for i, p in enumerate(x for x in out["parameters"] if x["value"] is not None):
            reqs.append(Requirement(req_id=f"EXT-{i + 1:03d}", parameter=p["parameter"], value=p["value"],
                                    unit=p["unit"], value_origin="MODEL_EXTRACTED",
                                    evidence_snippets=_snippet(blob.blob_id, text, p["evidence_text"])))
        f = Findings()
        reqs = normalize_requirements(reqs, "requirements", f)
        derived = derive_values(reqs)
        physics_checks(reqs, derived, "requirements", f)
        consistency_checks(reqs, "requirements", f)
        provenance_checks("RFQ", {"extraction_model_run_id": run.run_id}, reqs, derived, f)
        evidence_checks(reqs, text, f)

        known = set(RFQ_PARAMETERS) | set(_TOP_LEVEL)
        missing = [m for m in out["missing_fields"] if m in known]
        stated = {r.parameter for r in reqs}
        missing += [p for p in RFQ_PARAMETERS if p not in stated and p not in missing]
        missing += [k for k in _TOP_LEVEL if out.get(k) is None and k not in missing]
        critical = missing_specs("RFQ", {"product_category": out.get("product_category") or ""}, reqs)
        for m in critical:
            f.add("MISSING_CRITICAL_FIELD", f"critical specification {m} was not found in the document", m)
        for k in _TOP_LEVEL:
            if out.get(k) is None:
                f.add("MISSING_CRITICAL_FIELD", f"RFQ field {k} was not found in the document", f"payload.{k}")

        all_reqs = reqs + derived
        draft = RFQDraft(draft_id=new_id("DRF"), tenant_id=req.tenant_id, source_id=req.source_id,
                         blob_id=blob.blob_id, customer_name=out.get("customer_name"),
                         product_category=out.get("product_category"), quantity=out.get("quantity"),
                         requirements=all_reqs, missing_fields=sorted(set(missing) | set(critical)),
                         evidence_snippets=[s for r in reqs for s in r.evidence_snippets], model_run_id=run.run_id)
        return ExtractionResponse(draft=draft, model_run=run, violated_rules=f.items,
                                  missing_fields=draft.missing_fields, decision_preview=decide(f.items),
                                  pii_findings=pii, parser=parser)
