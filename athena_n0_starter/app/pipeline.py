"""The six-stage N0 gateway pipeline behind ``POST /v1/gateway/evaluate``.

    N0.1 intake & permissions    authorization, tenant isolation, SourceRecord rights, clearance,
                                 revision control, SHA-256 integrity of files and payload
    N0.2 parsing & normalization Docling parsing of referenced files, Presidio PII detection,
                                 SI unit normalization of every requirement
    N0.3 context assembly        payload classification vs declared type, upstream ID@REV resolution,
                                 model-run linkage, evidence-in-source, critical-spec completeness
    N0.4 data quality            JSON Schema 2020-12, V × I and other physics, tolerance/derating,
                                 duplicates, contradictions, freshness, value_origin truthfulness
    N0.5 hard policy             synthetic data, calibration, safety, stage gates
    N0.6 release                 decision (BLOCK > REWORK > REVIEW > PASS), allowed destinations,
                                 canonical artifact, release manifest, lineage, audit event

A BLOCK in N0.1 short-circuits: the submission is *not admitted* — nothing of it is stored except
the audit event, so a foreign or unlicensed payload never lands in N0 storage and a rejected
resubmission can never overwrite the decision of an already admitted revision.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from pydantic import ValidationError

from .db import ArtifactRow, BlobRow, Database
from .models import (REQUIREMENT_LISTS, Artifact, AuditEventOut, EvaluateRequest, EvaluationResult, FileRef,
                     GatewayDecision, ModelRun, Provenance, QualityReport, ReleaseManifest, Requirement,
                     RightsDecision, SourceRecord, utcnow)
from .parsing import DoclingAdapter, ParseError, PresidioAdapter
from .repository import Repository, dump, new_id
from .rules import (VALIDATOR_VERSION, Findings, allowed_destinations, canonical_json, check_clearance, classify,
                    consistency_checks, decide, design_checks, derive_values, evaluate_rights, evidence_checks, explain,
                    freshness_checks, hard_policy_checks, market_checks, missing_specs, normalize_requirements,
                    normalize_revision,
                    physics_checks, provenance_checks, quality_score, revision_order, schema_checks, sha256_hex)
from .security import Principal
from .storage import ObjectStore, StorageError, make_uri, parse_uri

PARSEABLE = {"application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "text/plain", "text/csv",
             "application/json"}


@dataclass
class _Ctx:
    principal: Principal
    request: EvaluateRequest
    now: datetime
    payload_sha256: str
    f: Findings = field(default_factory=Findings)
    timings: dict[str, float] = field(default_factory=dict)
    source: Optional[SourceRecord] = None
    rights: Optional[RightsDecision] = None
    revision: Optional[str] = None
    existing: Optional[ArtifactRow] = None
    previous: Optional[ArtifactRow] = None
    documents: list[tuple[BlobRow, bytes]] = field(default_factory=list)
    document_text: Optional[str] = None
    redacted_text: Optional[str] = None
    pii: list[dict] = field(default_factory=list)
    list_field: Optional[str] = None
    reqs: list[Requirement] = field(default_factory=list)
    derived: list[Requirement] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    upstream: list[str] = field(default_factory=list)
    model_runs: list[ModelRun] = field(default_factory=list)
    schema_valid: bool = False
    admitted: bool = False          # frozen at the end of N0.1; later-stage BLOCKs are recorded, not dropped

    @property
    def artifact(self) -> Artifact:
        return self.request.artifact


def _free_text(obj: Any) -> list[str]:
    """Free-text strings in a payload (whitespace-bearing, > 20 chars) — where personal data hides."""
    out: list[str] = []
    if isinstance(obj, dict):
        for v in obj.values():
            out += _free_text(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _free_text(v)
    elif isinstance(obj, str) and len(obj) > 20 and " " in obj:
        out.append(obj)
    return out


def promote_blobs(repo: Repository, store: ObjectStore, tenant_id: str, file_refs: list[FileRef],
                  payload_sha256: str) -> list[FileRef]:
    """Copy an artifact's files and payload from quarantine/ to released/ once it reaches PASS
    (directly, or through a REVIEW resolution). The quarantine copy stays as the immutable original."""
    refs = []
    for ref in file_refs:
        blob = repo.get_blob(ref.blob_id) if ref.blob_id else repo.find_blob(tenant_id, ref.sha256)
        store.copy(blob.bucket, blob.object_key, "released")
        blob.status = "RELEASED"
        refs.append(ref.model_copy(update={"uri": make_uri("released", blob.object_key), "blob_id": blob.blob_id}))
    payload_blob = repo.find_blob(tenant_id, payload_sha256, "PAYLOAD")
    if payload_blob is not None:
        store.copy(payload_blob.bucket, payload_blob.object_key, "released")
        payload_blob.status = "RELEASED"
    return refs


class N0Pipeline:
    def __init__(self, db: Database, store: ObjectStore, docling: DoclingAdapter, presidio: PresidioAdapter,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.db, self.store, self.docling, self.presidio, self.clock = db, store, docling, presidio, clock

    # ==========================================================================================
    def evaluate(self, principal: Principal, request: EvaluateRequest) -> EvaluationResult:
        ctx = _Ctx(principal, request, self.clock(), sha256_hex(canonical_json(request.artifact.payload)))
        with self.db.session() as s:
            repo = Repository(s)
            self._stage(ctx, "N0.1", lambda: self.intake(repo, ctx))
            ctx.admitted = not ctx.f.has_block and ctx.revision is not None
            if ctx.admitted:
                self._stage(ctx, "N0.2", lambda: self.parse_and_normalize(ctx))
                self._stage(ctx, "N0.3", lambda: self.assemble_context(repo, ctx))
                self._stage(ctx, "N0.4", lambda: self.data_quality(repo, ctx))
                self._stage(ctx, "N0.5", lambda: self.hard_policy(ctx))
            return self._stage(ctx, "N0.6", lambda: self.release(repo, ctx))

    @staticmethod
    def _stage(ctx: _Ctx, name: str, fn: Callable[[], Any]) -> Any:
        t = time.perf_counter()
        try:
            return fn()
        finally:
            ctx.timings[name] = round((time.perf_counter() - t) * 1000, 3)

    # ------------------------------------------------------------------ N0.1
    def intake(self, repo: Repository, ctx: _Ctx) -> None:
        a, p, f = ctx.artifact, ctx.principal, ctx.f
        f.ran("tenant_isolation")
        if a.tenant_id != p.tenant_id:
            f.add("CROSS_TENANT_REFERENCE", f"principal {p.principal_id} (tenant {p.tenant_id}) submitted an "
                                            f"artifact for tenant {a.tenant_id}", "tenant_id")
            ctx.rights = RightsDecision(status="DENIED", source_id=a.source_id, processing_purpose=a.processing_purpose,
                                        purpose_permitted=False, llm_processing_allowed=False,
                                        vendor_transmission_allowed=False, training_allowed=False,
                                        reasons=["cross-tenant submission"])
            return
        f.ran("authorization")
        origin = ctx.request.origin_node
        if origin != "N0":
            if not (p.has("node_service") and origin in p.nodes):
                f.add("UNAUTHORIZED_PRODUCER", f"{p.principal_id} is not a node_service identity for {origin}",
                      "origin_node")
        elif not p.has("producer"):
            f.add("UNAUTHORIZED_PRODUCER", f"{p.principal_id} lacks the producer role for N0 intake", "origin_node")

        ctx.source = repo.get_source(a.tenant_id, a.source_id)
        ctx.rights = evaluate_rights(a.processing_purpose, a.source_id, ctx.source, ctx.now, f)
        if ctx.source is not None:
            check_clearance(ctx.source.confidentiality, p.clearance, f)

        f.ran("revision_control")
        ctx.revision = normalize_revision(a.revision)
        if ctx.revision is None:
            f.add("REVISION_FORMAT", f"revision {a.revision!r} is not A..ZZZ or a number", "revision")
        else:
            revs = repo.revisions(a.tenant_id, a.artifact_id)
            order = revision_order(ctx.revision)
            newer = [r for r in revs if r.revision_order > order]
            if newer:
                f.add("SUPERSEDED_REVISION", f"{a.artifact_id}@{ctx.revision} is superseded by "
                                             f"@{newer[-1].revision}", "revision")
            ctx.existing = next((r for r in revs if r.revision == ctx.revision), None)
            older = [r for r in revs if r.revision_order < order]
            ctx.previous = older[-1] if older else None
            if ctx.existing is not None and canonical_json(ctx.existing.submitted) != canonical_json(
                    self._submitted(ctx)):
                f.add("REVISION_IMMUTABLE", f"{a.artifact_id}@{ctx.revision} was already submitted with different "
                                            "content; submit a new revision", "revision")

        f.ran("integrity")
        for i, ref in enumerate(a.file_refs):
            self._check_file(repo, ctx, i, ref)
        expected = a.file_refs[0].sha256 if a.file_refs else ctx.payload_sha256
        if a.source_sha256 != expected:
            what = "the primary file" if a.file_refs else "the canonical payload JSON"
            f.add("SHA256_MISMATCH", f"source_sha256 {a.source_sha256[:12]}… does not match {what} "
                                     f"({expected[:12]}…)", "source_sha256")

    def _submitted(self, ctx: _Ctx) -> dict:
        return {**dump(ctx.artifact), "revision": ctx.revision}

    def _check_file(self, repo: Repository, ctx: _Ctx, i: int, ref: FileRef) -> None:
        a, f, where = ctx.artifact, ctx.f, f"file_refs[{i}]"
        try:
            _, key = parse_uri(ref.uri)
        except StorageError as exc:
            f.add("BLOB_NOT_CLEARED", str(exc), where)
            return
        uri_tenant, uri_sha = key.split("/", 1)
        if uri_tenant != a.tenant_id:
            f.add("CROSS_TENANT_REFERENCE", "file reference points into another tenant's storage", where)
            return
        if uri_sha != ref.sha256:
            f.add("SHA256_MISMATCH", "file_refs sha256 disagrees with the SHA-256 in its URI", where)
            return
        blob = repo.get_blob(ref.blob_id) if ref.blob_id else repo.find_blob(a.tenant_id, ref.sha256)
        if blob is not None and blob.tenant_id != a.tenant_id:
            f.add("CROSS_TENANT_REFERENCE", "file reference points to another tenant's blob", where)
            return
        if blob is None:
            f.add("BLOB_NOT_CLEARED", f"no uploaded file with SHA-256 {ref.sha256[:12]}… in this tenant", where)
            return
        if blob.sha256 != ref.sha256:
            f.add("SHA256_MISMATCH", f"declared SHA-256 {ref.sha256[:12]}… differs from the uploaded file's "
                                     f"{blob.sha256[:12]}…", where)
            return
        if blob.status not in ("SCAN_PASSED", "RELEASED"):
            f.add("BLOB_NOT_CLEARED", f"file {blob.blob_id} is {blob.status}", where)
            return
        if blob.source_id != a.source_id:
            f.add("SOURCE_MISMATCH", f"file {blob.blob_id} was uploaded under source {blob.source_id}, not "
                                     f"{a.source_id}", where)
            return
        data = self.store.get(blob.bucket, blob.object_key)
        if sha256_hex(data) != blob.sha256:
            f.add("SHA256_MISMATCH", f"stored bytes of {blob.blob_id} no longer match their SHA-256", where)
            return
        ctx.documents.append((blob, data))

    # ------------------------------------------------------------------ N0.2
    def parse_and_normalize(self, ctx: _Ctx) -> None:
        a, f = ctx.artifact, ctx.f
        f.ran("parsing")
        texts = []
        for i, (blob, data) in enumerate(ctx.documents):
            if blob.sniffed_mime not in PARSEABLE:
                continue
            try:
                texts.append(self.docling.parse(data, blob.sniffed_mime, blob.filename).text)
            except ParseError as exc:
                f.add("PARSE_FAILED", str(exc), f"file_refs[{i}]")
        ctx.document_text = "\n\n".join(texts) if texts else None

        f.ran("pii")
        for where, text in ([("document", ctx.document_text)] if ctx.document_text else []) + \
                [("payload", t) for t in _free_text(a.payload)]:
            for hit in self.presidio.analyze(text):
                ctx.pii.append({"location": where, **hit.to_dict()})
        if ctx.document_text:
            ctx.redacted_text, _ = self.presidio.redact(ctx.document_text)
        if ctx.pii and not (ctx.source and "PERSONAL_DATA_PROCESSING" in ctx.source.permitted_use):
            kinds = sorted({h["entity_type"] for h in ctx.pii})
            f.add("PII_DETECTED", f"personal data ({', '.join(kinds)}) without PERSONAL_DATA_PROCESSING rights")

        ctx.list_field = REQUIREMENT_LISTS.get(a.artifact_type)
        raw = a.payload.get(ctx.list_field) if ctx.list_field else None
        parsed: list[Requirement] = []
        for item in raw if isinstance(raw, list) else []:
            try:
                parsed.append(Requirement.model_validate(item))
            except ValidationError:
                pass                                   # reported as SCHEMA_INVALID in N0.4
        ctx.reqs = normalize_requirements(parsed, ctx.list_field or "payload", f)

    # ------------------------------------------------------------------ N0.3
    def assemble_context(self, repo: Repository, ctx: _Ctx) -> None:
        a, f = ctx.artifact, ctx.f
        f.ran("classification")
        detected = classify(a.payload)
        if detected is not None and detected != a.artifact_type:
            f.add("ARTIFACT_TYPE_MISMATCH", f"payload looks like {detected}, declared {a.artifact_type}",
                  "artifact_type")

        f.ran("upstream_resolution")
        for ref in a.source_artifact_ids:
            uid, raw_rev = ref.split("@", 1)
            urev = normalize_revision(raw_rev) or raw_rev
            row = repo.get_artifact(a.tenant_id, uid, urev)
            if row is None:
                f.add("UPSTREAM_NOT_FOUND", f"{uid}@{urev} does not resolve in this tenant", "source_artifact_ids")
                continue
            latest = repo.latest_revision(a.tenant_id, uid)
            if latest is not None and latest.revision_order > row.revision_order:
                f.add("UPSTREAM_SUPERSEDED", f"{uid}@{urev} is superseded by @{latest.revision}",
                      "source_artifact_ids")
            d = repo.latest_decision(a.tenant_id, uid, urev)
            if d is None or d.decision != "PASS":
                f.add("UPSTREAM_NOT_RELEASED", f"{uid}@{urev} is {d.decision if d else 'undecided'}, not PASS",
                      "source_artifact_ids")
            ctx.upstream.append(f"{uid}@{urev}")

        run_id = a.payload.get("extraction_model_run_id") or (
            a.payload.get("model_run_id") if a.artifact_type == "MODEL_OUTPUT" else None)
        if isinstance(run_id, str) and run_id:
            run = repo.get_model_run(a.tenant_id, run_id)
            if run is None:
                f.add("UPSTREAM_NOT_FOUND", f"model run {run_id} does not resolve in this tenant",
                      "payload.extraction_model_run_id")
            else:
                ctx.model_runs.append(run)
                if ctx.document_text is None and run.blob_id:
                    self._load_run_document(repo, ctx, run)

        haystack = "\n".join(t for t in (ctx.document_text, ctx.redacted_text) if t) or None
        evidence_checks(ctx.reqs, haystack, f)
        f.ran("completeness")
        ctx.missing = missing_specs(a.artifact_type, a.payload, ctx.reqs)

    def _load_run_document(self, repo: Repository, ctx: _Ctx, run: ModelRun) -> None:
        blob = repo.get_blob(run.blob_id)
        if blob is None or blob.tenant_id != ctx.artifact.tenant_id or blob.sniffed_mime not in PARSEABLE:
            return
        try:
            text = self.docling.parse(self.store.get(blob.bucket, blob.object_key), blob.sniffed_mime,
                                      blob.filename).text
        except (ParseError, StorageError):
            return
        ctx.document_text = text
        ctx.redacted_text, _ = self.presidio.redact(text)

    # ------------------------------------------------------------------ N0.4
    def data_quality(self, repo: Repository, ctx: _Ctx) -> None:
        a, f = ctx.artifact, ctx.f
        model, missing_required = schema_checks(a, f)
        ctx.schema_valid = model is not None
        ctx.derived = derive_values(ctx.reqs)
        lf = ctx.list_field or "payload"
        physics_checks(ctx.reqs, ctx.derived, lf, f)
        consistency_checks(ctx.reqs, lf, f)
        provenance_checks(a.artifact_type, a.payload, ctx.reqs, ctx.derived, f)
        freshness_checks(a.artifact_type, a.payload, ctx.now, f)
        market_checks(a.artifact_type, a.payload, a.synthetic, f)
        design_checks(a.artifact_type, a.payload, f)
        f.ran("duplicate_content")
        dupes = repo.same_content_elsewhere(a.tenant_id, a.artifact_id, ctx.payload_sha256)
        if dupes:
            f.add("DUPLICATE_CONTENT", f"identical payload already submitted as {', '.join(dupes[:3])}", "payload")
        ctx.missing = sorted(set(ctx.missing) | set(missing_required))
        for m in ctx.missing:
            f.add("MISSING_CRITICAL_FIELD", f"{m} is required", m)

    # ------------------------------------------------------------------ N0.5
    def hard_policy(self, ctx: _Ctx) -> None:
        hard_policy_checks(ctx.artifact, ctx.source, ctx.reqs, ctx.now, ctx.f)

    # ------------------------------------------------------------------ N0.6
    def release(self, repo: Repository, ctx: _Ctx) -> EvaluationResult:
        a, f, p = ctx.artifact, ctx.f, ctx.principal
        f.ran("release_routing")
        decision = decide(f.items)
        destinations = allowed_destinations(decision, a.artifact_type, a.processing_purpose,
                                            ctx.request.origin_node, ctx.request.requested_destinations)
        revision = ctx.revision or a.revision
        gd = GatewayDecision(
            decision_id=new_id("DEC"), tenant_id=a.tenant_id, artifact_id=a.artifact_id, revision=revision,
            decision=decision, violated_rules=f.items, missing_fields=ctx.missing, rights_decision=ctx.rights,
            explanation=explain(decision, f.items, ctx.missing), allowed_destinations=destinations,
            validator_version=VALIDATOR_VERSION, quality_score=quality_score(f.items),
            origin_node=ctx.request.origin_node, decided_at=ctx.now)

        canonical: Optional[Artifact] = None
        payload_blob_id: Optional[str] = None
        if ctx.admitted:
            payload_blob_id = self._store_payload(repo, ctx)
            canonical = self._canonical(ctx, promote=decision == "PASS", repo=repo)
            if ctx.existing is None:
                repo.add_artifact(ArtifactRow(
                    tenant_id=a.tenant_id, artifact_id=a.artifact_id, revision=revision,
                    revision_order=revision_order(revision), project_id=a.project_id, artifact_type=a.artifact_type,
                    release_stage=a.release_stage, synthetic=a.synthetic, source_id=a.source_id,
                    source_sha256=a.source_sha256, payload_sha256=ctx.payload_sha256, submitted=self._submitted(ctx),
                    canonical=dump(canonical), submitted_by=p.principal_id, origin_node=ctx.request.origin_node))
            repo.add_decision(gd)

        manifest = ReleaseManifest(
            manifest_id=new_id("REL"), tenant_id=a.tenant_id, artifact_id=a.artifact_id, revision=revision,
            status="RELEASABLE" if decision == "PASS" else "NOT_RELEASED", source_node=ctx.request.origin_node,
            allowed_destinations=destinations, source_sha256=a.source_sha256, payload_sha256=ctx.payload_sha256,
            decision_id=gd.decision_id, issued_at=ctx.now, validator_version=VALIDATOR_VERSION)
        if ctx.admitted:
            manifest = repo.add_release(manifest)
            self._lineage(repo, ctx, gd, manifest, payload_blob_id)

        provenance = Provenance(source_id=a.source_id, source_sha256=a.source_sha256, revision=revision,
                                payload_sha256=ctx.payload_sha256, file_refs=a.file_refs, upstream=ctx.upstream,
                                model_run_ids=[r.run_id for r in ctx.model_runs])
        quality = QualityReport(violated_rules=f.items, error_codes=f.codes, missing_fields=ctx.missing,
                                checks_run=f.checks, derived_values=ctx.derived, pii_findings=ctx.pii,
                                quality_score=gd.quality_score)
        audit = self._audit(repo, ctx, gd, manifest)
        return EvaluationResult(canonical_artifact=canonical, provenance=provenance, quality_report=quality,
                                rights_decision=ctx.rights, decision=gd, release_manifest=manifest, audit_event=audit)

    def _store_payload(self, repo: Repository, ctx: _Ctx) -> str:
        """Quarantine-first: the canonical JSON of every admitted payload is stored write-once."""
        a = ctx.artifact
        existing = repo.find_blob(a.tenant_id, ctx.payload_sha256, "PAYLOAD")
        if existing is not None:
            return existing.blob_id
        data = canonical_json(a.payload)
        key = self.store.key_for(a.tenant_id, ctx.payload_sha256)
        self.store.put_immutable("quarantine", key, data)
        row = repo.add_blob(BlobRow(blob_id=new_id("BLB"), tenant_id=a.tenant_id, source_id=a.source_id,
                                    kind="PAYLOAD", sha256=ctx.payload_sha256, size_bytes=len(data),
                                    filename=f"{a.artifact_id}@{ctx.revision}.payload.json",
                                    declared_mime="application/json", sniffed_mime="application/json",
                                    status="SCAN_PASSED", scan={"engine": "n/a (structured payload)"},
                                    bucket="quarantine", object_key=key, created_by=ctx.principal.principal_id))
        return row.blob_id

    def _canonical(self, ctx: _Ctx, promote: bool, repo: Repository) -> Artifact:
        a = ctx.artifact
        payload = dict(a.payload)
        if ctx.schema_valid and ctx.list_field:
            recomputed = {d.parameter for d in ctx.derived}
            kept = [r for r in ctx.reqs if not (r.value_origin == "DERIVED" and r.parameter in recomputed)]
            payload[ctx.list_field] = [r.model_dump(mode="json") for r in kept + ctx.derived]
        refs = promote_blobs(repo, self.store, a.tenant_id, a.file_refs, ctx.payload_sha256) if promote \
            else list(a.file_refs)
        return a.model_copy(update={"revision": ctx.revision, "payload": payload, "file_refs": refs})

    def _lineage(self, repo: Repository, ctx: _Ctx, gd: GatewayDecision, manifest: ReleaseManifest,
                 payload_blob_id: Optional[str]) -> None:
        a, t = ctx.artifact, ctx.artifact.tenant_id
        node = f"artifact:{a.artifact_id}@{ctx.revision}"
        repo.add_edge(t, f"source:{a.source_id}", node, "SOURCE_OF")
        for blob, _ in ctx.documents:
            repo.add_edge(t, f"blob:{blob.blob_id}", node, "FILE_OF", sha256=blob.sha256)
        if payload_blob_id:
            repo.add_edge(t, f"blob:{payload_blob_id}", node, "PAYLOAD_OF", sha256=ctx.payload_sha256)
        for up in ctx.upstream:
            repo.add_edge(t, f"artifact:{up}", node, "UPSTREAM_OF")
        for run in ctx.model_runs:
            repo.add_edge(t, f"model_run:{run.run_id}", node, "EXTRACTED_INTO")
        if ctx.previous is not None:
            repo.add_edge(t, f"artifact:{a.artifact_id}@{ctx.previous.revision}", node, "SUPERSEDED_BY")
        repo.add_edge(t, node, f"decision:{gd.decision_id}", "EVALUATED_AS", decision=gd.decision)
        repo.add_edge(t, node, f"release:{manifest.manifest_id}", "MANIFEST", status=manifest.status)

    def _audit(self, repo: Repository, ctx: _Ctx, gd: GatewayDecision, manifest: ReleaseManifest) -> AuditEventOut:
        a, p = ctx.artifact, ctx.principal
        return repo.append_audit(
            tenant_id=p.tenant_id, event_type="GATEWAY_EVALUATION", actor_id=p.principal_id, actor_roles=p.roles,
            subject=f"{a.artifact_id}@{gd.revision}",
            details={"decision": gd.decision, "decision_id": gd.decision_id, "admitted": ctx.admitted,
                     "declared_tenant_id": a.tenant_id, "artifact_type": a.artifact_type,
                     "error_codes": ctx.f.codes, "missing_fields": ctx.missing,
                     "allowed_destinations": gd.allowed_destinations, "manifest_id": manifest.manifest_id,
                     "payload_sha256": ctx.payload_sha256, "source_sha256": a.source_sha256,
                     "origin_node": ctx.request.origin_node, "quality_score_advisory": gd.quality_score},
            model_runs=[{"run_id": r.run_id, "model_name": r.model_name, "model_version": r.model_version,
                         "provider": r.provider, "prompt_revision": r.prompt_revision,
                         "estimated_cost_usd": r.estimated_cost_usd} for r in ctx.model_runs],
            stage_timings_ms=ctx.timings)
