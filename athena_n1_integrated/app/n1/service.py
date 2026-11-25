"""N1 orchestration over the N0 gateway.

* intake — the N0 gate dependency. N1 reads only MARKET_BATCH / BUYER_RECORD artifacts whose latest
  N0 decision is PASS, are routed to N1, are not superseded, still have granted source rights, and
  whose stored payload bytes still hash to the recorded SHA-256.
* previews / imports — connector output is staged with its raw-payload hash, then imported as an N0
  artifact. Nothing reaches N1 without N0.
* attestations, reports, optimization, feedback, SLM extraction, explanations.
* handoff — packages for N2 (RFQ seed), N6 (shortlist) and N7 (SAM evidence) are submitted to N0 as
  the N1 service identity, then released by the human approver through N0's RELEASE approval and
  handoff (four-eyes: the submitter is the service, the approver is a person).

Each step runs in its own short transaction, so N0's pipeline (which commits on its own) never
contends with an open N1 write on SQLite.
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Optional

from ..db import BlobRow
from ..errors import N0Error
from ..governance import record_approval, release_handoff
from ..llm_adapter import (LLMBoundaryError, LLMDisabledError, LLMError, LLMOutputInvalid, LLMRefusalError,
                           estimate_cost_usd)
from ..models import (ApprovalRequest, Artifact, BuyerRecordPayload, EvaluateRequest, HandoffReleaseRequest,
                      MarketBatchPayload, MarketObservation, ModelRun, TokenUsage, utcnow)
from ..repository import Repository, dump, new_id
from ..rules import Findings, canonical_json, evaluate_rights, normalize_revision, revision_order, sha256_hex
from ..security import Principal
from ..storage import make_uri
from . import engine
from . import llm_adapter as explainer
from . import slm_extractor as slm
from .connectors import ConnectorError, make_connector, redact
from .db import N1BatchRow, N1ObservationRow
from .models import (ActionOptimizationRequest, ActionOptimizationResult, AttestationRequest, BatchSubmission,
                     BuyerAccount, BuyerSubmission, ConnectorImport, ConnectorPreview, ConnectorPreviewRequest,
                     EvidenceAttestation, FeedbackCase, FeedbackRequest, HandoffPackage, HandoffRequest,
                     HandoffResult, IntakeResult, MarketBatch, MarketReport, ReportExplanation, ReportRequest,
                     ReportSummary, SlmEvaluation, SlmExtractionRequest, SlmExtractionResult, SubmissionResult)
from .store import N1Store

MARKET_TYPES = ("MARKET_BATCH", "BUYER_RECORD")
PACKAGE_PURPOSE = {"N2": "RFQ_ANALYSIS", "N6": "COMMERCIAL", "N7": "FINANCE"}


def _obs_sha(obs: MarketObservation) -> str:
    return sha256_hex(canonical_json(obs.model_dump(mode="json")))


def records_to_observations(p: BuyerRecordPayload) -> list[MarketObservation]:
    out = []
    for r in p.records:
        cats = r.service_categories
        out.append(MarketObservation(
            evidence_id=r.record_id, observation_type="CRM_RECORD" if r.record_type == "CRM_ACCOUNT" else "BUYER_INTERVIEW",
            source_connector="CRM" if r.record_type == "CRM_ACCOUNT" else "INTERVIEW", source_url=r.source_url,
            buyer_name=r.buyer_name, buyer_identifiers=r.buyer_identifiers, buyer_country=r.buyer_country,
            buyer_type=r.buyer_type, description=r.stated_need, service_category=cats[0] if len(cats) == 1 else None,
            amount=r.stated_budget, amount_kind="STATED_SPEND" if r.stated_budget else None,
            budget_status=r.budget_status, published_at=r.recorded_at, relationship_stage=r.relationship_stage,
            do_not_contact=r.do_not_contact, synthetic=r.synthetic))
    return out


class N1Service:
    def __init__(self, state, settings, *, slm_provider=None, explainer_provider=None, transport=None,
                 limiter_factory: Optional[Callable[[str], Any]] = None) -> None:
        self.db, self.store, self.pipeline = state.db, state.store, state.pipeline
        self.presidio, self.clock = state.presidio, state.clock
        self.settings, self.transport, self.limiter_factory = settings, transport, limiter_factory
        self.slm = slm_provider if slm_provider is not None else slm.make_slm(settings)
        self.explainer = explainer_provider if explainer_provider is not None else explainer.make_explainer(settings)

    # ==========================================================================================
    # submission → N0 → intake
    # ==========================================================================================
    def _submit(self, p: Principal, *, kind: str, tenant_id: str, project_id: str, source_id: str,
                payload: dict, synthetic: bool, artifact_id: str, revision: str) -> SubmissionResult:
        if p.tenant_id != tenant_id:
            raise N0Error(403, "CROSS_TENANT_REFERENCE", "cross-tenant submission is not permitted")
        artifact = Artifact(tenant_id=tenant_id, project_id=project_id, artifact_id=artifact_id, artifact_type=kind,
                            source_id=source_id, processing_purpose="MARKET_INTELLIGENCE", release_stage="INTAKE",
                            revision=revision, synthetic=synthetic, source_sha256=sha256_hex(canonical_json(payload)),
                            payload=payload)
        res = self.pipeline.evaluate(p, EvaluateRequest(artifact=artifact, origin_node="N0",
                                                        requested_destinations=["N1"]))
        if res.decision.decision != "PASS":
            return SubmissionResult(n0_decision=res.decision)
        return SubmissionResult(n0_decision=res.decision, intake=self.intake(p, artifact_id, res.decision.revision))

    def submit_batch(self, p: Principal, sub: BatchSubmission) -> SubmissionResult:
        return self._submit(p, kind="MARKET_BATCH", tenant_id=sub.tenant_id, project_id=sub.project_id,
                            source_id=sub.source_id, payload=sub.batch.model_dump(mode="json"),
                            synthetic=sub.synthetic, artifact_id=sub.artifact_id or f"MB-{sub.batch.batch_id}",
                            revision=sub.revision)

    def submit_buyers(self, p: Principal, sub: BuyerSubmission) -> SubmissionResult:
        return self._submit(p, kind="BUYER_RECORD", tenant_id=sub.tenant_id, project_id=sub.project_id,
                            source_id=sub.source_id, payload=sub.records.model_dump(mode="json"),
                            synthetic=sub.synthetic, artifact_id=sub.artifact_id or f"BR-{sub.records.batch_id}",
                            revision=sub.revision)

    def intake(self, p: Principal, artifact_id: str, revision: str) -> IntakeResult:
        rev = normalize_revision(revision)
        if rev is None:
            raise N0Error(422, "REVISION_FORMAT", f"revision {revision!r} is not A..ZZZ or a number")
        now = self.clock()
        with self.db.session() as s:
            repo, st = Repository(s), N1Store(s)
            row = repo.get_artifact(p.tenant_id, artifact_id, rev)
            if row is None:
                raise N0Error(404, "N0_ARTIFACT_NOT_FOUND", f"{artifact_id}@{rev} was not admitted by N0 in this tenant")
            if row.artifact_type not in MARKET_TYPES:
                raise N0Error(422, "NOT_A_MARKET_ARTIFACT", f"N1 ingests {MARKET_TYPES}, not {row.artifact_type}")
            decision = repo.latest_decision(p.tenant_id, artifact_id, rev)
            if decision is None or decision.decision != "PASS":
                raise N0Error(409, "N0_NOT_PASS", f"N0 decision for {artifact_id}@{rev} is "
                                                  f"{decision.decision if decision else 'missing'}; N1 ingests PASS only",
                              decision_id=decision.decision_id if decision else None)
            if "N1" not in decision.allowed_destinations:
                raise N0Error(409, "N0_NOT_ROUTED_TO_N1", f"allowed_destinations {decision.allowed_destinations}")
            latest = repo.latest_revision(p.tenant_id, artifact_id)
            if latest is not None and latest.revision_order > row.revision_order:
                raise N0Error(409, "SUPERSEDED_REVISION", f"{artifact_id}@{rev} is superseded by @{latest.revision}")
            source = repo.get_source(p.tenant_id, row.source_id)
            rights = evaluate_rights(row.canonical["processing_purpose"], row.source_id, source, now, Findings())
            if rights.status != "GRANTED":
                raise N0Error(409, "N0_RIGHTS_DENIED", f"source rights are no longer granted: {rights.reasons}")
            payload = row.submitted["payload"]
            blob = repo.find_blob(p.tenant_id, row.payload_sha256, "PAYLOAD")
            stored_ok = blob is not None and sha256_hex(self.store.get(blob.bucket, blob.object_key)) == row.payload_sha256
            if sha256_hex(canonical_json(payload)) != row.payload_sha256 or not stored_ok:
                raise N0Error(409, "SHA256_MISMATCH", f"stored payload of {artifact_id}@{rev} no longer matches its "
                                                      "recorded SHA-256")
            existing = st.get_batch(p.tenant_id, artifact_id, rev)
            if existing is not None:
                return IntakeResult.model_validate({**existing.record["intake"], "already_ingested": True})

            if row.artifact_type == "MARKET_BATCH":
                parsed = MarketBatchPayload.model_validate(payload)
                observations, connector = parsed.observations, parsed.connector
                raw_sha = parsed.raw_payload_sha256
            else:
                parsed = BuyerRecordPayload.model_validate(payload)
                observations, connector, raw_sha = records_to_observations(parsed), parsed.connector, None
            accounts = st.latest_accounts(p.tenant_id)
            new_revs, unchanged, touched = [], [], []
            for obs in observations:
                if row.synthetic and not obs.synthetic:
                    obs = obs.model_copy(update={"synthetic": True})
                ev_sha = _obs_sha(obs)
                prev = st.latest_observation(p.tenant_id, obs.evidence_id)
                if prev is not None and prev.evidence_sha256 == ev_sha:
                    unchanged.append(obs.evidence_id)
                acc_id = self._resolve_account(st, accounts, p.tenant_id, obs, now) if obs.buyer_name else None
                if acc_id:
                    touched.append(acc_id)
                number = (prev.revision + 1) if prev else 1
                st.add_observation(N1ObservationRow(tenant_id=p.tenant_id, evidence_id=obs.evidence_id, revision=number,
                                                    evidence_sha256=ev_sha, n0_artifact_id=artifact_id, n0_revision=rev,
                                                    account_id=acc_id, synthetic=obs.synthetic,
                                                    record=obs.model_dump(mode="json")))
                new_revs.append(f"{obs.evidence_id}@{number}")
                repo.add_edge(p.tenant_id, f"n1_batch:{artifact_id}@{rev}", f"n1_evidence:{obs.evidence_id}@{number}",
                              "CONTAINS")
                if acc_id:
                    repo.add_edge(p.tenant_id, f"n1_evidence:{obs.evidence_id}@{number}", f"n1_account:{acc_id}",
                                  "RESOLVED_TO")
            result = IntakeResult(kind=row.artifact_type, batch_id=parsed.batch_id, n0_artifact_id=artifact_id,
                                  n0_revision=rev, n0_decision_id=decision.decision_id,
                                  payload_sha256=row.payload_sha256, synthetic=row.synthetic,
                                  observations_ingested=len(observations), new_evidence_revisions=new_revs,
                                  unchanged_evidence=unchanged, accounts=sorted(set(touched)))
            batch = MarketBatch(tenant_id=p.tenant_id, kind=row.artifact_type, batch_id=parsed.batch_id,
                                connector=connector, source_id=row.source_id, retrieved_at=parsed.retrieved_at,
                                synthetic=row.synthetic, n0_artifact_id=artifact_id, n0_revision=rev,
                                n0_decision_id=decision.decision_id, payload_sha256=row.payload_sha256,
                                raw_payload_sha256=raw_sha, evidence_ids=[o.evidence_id for o in observations],
                                ingested_by=p.principal_id, ingested_at=now)
            st.add_batch(N1BatchRow(tenant_id=p.tenant_id, n0_artifact_id=artifact_id, n0_revision=rev,
                                    kind=row.artifact_type, batch_id=parsed.batch_id, source_id=row.source_id,
                                    synthetic=row.synthetic, payload_sha256=row.payload_sha256,
                                    n0_decision_id=decision.decision_id, submitted_by=row.submitted_by,
                                    record={"batch": dump(batch), "intake": dump(result)}))
            repo.add_edge(p.tenant_id, f"artifact:{artifact_id}@{rev}", f"n1_batch:{artifact_id}@{rev}", "INGESTED_BY_N1",
                          decision_id=decision.decision_id)
            repo.append_audit(tenant_id=p.tenant_id, event_type="N1_INTAKE", actor_id=p.principal_id,
                              actor_roles=p.roles, subject=f"{artifact_id}@{rev}",
                              details={"kind": row.artifact_type, "observations": len(observations),
                                       "accounts": result.accounts, "synthetic": row.synthetic,
                                       "decision_id": decision.decision_id})
            return result

    def _resolve_account(self, st: N1Store, accounts: dict[str, BuyerAccount], tenant: str, obs: MarketObservation,
                         now: datetime) -> str:
        match, method = engine.match_account(obs.buyer_name, obs.buyer_identifiers, obs.buyer_country,
                                             accounts.values())
        from_crm = obs.observation_type in ("CRM_RECORD", "BUYER_INTERVIEW")
        cat, _ = engine.tag_category(obs)
        if match is None:
            norm = engine.normalize_name(obs.buyer_name)
            acc = BuyerAccount(account_id=engine.account_id_for(tenant, obs.buyer_country, norm), tenant_id=tenant,
                               revision=1, legal_name=obs.buyer_name, normalized_name=norm,
                               country=obs.buyer_country, buyer_type=obs.buyer_type,
                               identifiers=dict(obs.buyer_identifiers),
                               relationship_stage=obs.relationship_stage or "NONE",
                               do_not_contact=obs.do_not_contact, named_buyer_validated=from_crm and not obs.synthetic,
                               service_categories=[cat] if cat else [], evidence_ids=[obs.evidence_id],
                               synthetic_only=obs.synthetic, match_method="NEW", updated_at=now)
            if acc.account_id in accounts:                      # same normalized name + country already exists
                match, method = accounts[acc.account_id], "EXACT_NAME"
            else:
                st.add_account(acc)
                accounts[acc.account_id] = acc
                return acc.account_id
        merged = match.model_copy(deep=True)
        if obs.buyer_name != match.legal_name and obs.buyer_name not in merged.aliases:
            merged.aliases = sorted({*merged.aliases, obs.buyer_name})
        for k, v in obs.buyer_identifiers.items():
            merged.identifiers.setdefault(k, v)
        merged.country = merged.country or obs.buyer_country
        merged.buyer_type = merged.buyer_type or obs.buyer_type
        if from_crm:
            merged.relationship_stage = obs.relationship_stage or merged.relationship_stage
            merged.do_not_contact = obs.do_not_contact
            merged.named_buyer_validated = merged.named_buyer_validated or not obs.synthetic
        if cat and cat not in merged.service_categories:
            merged.service_categories = sorted({*merged.service_categories, cat})
        if obs.evidence_id not in merged.evidence_ids:
            merged.evidence_ids = sorted({*merged.evidence_ids, obs.evidence_id})
        merged.synthetic_only = match.synthetic_only and obs.synthetic
        merged.match_method = method
        strip = {"revision", "updated_at", "match_method"}
        if merged.model_dump(exclude=strip) != match.model_dump(exclude=strip):
            merged = merged.model_copy(update={"revision": match.revision + 1, "updated_at": now})
            st.add_account(merged)
            accounts[merged.account_id] = merged
        return match.account_id

    # ==========================================================================================
    # connectors
    # ==========================================================================================
    def preview(self, p: Principal, name: str, req: ConnectorPreviewRequest) -> ConnectorPreview:
        if p.tenant_id != req.tenant_id:
            raise N0Error(403, "CROSS_TENANT_REFERENCE", "cross-tenant access is not permitted")
        limiter = self.limiter_factory(name) if self.limiter_factory else None
        conn = make_connector(name, self.settings, self.transport, limiter)
        now = self.clock()
        if req.mode == "offline":
            if req.raw_payload is None:
                raise ConnectorError(422, "RAW_PAYLOAD_REQUIRED", "offline mode normalizes a supplied raw_payload")
            raw = req.raw_payload.encode() if isinstance(req.raw_payload, str) else \
                json.dumps(req.raw_payload, sort_keys=True, ensure_ascii=False).encode()
            request_meta, mode = {"mode": "offline"}, "offline"
        else:
            fetched = conn.fetch(req.query)
            raw, request_meta, mode = fetched.raw, fetched.request, fetched.mode
        raw = redact(raw, conn.secrets())
        raw_sha = sha256_hex(raw)
        key = self.store.key_for(p.tenant_id, raw_sha)
        raw_uri = self.store.put_immutable("quarantine", key, raw)
        norm = conn.normalize(conn.parse_raw(raw), now)
        observations = [o.model_copy(update={"raw_sha256": raw_sha, "source_url": o.source_url or raw_uri})
                        for o in norm.observations]
        preview = ConnectorPreview(preview_id=new_id("PRV"), tenant_id=p.tenant_id, connector=name, mode=mode,
                                   request=request_meta, retrieved_at=now, raw_sha256=raw_sha, raw_uri=raw_uri,
                                   record_count=norm.record_count, observations=observations,
                                   buyer_records=norm.buyer_records, warnings=norm.warnings, created_by=p.principal_id)
        with self.db.session() as s:
            repo = Repository(s)
            blob = repo.find_blob(p.tenant_id, raw_sha, "RAW")
            if blob is None:
                blob = repo.add_blob(BlobRow(blob_id=new_id("BLB"), tenant_id=p.tenant_id, source_id=f"connector:{name}",
                                             kind="RAW", sha256=raw_sha, size_bytes=len(raw),
                                             filename=f"{name}-{now:%Y%m%dT%H%M%S}.raw", declared_mime=None,
                                             sniffed_mime="application/json", status="QUARANTINED",
                                             scan={"engine": "n/a (connector payload, not executable content)"},
                                             bucket="quarantine", object_key=key, created_by=p.principal_id))
            N1Store(s).add_preview(preview)
            repo.add_edge(p.tenant_id, f"blob:{blob.blob_id}", f"n1_preview:{preview.preview_id}", "NORMALIZED_INTO")
            repo.append_audit(tenant_id=p.tenant_id, event_type="N1_CONNECTOR_PREVIEW", actor_id=p.principal_id,
                              actor_roles=p.roles, subject=f"n1_preview:{preview.preview_id}",
                              details={"connector": name, "mode": mode, "raw_sha256": raw_sha,
                                       "records": norm.record_count, "warnings": len(norm.warnings),
                                       "request": request_meta})
        return preview

    def import_preview(self, p: Principal, req: ConnectorImport) -> SubmissionResult:
        if p.tenant_id != req.tenant_id:
            raise N0Error(403, "CROSS_TENANT_REFERENCE", "cross-tenant access is not permitted")
        with self.db.session() as s:
            prev = N1Store(s).get_preview(p.tenant_id, req.preview_id)
        if prev is None:
            raise N0Error(404, "PREVIEW_NOT_FOUND", f"{req.preview_id} not found in this tenant")
        keep = set(req.evidence_ids) if req.evidence_ids else None
        if prev.connector == "crm":
            records = [r for r in prev.buyer_records if keep is None or r.record_id in keep]
            if not records:
                raise N0Error(422, "NOTHING_TO_IMPORT", "no previewed records selected")
            payload = BuyerRecordPayload(batch_id=prev.preview_id, connector="CRM", retrieved_at=prev.retrieved_at,
                                         records=[r.model_copy(update={"synthetic": r.synthetic or req.synthetic})
                                                  for r in records])
            return self.submit_buyers(p, BuyerSubmission(tenant_id=req.tenant_id, project_id=req.project_id,
                                                         source_id=req.source_id, records=payload,
                                                         synthetic=req.synthetic))
        obs = [o for o in prev.observations if keep is None or o.evidence_id in keep]
        if not obs:
            raise N0Error(422, "NOTHING_TO_IMPORT", "no previewed observations selected")
        batch = MarketBatchPayload(batch_id=prev.preview_id, connector=obs[0].source_connector,
                                   retrieved_at=prev.retrieved_at, query=prev.request,
                                   raw_payload_sha256=prev.raw_sha256,
                                   observations=[o.model_copy(update={"synthetic": o.synthetic or req.synthetic})
                                                 for o in obs])
        return self.submit_batch(p, BatchSubmission(tenant_id=req.tenant_id, project_id=req.project_id,
                                                    source_id=req.source_id, batch=batch, synthetic=req.synthetic))

    # ==========================================================================================
    # attestation & feedback
    # ==========================================================================================
    def attest(self, p: Principal, req: AttestationRequest) -> EvidenceAttestation:
        with self.db.session() as s:
            repo, st = Repository(s), N1Store(s)
            row = st.latest_observation(p.tenant_id, req.evidence_id)
            if row is None:
                raise N0Error(404, "EVIDENCE_NOT_FOUND", f"{req.evidence_id} not found in this tenant")
            if row.synthetic and req.status == "ATTESTED":
                raise N0Error(422, "SYNTHETIC_NOT_ATTESTABLE", "synthetic evidence can never be attested as real")
            batch = st.get_batch(p.tenant_id, row.n0_artifact_id, row.n0_revision)
            if batch is not None and batch.submitted_by == p.principal_id:
                raise N0Error(403, "FOUR_EYES", "the submitter of the evidence cannot attest it")
            event = repo.append_approval(
                tenant_id=p.tenant_id, artifact_id=f"n1_evidence:{req.evidence_id}", exact_revision=str(row.revision),
                approver_id=p.principal_id, approver_role="approver",
                decision="APPROVED" if req.status == "ATTESTED" else "REJECTED", scope="EVIDENCE_ATTESTATION",
                reason=f"{req.status} via {req.method}: {req.notes}"[:2000])
            att = EvidenceAttestation(attestation_id=new_id("ATT"), tenant_id=p.tenant_id, evidence_id=req.evidence_id,
                                      evidence_revision=row.revision, evidence_sha256=row.evidence_sha256,
                                      status=req.status, method=req.method, notes=req.notes,
                                      attested_by=p.principal_id, attested_at=self.clock(),
                                      approval_event_id=event.approval_id)
            st.add_attestation(att)
            repo.add_edge(p.tenant_id, f"n1_attestation:{att.attestation_id}",
                          f"n1_evidence:{req.evidence_id}@{row.revision}", "ATTESTS", status=req.status)
            repo.append_audit(tenant_id=p.tenant_id, event_type="N1_EVIDENCE_ATTESTATION", actor_id=p.principal_id,
                              actor_roles=p.roles, subject=f"n1_evidence:{req.evidence_id}@{row.revision}",
                              details={"status": req.status, "method": req.method,
                                       "attestation_id": att.attestation_id, "approval_id": event.approval_id})
            return att

    def pending_attestations(self, p: Principal) -> list[dict]:
        with self.db.session() as s:
            st = N1Store(s)
            atts = st.attestations_for(p.tenant_id)
            out = []
            for r in st.latest_observations(p.tenant_id):
                obs = MarketObservation.model_validate(r.record)
                spend, _ = engine.classify_spend(obs)
                a = atts.get(r.evidence_id)
                current = a is not None and a.evidence_sha256 == r.evidence_sha256
                if spend == "VERIFIED_BUDGET" and not r.synthetic and not current:
                    out.append({"evidence_id": r.evidence_id, "revision": r.revision, "account_id": r.account_id,
                                "observation_type": obs.observation_type, "source_url": obs.source_url,
                                "amount": str(obs.amount.amount) if obs.amount else None,
                                "currency": obs.amount.currency if obs.amount else None})
            return out

    def feedback(self, p: Principal, req: FeedbackRequest) -> FeedbackCase:
        with self.db.session() as s:
            repo, st = Repository(s), N1Store(s)
            synthetic = self._target_synthetic(repo, st, p.tenant_id, req.target_kind, req.target_id)
            case = FeedbackCase(case_id=new_id("FBC"), tenant_id=p.tenant_id, target_kind=req.target_kind,
                                target_id=req.target_id, label=req.label, corrected_value=req.corrected_value,
                                reason=req.reason, created_by=p.principal_id, created_at=self.clock(),
                                synthetic_target=synthetic, training_eligible=not synthetic)
            st.add_feedback(case)
            repo.append_audit(tenant_id=p.tenant_id, event_type="N1_FEEDBACK", actor_id=p.principal_id,
                              actor_roles=p.roles, subject=f"{req.target_kind}:{req.target_id}",
                              details={"case_id": case.case_id, "label": req.label,
                                       "training_eligible": case.training_eligible})
            return case

    def _target_synthetic(self, repo: Repository, st: N1Store, tenant: str, kind: str, target: str) -> bool:
        if kind == "EVIDENCE":
            row = st.latest_observation(tenant, target)
            if row is None:
                raise N0Error(404, "TARGET_NOT_FOUND", f"evidence {target} not found")
            return row.synthetic
        if kind in ("ACCOUNT", "OPPORTUNITY"):
            acc_id = target.removeprefix("OPP-")
            revs = st.account_revisions(tenant, acc_id)
            if not revs:
                raise N0Error(404, "TARGET_NOT_FOUND", f"account {acc_id} not found")
            return revs[-1].synthetic_only
        if kind == "REPORT":
            rep = st.get_report(tenant, target)
            if rep is None:
                raise N0Error(404, "TARGET_NOT_FOUND", f"report {target} not found")
            return all(f.synthetic for f in rep.evidence_features)
        if repo.get_model_run(tenant, target) is None:
            raise N0Error(404, "TARGET_NOT_FOUND", f"model run {target} not found")
        return False

    # ==========================================================================================
    # reports (N1.2 – N1.4, N1.6)
    # ==========================================================================================
    def _inputs(self, repo: Repository, st: N1Store, tenant: str, now: datetime) -> list[engine.EvidenceInput]:
        batches, atts = st.batches(tenant), st.attestations_for(tenant)
        rights_cache: dict[str, bool] = {}
        inputs = []
        for r in st.latest_observations(tenant):
            b = batches.get((r.n0_artifact_id, r.n0_revision))
            blocked = None
            if b is not None:
                if b.source_id not in rights_cache:
                    src = repo.get_source(tenant, b.source_id)
                    rights_cache[b.source_id] = evaluate_rights("MARKET_INTELLIGENCE", b.source_id, src, now,
                                                                Findings()).status == "GRANTED"
                if not rights_cache[b.source_id]:
                    blocked = f"rights for source {b.source_id} are no longer granted"
            inputs.append(engine.EvidenceInput(obs=MarketObservation.model_validate(r.record), revision=r.revision,
                                               sha256=r.evidence_sha256, account_id=r.account_id,
                                               n0_artifact=f"{r.n0_artifact_id}@{r.n0_revision}",
                                               attestation=atts.get(r.evidence_id), blocked=blocked))
        return inputs

    def build_report(self, p: Principal, req: ReportRequest) -> MarketReport:
        if p.tenant_id != req.tenant_id:
            raise N0Error(403, "CROSS_TENANT_REFERENCE", "cross-tenant access is not permitted")
        now = self.clock()
        as_of = req.as_of or now
        with self.db.session() as s:
            repo, st = Repository(s), N1Store(s)
            scope = engine.scope_key_for(p.tenant_id, req)
            prev = st.latest_report_row(p.tenant_id, scope)
            report = engine.build_report(
                report_id=new_id("RPT"), tenant_id=p.tenant_id, request=req, as_of=as_of, generated_at=now,
                generated_by=p.principal_id, scope_key=scope, revision=(prev.revision + 1) if prev else 1,
                supersedes=prev.report_id if prev else None, inputs=self._inputs(repo, st, p.tenant_id, now),
                accounts=st.latest_accounts(p.tenant_id), labeled_outcomes=st.labeled_outcomes(p.tenant_id))
            st.add_report(report)
            node = f"n1_report:{report.report_id}"
            if prev:
                repo.add_edge(p.tenant_id, f"n1_report:{prev.report_id}", node, "SUPERSEDED_BY")
            for c in report.citations:
                repo.add_edge(p.tenant_id, f"n1_evidence:{c.evidence_id}@{c.evidence_revision}", node, "CITED_BY")
            repo.append_audit(tenant_id=p.tenant_id, event_type="N1_REPORT", actor_id=p.principal_id,
                              actor_roles=p.roles, subject=node,
                              details={"scope": req.scope_name, "revision": report.revision,
                                       "report_sha256": report.report_sha256,
                                       "verified_real_sam": str(report.sam.verified_real_sam),
                                       "hypothetical_addressable_spend": str(report.sam.hypothetical_addressable_spend),
                                       "opportunities": len(report.opportunities),
                                       "excluded": len(report.exclusions)})
            return report

    def get_report(self, p: Principal, report_id: str) -> MarketReport:
        with self.db.session() as s:
            rep = N1Store(s).get_report(p.tenant_id, report_id)
        if rep is None:
            raise N0Error(404, "REPORT_NOT_FOUND", f"{report_id} not found in this tenant")
        return rep

    def list_reports(self, p: Principal, limit: int = 50) -> list[ReportSummary]:
        with self.db.session() as s:
            st = N1Store(s)
            rows = st.report_rows(p.tenant_id, limit)
            latest = {}
            for r in rows:
                cur = st.latest_report_row(p.tenant_id, r.scope_key)
                latest[r.scope_key] = cur.report_id if cur else None
            out = []
            for r in rows:
                rep = MarketReport.model_validate(r.record)
                out.append(ReportSummary(report_id=rep.report_id, scope_name=rep.scope_name, revision=rep.revision,
                                         as_of=rep.as_of, verified_real_sam=rep.sam.verified_real_sam,
                                         hypothetical_addressable_spend=rep.sam.hypothetical_addressable_spend,
                                         base_currency=rep.base_currency,
                                         superseded=latest.get(r.scope_key) != rep.report_id))
            return out

    # ==========================================================================================
    # N1.5 cost-weighted decisions (never executed)
    # ==========================================================================================
    def optimize(self, p: Principal, req: ActionOptimizationRequest) -> ActionOptimizationResult:
        if p.tenant_id != req.tenant_id:
            raise N0Error(403, "CROSS_TENANT_REFERENCE", "cross-tenant access is not permitted")
        now = self.clock()
        with self.db.session() as s:
            repo, st = Repository(s), N1Store(s)
            as_of = now
            if req.report_id:
                rep = st.get_report(p.tenant_id, req.report_id)
                if rep is None:
                    raise N0Error(404, "REPORT_NOT_FOUND", f"{req.report_id} not found in this tenant")
                as_of = rep.as_of
            accounts = st.latest_accounts(p.tenant_id)
            quiet = set()
            for r in st.latest_observations(p.tenant_id):
                o = MarketObservation.model_validate(r.record)
                acc = accounts.get(r.account_id or "")
                if (o.observation_type == "TENDER_NOTICE" and o.deadline_at and o.deadline_at > as_of and acc
                        and (acc.buyer_type == "PUBLIC" or o.source_connector in engine.OFFICIAL_CONNECTORS)):
                    quiet.add(acc.account_id)
            ranked, blocked, total = engine.optimize_actions(req, accounts, quiet)
            result = ActionOptimizationResult(optimization_id=new_id("OPT"), tenant_id=p.tenant_id,
                                              report_id=req.report_id, currency=req.currency, ranked=ranked,
                                              blocked=blocked, total_recommended_cost=total,
                                              created_by=p.principal_id, created_at=now)
            st.add_optimization(result)
            repo.append_audit(tenant_id=p.tenant_id, event_type="N1_OPTIMIZATION", actor_id=p.principal_id,
                              actor_roles=p.roles, subject=f"n1_optimization:{result.optimization_id}",
                              details={"ranked": len(ranked), "blocked": [b.action_id for b in blocked],
                                       "executed": False, "total_recommended_cost": str(total)})
            return result

    # ==========================================================================================
    # SLM extraction (gated by the held-out gold set)
    # ==========================================================================================
    def _redact(self, text: str) -> str:
        return self.presidio.redact(text)[0]

    def slm_evaluate(self, p: Principal) -> SlmEvaluation:
        if self.slm is None:
            raise N0Error(503, "SLM_DISABLED", "N1_SLM_PROVIDER is disabled")
        gold, gold_sha = slm.load_gold(self.settings.slm_gold_path)
        metrics, failures = slm.evaluate(self.slm, gold, self._redact)
        ev = SlmEvaluation(evaluation_id=new_id("SLE"), tenant_id=p.tenant_id, provider=self.slm.name,
                           model=self.slm.model, prompt_revision=slm.PROMPT_REVISION,
                           gold_set=str(self.settings.slm_gold_path).rsplit("/", 1)[-1], gold_set_sha256=gold_sha,
                           n_examples=len(gold), metrics=metrics, thresholds=slm.THRESHOLDS, failures=failures[:50],
                           passed=slm.passes(metrics), evaluated_by=p.principal_id, evaluated_at=self.clock())
        with self.db.session() as s:
            N1Store(s).add_slm_evaluation(ev)
            Repository(s).append_audit(tenant_id=p.tenant_id, event_type="N1_SLM_EVALUATION", actor_id=p.principal_id,
                                       actor_roles=p.roles, subject=f"slm:{ev.provider}/{ev.model}",
                                       details={"passed": ev.passed, "metrics": metrics, "gold_set_sha256": gold_sha})
        return ev

    def slm_extract(self, p: Principal, req: SlmExtractionRequest) -> SlmExtractionResult:
        if p.tenant_id != req.tenant_id:
            raise N0Error(403, "CROSS_TENANT_REFERENCE", "cross-tenant access is not permitted")
        with self.db.session() as s:
            source = Repository(s).get_source(req.tenant_id, req.source_id)
        try:
            slm.check_boundary(self.slm, req.tenant_id, source)
        except LLMDisabledError as exc:
            raise N0Error(503, "SLM_DISABLED", str(exc)) from exc
        except LLMBoundaryError as exc:
            raise N0Error(403, exc.code, str(exc)) from exc
        _, gold_sha = slm.load_gold(self.settings.slm_gold_path)
        with self.db.session() as s:
            qual = N1Store(s).passing_evaluation(p.tenant_id, self.slm.name, self.slm.model, slm.PROMPT_REVISION, gold_sha)
        if qual is None:
            raise N0Error(409, "SLM_NOT_QUALIFIED", f"{self.slm.name}/{self.slm.model} has no passing evaluation on the "
                                                    "held-out gold set; run POST /v1/n1/slm/evaluate first")
        text = self._redact(req.text)
        input_hash = sha256_hex(f"{slm.PROMPT_REVISION}\n{text}".encode())
        started = self.clock()
        try:
            result = slm.run_model(self.slm, text)
        except (LLMOutputInvalid, LLMRefusalError) as exc:
            self._record_run(p, req.source_id, input_hash, None, started, "REFUSED" if isinstance(exc, LLMRefusalError)
                             else "FAILED", {"error": str(exc)})
            raise N0Error(422, exc.code, str(exc)) from exc
        except LLMError as exc:
            raise N0Error(502, exc.code, str(exc)) from exc
        run = self._record_run(p, req.source_id, input_hash, result, started, "SUCCEEDED", result.output)
        grounded = slm.ground(result.output, text)
        obs = slm.build_observation(req, grounded, text, run.run_id)
        return SlmExtractionResult(observation=obs, model_run=run, evaluation_id=qual.evaluation_id,
                                   missing_fields=sorted(set(grounded.missing)), warnings=grounded.warnings)

    def _record_run(self, p: Principal, source_id: str, input_hash: str, result, started, status: str,
                    output: dict, prompt_revision: str = slm.PROMPT_REVISION) -> ModelRun:
        usage = result.usage if result else TokenUsage(input_tokens=0, output_tokens=0)
        name = result.model_name if result else (self.slm.model if self.slm else "none")
        version = result.model_version if result else "n/a"
        run = ModelRun(run_id=new_id("MR"), tenant_id=p.tenant_id, model_name=name, model_version=version,
                       provider=result.provider if result else (self.slm.name if self.slm else "none"),
                       prompt_revision=prompt_revision, input_hash=input_hash, output_payload=output,
                       token_usage=usage, estimated_cost_usd=estimate_cost_usd(version, usage) or
                       estimate_cost_usd(name, usage), status=status, source_id=source_id,
                       started_at=started, completed_at=self.clock())
        with self.db.session() as s:
            repo = Repository(s)
            repo.add_model_run(run)
            repo.append_audit(tenant_id=p.tenant_id, event_type="N1_MODEL_RUN", actor_id=p.principal_id,
                              actor_roles=p.roles, subject=f"model_run:{run.run_id}",
                              details={"status": status, "prompt_revision": prompt_revision},
                              model_runs=[{"run_id": run.run_id, "model_name": run.model_name,
                                           "model_version": run.model_version, "provider": run.provider,
                                           "prompt_revision": prompt_revision,
                                           "estimated_cost_usd": run.estimated_cost_usd}])
        return run

    # ==========================================================================================
    # explainer (opt-in)
    # ==========================================================================================
    def explain(self, p: Principal, report_id: str) -> ReportExplanation:
        if self.explainer is None:
            raise N0Error(503, "EXPLAINER_DISABLED", "N1_EXPLAINER_PROVIDER is disabled")
        report = self.get_report(p, report_id)
        if self.explainer.external:
            with self.db.session() as s:
                repo, st = Repository(s), N1Store(s)
                batches = st.batches(p.tenant_id)
                for c in report.citations:
                    aid, rev = c.n0_artifact.rsplit("@", 1)
                    b = batches.get((aid, rev))
                    src = repo.get_source(p.tenant_id, b.source_id) if b else None
                    if src is None or "LLM_EXTRACTION" not in src.permitted_use:
                        raise N0Error(403, "LLM_TRANSMISSION_UNAPPROVED", f"{c.evidence_id}: its source does not "
                                                                          "permit LLM_EXTRACTION")
        started = self.clock()
        try:
            out, result = explainer.explain(self.explainer, report)
        except explainer.ExplainerRejected as exc:
            raise N0Error(422, exc.code, str(exc)) from exc
        except LLMOutputInvalid as exc:
            raise N0Error(422, exc.code, str(exc)) from exc
        except LLMError as exc:
            raise N0Error(502, exc.code, str(exc)) from exc
        run = self._record_run(p, "n1-report", sha256_hex(canonical_json(explainer.facts(report))), result, started,
                               "SUCCEEDED", result.output, explainer.PROMPT_REVISION)
        return out.model_copy(update={"model_run_id": run.run_id})

    # ==========================================================================================
    # N1 → N2 / N6 / N7 handoff through N0
    # ==========================================================================================
    def handoff(self, approver: Principal, report_id: str, req: HandoffRequest) -> HandoffResult:
        now = self.clock()
        with self.db.session() as s:
            repo, st = Repository(s), N1Store(s)
            row = st.report_row(approver.tenant_id, report_id)
            if row is None:
                raise N0Error(404, "REPORT_NOT_FOUND", f"{report_id} not found in this tenant")
            report = MarketReport.model_validate(row.record)
            latest = st.latest_report_row(approver.tenant_id, report.scope_key)
            if latest is not None and latest.report_id != report_id:
                raise N0Error(409, "REPORT_SUPERSEDED", f"{report_id} is superseded by {latest.report_id}")
            if engine.report_digest(report) != report.report_sha256 or row.report_sha256 != report.report_sha256:
                raise N0Error(409, "REPORT_INTEGRITY", "stored report no longer matches its SHA-256")
            if report.generated_by == approver.principal_id:
                raise N0Error(403, "FOUR_EYES", "the author of a report cannot approve its handoff")
            if not any(not f.synthetic for f in report.evidence_features):
                raise N0Error(409, "SYNTHETIC_ONLY_REPORT", "a report built only from synthetic data is never handed "
                                                            "off; hypothetical spend stays inside N1")
            current = self._current_evidence(repo, st, approver.tenant_id, report, now)
            obs = {r.evidence_id: MarketObservation.model_validate(r.record)
                   for r in st.latest_observations(approver.tenant_id)}
            accounts = st.latest_accounts(approver.tenant_id)
            optimization = st.get_optimization(approver.tenant_id, req.optimization_id) if req.optimization_id else None
            if req.optimization_id and optimization is None:
                raise N0Error(404, "OPTIMIZATION_NOT_FOUND", f"{req.optimization_id} not found in this tenant")
            released_before = {pk.artifact_id: (h.handoff_id, pk) for h in st.handoffs_for(approver.tenant_id, report_id)
                               for pk in h.packages if pk.status == "RELEASED"}
        svc = Principal(principal_id=f"svc-n1@{approver.tenant_id}", tenant_id=approver.tenant_id,
                        roles=["node_service"], assigned_roles=["service"], nodes=["N1"],
                        clearance=approver.clearance, auth_method="internal")
        packages: list[HandoffPackage] = []
        for target in dict.fromkeys(req.targets):
            builds = {"N2": self._n2_packages, "N6": self._n6_package, "N7": self._n7_package}[target](
                report, req, current, obs, accounts, optimization)
            for art, excluded, refusal in builds:
                if refusal:
                    packages.append(HandoffPackage(target=target, artifact_type=art or "-", artifact_id="-",
                                                   revision="-", status="REFUSED", reason=refusal,
                                                   excluded_evidence=excluded))
                    continue
                if art.artifact_id in released_before:
                    hid, pk = released_before[art.artifact_id]
                    packages.append(pk.model_copy(update={"reason": f"already released in {hid}"}))
                    continue
                packages.append(self._emit(svc, approver, art, target, report_id, excluded))
        result = HandoffResult(handoff_id=new_id("HND"), tenant_id=approver.tenant_id, report_id=report_id,
                               report_sha256=report.report_sha256, requested_by=approver.principal_id, created_at=now,
                               packages=packages)
        with self.db.session() as s:
            repo = Repository(s)
            N1Store(s).add_handoff(result)
            repo.append_audit(tenant_id=approver.tenant_id, event_type="N1_HANDOFF", actor_id=approver.principal_id,
                              actor_roles=approver.roles, subject=f"n1_report:{report_id}",
                              details={"handoff_id": result.handoff_id,
                                       "packages": [{"target": pk.target, "artifact_id": pk.artifact_id,
                                                     "status": pk.status, "n0_decision": pk.n0_decision}
                                                    for pk in packages]})
        return result

    def _current_evidence(self, repo: Repository, st: N1Store, tenant: str, report: MarketReport,
                          now: datetime) -> dict[str, tuple[Optional[str], set[str]]]:
        """evidence_id → (reason it is no longer usable or None, purposes its source permits)."""
        batches = st.batches(tenant)
        out = {}
        for f in report.evidence_features:
            latest = st.latest_observation(tenant, f.evidence_id)
            aid, rev = f.n0_artifact.rsplit("@", 1)
            b = batches.get((aid, rev))
            src = repo.get_source(tenant, b.source_id) if b else None
            reason = None
            if latest is None or latest.evidence_sha256 != f.evidence_sha256:
                reason = "evidence changed after the report; regenerate the report"
            else:
                d = repo.latest_decision(tenant, aid, rev)
                top = repo.latest_revision(tenant, aid)
                if d is None or d.decision != "PASS":
                    reason = f"N0 decision for {f.n0_artifact} is no longer PASS"
                elif top is not None and top.revision_order > revision_order(rev):
                    reason = f"{f.n0_artifact} is superseded at N0"
                elif evaluate_rights("MARKET_INTELLIGENCE", b.source_id, src, now, Findings()).status != "GRANTED":
                    reason = f"rights for {b.source_id} are no longer granted"
            out[f.evidence_id] = (reason, set(src.permitted_use) if src else set())
        return out

    def _usable(self, current, eid: str, purpose: str) -> Optional[str]:
        reason, purposes = current.get(eid, ("not in report", set()))
        if reason:
            return reason
        if purpose not in purposes:
            return f"source rights do not permit {purpose}"
        return None

    def _artifact(self, report: MarketReport, req: HandoffRequest, target: str, kind: str, suffix: str,
                  payload: dict, upstream: list[str]) -> Artifact:
        payload = json.loads(json.dumps(payload, default=str))
        return Artifact(tenant_id=report.tenant_id, project_id=req.project_id,
                        artifact_id=f"N1-{report.report_id}-{target}{('-' + suffix) if suffix else ''}"[:128],
                        artifact_type=kind, source_id=req.derived_source_id, processing_purpose=PACKAGE_PURPOSE[target],
                        release_stage="CONCEPT", revision="A", synthetic=False,
                        source_sha256=sha256_hex(canonical_json(payload)), payload=payload,
                        source_artifact_ids=sorted(set(upstream)))

    def _n2_packages(self, report, req, current, obs, accounts, optimization):
        out = []
        wanted = set(req.opportunity_ids) if req.opportunity_ids else None
        for o in report.opportunities:
            if wanted is not None and o.opportunity_id not in wanted:
                continue
            if o.synthetic_only:
                continue
            if not o.rfq_seed_evidence_id:
                if wanted is not None:
                    out.append(("RFQ", [], f"{o.opportunity_id}: not seed-ready ({'; '.join(o.rfq_seed_blockers)})"))
                continue
            eid = o.rfq_seed_evidence_id
            why = self._usable(current, eid, "RFQ_ANALYSIS")
            if why:
                out.append(("RFQ", [eid], f"{o.opportunity_id}: seed evidence {eid} unusable — {why}"))
                continue
            ev, feat = obs[eid], next(f for f in report.evidence_features if f.evidence_id == eid)
            slm_made = ev.extraction_method == "SLM"
            reqs = []
            for r in ev.requirements:
                d = r.model_dump(mode="json", exclude={"si_value", "si_unit"})
                d["value_origin"] = "MODEL_EXTRACTED" if slm_made else r.value_origin
                if not d.get("evidence_snippets"):
                    d["evidence_snippets"] = [{"source_ref": eid, "text": f"{r.parameter} {r.value} {r.unit or ''}".strip()}]
                reqs.append(d)
            payload = {"rfq_id": f"N1-{o.account_id}", "customer_name": o.legal_name,
                       "product_category": feat.service_category, "quantity": ev.quantity, "requirements": reqs,
                       "compliance_standards": ev.compliance_standards,
                       "notes": (f"Opportunity seed generated by N1 from attested evidence {eid} "
                                 f"({ev.source_url or feat.n0_artifact}); report {report.report_id}. The heuristic "
                                 "priority score is not a win probability.")}
            if slm_made and ev.extraction_model_run_id:
                payload["extraction_model_run_id"] = ev.extraction_model_run_id
            out.append((self._artifact(report, req, "N2", "RFQ", o.account_id, payload, [feat.n0_artifact]), [], None))
        if not out:
            out.append(("RFQ", [], "no attested, real opportunity with requirement clauses and a quantity"))
        return out

    def _n6_package(self, report, req, current, obs, accounts, optimization):
        entries, excluded, upstream = [], [], []
        feats = {f.evidence_id: f for f in report.evidence_features}
        for o in report.opportunities:
            if o.synthetic_only:
                continue
            ev_ok = []
            for eid in o.evidence_ids:
                if feats[eid].synthetic:
                    continue
                if self._usable(current, eid, "COMMERCIAL"):
                    excluded.append(eid)
                else:
                    ev_ok.append(eid)
                    upstream.append(feats[eid].n0_artifact)
            if not ev_ok:
                continue
            entries.append({"rank": len(entries) + 1, "account_id": o.account_id, "legal_name": o.legal_name,
                            "country": o.country, "heuristic_priority_score": o.heuristic_priority_score,
                            "tender_deadlines": [d.model_dump(mode="json") for d in o.tender_deadlines
                                                 if d.evidence_id in ev_ok],
                            "eligibility_risk_flags": o.risk_flags, "evidence_ids": ev_ok})
        if not entries:
            return [("COMMERCIAL_SHORTLIST", excluded, "no qualified real buyers with usable evidence")]
        payload = {"report_id": report.report_id, "report_sha256": report.report_sha256,
                   "generated_at": report.generated_at.isoformat(), "entries": entries,
                   "disclaimer": report.disclaimers[1]}
        return [(self._artifact(report, req, "N6", "COMMERCIAL_SHORTLIST", "", payload, upstream), excluded, None)]

    def _n7_package(self, report, req, current, obs, accounts, optimization):
        items, excluded, upstream = [], [], []
        ind = hyp = Decimal(0)
        for f in report.evidence_features:
            if f.synthetic or f.fx is None:
                continue
            if self._usable(current, f.evidence_id, "FINANCE"):
                if f.counts_as in ("VERIFIED", "INDICATIVE", "HYPOTHETICAL"):
                    excluded.append(f.evidence_id)
                continue
            if f.counts_as == "VERIFIED":
                items.append({"evidence_id": f.evidence_id, "account_id": f.account_id or "UNRESOLVED",
                              "spend_type": f.spend_type,
                              "synthetic": False, "attestation_id": f.attestation_id,
                              "original": f.fx.original.model_dump(mode="json"), "fx_rate": str(f.fx.rate),
                              "fx_as_of": f.fx.fx_as_of.isoformat(), "amount_base": str(f.fx.amount_base),
                              "source_url": f.source_url})
                upstream.append(f.n0_artifact)
            elif f.counts_as == "INDICATIVE":
                ind += f.fx.amount_base
                upstream.append(f.n0_artifact)
            elif f.counts_as == "HYPOTHETICAL":
                hyp += f.fx.amount_base
                upstream.append(f.n0_artifact)
        if not upstream:
            return [("SAM_EVIDENCE", excluded, "no real evidence with usable rights for FINANCE")]
        total = engine.money(sum((Decimal(i["amount_base"]) for i in items), Decimal(0)))
        caveats = [report.sam.disclaimer,
                   f"FX table: {', '.join(f'{r.currency}@{r.rate_to_base} ({r.as_of}, {r.source})' for r in report.fx_table) or 'base currency only'}",
                   f"{len(report.pending_attestations)} verified-budget item(s) await attestation and are excluded",
                   f"{len(report.exclusions)} evidence item(s) excluded by N1.4 guardrails"]
        if total != report.sam.verified_real_sam:
            caveats.append(f"verified total re-checked at handoff: {total} (report: {report.sam.verified_real_sam}); "
                           f"{len(excluded)} item(s) dropped for rights or currency of evidence")
        actions = []
        if optimization is not None:
            actions = [{"action_id": a.action_id, "action_type": a.action_type,
                        "target_account_id": a.target_account_id, "execution_cost": str(a.execution_cost),
                        "net_value": str(a.net_value)} for a in optimization.ranked if a.recommended]
        payload = {"report_id": report.report_id, "report_sha256": report.report_sha256,
                   "as_of": report.as_of.date().isoformat(), "base_currency": report.base_currency,
                   "verified_real_sam": str(total), "indicative_spend": str(engine.money(ind)),
                   "hypothetical_addressable_spend": str(engine.money(hyp)), "verified_items": items,
                   "caveats": caveats, "costed_actions": actions}
        return [(self._artifact(report, req, "N7", "SAM_EVIDENCE", "", payload, upstream), excluded, None)]

    def _emit(self, svc: Principal, approver: Principal, art: Artifact, target: str, report_id: str,
              excluded: list[str]) -> HandoffPackage:
        res = self.pipeline.evaluate(svc, EvaluateRequest(artifact=art, origin_node="N1", requested_destinations=[target]))
        d = res.decision
        base = dict(target=target, artifact_type=art.artifact_type, artifact_id=art.artifact_id, revision=d.revision,
                    n0_decision=d.decision, n0_decision_id=d.decision_id,
                    violated_codes=sorted({v.code for v in d.violated_rules}), excluded_evidence=excluded)
        if d.decision != "PASS":
            hint = " — resolve the REVIEW through /v1/approvals, then hand off again" if d.decision == "REVIEW" else ""
            return HandoffPackage(**base, status="REFUSED", reason=f"N0 decision {d.decision}{hint}: {d.explanation}"[:2000])
        try:
            with self.db.session() as s:
                repo = Repository(s)
                now = self.clock()
                ap = record_approval(repo, approver, ApprovalRequest(
                    artifact_id=art.artifact_id, exact_revision=d.revision, scope="RELEASE", decision="APPROVED",
                    reason=f"N1 handoff of report {report_id} to {target}"), now, self.store)
                m = release_handoff(repo, approver, HandoffReleaseRequest(
                    artifact_id=art.artifact_id, revision=d.revision, source_node="N1", target_nodes=[target],
                    approval_id=ap.approval_event.approval_id), now)
                repo.add_edge(art.tenant_id, f"n1_report:{report_id}", f"artifact:{art.artifact_id}@{d.revision}",
                              "HANDOFF_PACKAGE", target_node=target)
        except N0Error as err:
            return HandoffPackage(**base, status="REFUSED", reason=f"N0 refused the release: {err.code}: {err.message}")
        return HandoffPackage(**base, status="RELEASED", manifest_id=m.manifest_id, approval_id=m.approval_id,
                              reason=f"released to {target} under manifest {m.manifest_id}")


__all__ = ["N1Service", "make_uri"]
