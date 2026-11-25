"""N4 service: registries, design intake, planning, PFMEA / CAPA, assessment, review and release.

Evidence intake (the four adapters) lives in ``intake.py`` (``EvidenceIntakeMixin``). Both share the
N0 database, object store, scanner, PII redaction and approval chain.

N4.1 lock_intake     the N3 DESIGN_RELEASEs released to N4 for every PRD scope, verified against N0
                     (decision, manifest, stored payload hash, approval chain) plus the N2 PRD and its
                     hazards; invalidated when the PRD or a design revision changes.
N4.2 create_plan     requirements-to-test plan; traceability with 100 % safety-critical coverage.
N4.3 PFMEA / control plan / CAPA, human-rated only; the custom attention heuristic unless a
     licensed AIAG-VDA AP table is configured.
N4.5 assess          deterministic verification (engine.py) over the re-hashed evidence.
N4.6 review          a separate, qualified test_authority / quality_lead approves the exact evidence
                     hash and design revision.
N4.7 release         N0-checked VERIFICATION_RELEASE packages to N8 / N6 / N7.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional

from sqlalchemy import select

from ..db import ApprovalRow
from ..errors import N0Error
from ..governance import record_approval, release_handoff
from ..models import ApprovalRequest, Artifact, EvaluateRequest, HandoffReleaseRequest, TraceEntry
from ..n2.service import N2ReleaseRow
from ..n3.db import N3EventRow, N3ReleaseRow
from ..n3.models import N3Release
from ..repository import Repository, chain_hash, dump, new_id
from ..rules import Findings as N0Findings
from ..rules import canonical_json, evaluate_rights, revision_order, sha256_hex
from ..security import Principal
from . import APPROVAL_SCOPE, HEURISTIC_LABEL, ai, engine, reliability
from .config import N4Settings
from .db import (N4AdjudicationRow, N4AssessmentRow, N4CalibrationRow, N4CapaRow, N4ControlPlanRow, N4CorpusRow,
                 N4EventRow, N4EvidenceRow, N4InstrumentRow, N4IntakeRow, N4LabRow, N4MeasurementRow, N4MethodRow,
                 N4PfmeaRow, N4PlanRow, N4QualificationRow, N4ReleaseRow, N4ReliabilityRow, N4ReviewRow,
                 N4SuggestionRow)
from .intake import EvidenceIntakeMixin
from .models import (CalibrationRequest, CapaRequest, CapaVerifyRequest, ControlPlanRequest, CorpusDocumentRequest,
                     DesignRef, IntakeRequest, N4AcceptedLab, N4Adjudication, N4CalibrationRecord, N4Capa,
                     N4ControlPlan, N4DesignIntake, N4EvidenceIntake, N4Instrument, N4Measurement, N4Pfmea,
                     N4Qualification, N4ReleasePackage, N4ReleaseRequest, N4ReliabilityIllustration, N4Review,
                     N4ReviewRequest, N4Suggestion, N4TestMethod, N4TestPlan, N4TraceabilityMatrix,
                     N4VerificationAssessment, N4VerificationRelease, PfmeaRequest, ReliabilityRequest,
                     SuggestionRequest, TestMethodRequest, TestPlanRequest, TraceRequirement)

PURPOSE = {"N8": "LIFECYCLE", "N6": "COMMERCIAL", "N7": "FINANCE"}


def _with_hash(model, field: str):
    body = {k: v for k, v in dump(model).items() if k != field}
    return model.model_copy(update={field: sha256_hex(canonical_json(body))})


class N4Service(EvidenceIntakeMixin):
    def __init__(self, state, settings: N4Settings, provider=None) -> None:
        self.db, self.store, self.pipeline, self.clock = state.db, state.store, state.pipeline, state.clock
        self.scanner, self.docling, self.presidio = state.scanner, state.docling, state.presidio
        self.n0_settings, self.n2, self.n3 = state.settings, state.n2, state.n3
        self.settings = settings
        self.ap = engine.ApTable.load(settings.ap_table_path, settings.ap_license_ref)
        self.provider = ai.make_provider(settings, provider)
        self.n2.release_listeners.append(self.on_prd_release)
        self.n3.release_listeners.append(self.on_design_release)

    # ==========================================================================================
    # helpers
    # ==========================================================================================
    @staticmethod
    def _svc(p: Principal) -> Principal:
        return Principal(principal_id=f"svc-n4@{p.tenant_id}", tenant_id=p.tenant_id, roles=["node_service"],
                         assigned_roles=["service"], nodes=["N4"], clearance=p.clearance, auth_method="internal")

    @staticmethod
    def _audit(s, p: Principal, event_type: str, subject: str, details: dict) -> None:
        Repository(s).append_audit(tenant_id=p.tenant_id, event_type=event_type, actor_id=p.principal_id,
                                   actor_roles=p.roles, subject=subject, details=details)

    def _event(self, s, tenant: str, kind: str, subject_id: str, status: str, actor: str, **details: Any) -> None:
        s.add(N4EventRow(event_id=new_id("N4E"), tenant_id=tenant, subject_kind=kind, subject_id=subject_id,
                         status=status, record={"status": status, "actor": actor, "at": self.clock().isoformat(),
                                                **details}))
        s.flush()

    @staticmethod
    def _events(s, tenant: str, kind: str, subject_id: str) -> list[dict]:
        rows = s.scalars(select(N4EventRow).where(N4EventRow.tenant_id == tenant, N4EventRow.subject_kind == kind,
                                                  N4EventRow.subject_id == subject_id)
                         .order_by(N4EventRow.created_at, N4EventRow.event_id))
        return [r.record for r in rows]

    def _invalidated(self, s, tenant: str, kind: str, subject_id: str) -> Optional[dict]:
        return next((e for e in self._events(s, tenant, kind, subject_id) if e["status"] == "INVALIDATED"), None)

    @staticmethod
    def _get(s, row_cls, model, tenant: str, key, code: str):
        row = s.get(row_cls, key)
        if row is None or row.tenant_id != tenant:
            raise N0Error(404, code, f"{key} not found in this tenant")
        return model.model_validate(row.record)

    @staticmethod
    def _latest(s, row_cls, model, tenant: str, **match) -> dict[Any, Any]:
        """Latest-seq record per key for the versioned registry tables."""
        q = select(row_cls).where(row_cls.tenant_id == tenant)
        for k, v in match.items():
            q = q.where(getattr(row_cls, k) == v)
        out: dict[Any, tuple[int, Any]] = {}
        for r in s.scalars(q):
            key = tuple(getattr(r, c.name) for c in row_cls.__table__.primary_key.columns
                        if c.name not in ("seq", "tenant_id"))
            key = key[0] if len(key) == 1 else key
            seq = getattr(r, "seq", 0)
            if key not in out or seq > out[key][0]:
                out[key] = (seq, model.model_validate(r.record))
        return {k: v[1] for k, v in out.items()}

    def _registry(self, s, tenant: str) -> engine.Registry:
        return engine.Registry(methods=self._latest(s, N4MethodRow, N4TestMethod, tenant),
                               instruments=self._latest(s, N4InstrumentRow, N4Instrument, tenant),
                               calibrations=self._latest(s, N4CalibrationRow, N4CalibrationRecord, tenant))

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

    def _verify_blobs(self, tenant: str, intakes: list[N4EvidenceIntake]) -> dict[str, str]:
        """Re-hash every stored evidence file; returns evidence_id → problem for tampered evidence."""
        bad: dict[str, str] = {}
        with self.db.session() as s:
            repo = Repository(s)
            for e in intakes:
                for f in e.files:
                    blob = repo.find_blob(tenant, f.sha256, "UPLOAD")
                    try:
                        data = self.store.get(blob.bucket, blob.object_key) if blob else None
                    except Exception:      # noqa: BLE001 - a missing object is a tampering signal
                        data = None
                    if data is None or sha256_hex(data) != f.sha256:
                        bad[e.evidence_id] = f"stored bytes of {f.path} no longer match SHA-256 {f.sha256[:12]}…"
                        break
        return bad

    # ==========================================================================================
    # registries
    # ==========================================================================================
    def register_method(self, p: Principal, req: TestMethodRequest, document: Optional[tuple[str, bytes]] = None
                        ) -> N4TestMethod:
        with self.db.session() as s:
            if self._latest(s, N4MethodRow, N4TestMethod, p.tenant_id).get((req.method_id, req.revision)):
                raise N0Error(409, "METHOD_EXISTS", f"{req.method_id} rev {req.revision} exists; issue a new revision")
            sha = uri = None
            if document is not None:
                name, data = document
                from ..scanning import validate_upload
                screen = validate_upload(data, name, None, self.n0_settings.max_upload_bytes)
                if not screen.ok or not self.scanner.scan(data).clean:
                    raise N0Error(422, "UPLOAD_REJECTED", f"SOP document rejected: {'; '.join(screen.problems)}")
                blob = self._store_bytes(Repository(s), p.tenant_id, "n4-registry", name, data, screen.sniffed_mime,
                                         p.principal_id, {"engine": self.scanner.name, "clean": True})
                sha, uri = blob.sha256, f"n0://quarantine/{blob.object_key}"
            m = N4TestMethod(**req.model_dump(), tenant_id=p.tenant_id, status="DRAFT", authored_by=p.principal_id,
                             document_sha256=sha, document_uri=uri)
            s.add(N4MethodRow(method_id=m.method_id, revision=m.revision, seq=1, tenant_id=p.tenant_id,
                              status="DRAFT", record=dump(m)))
            self._audit(s, p, "N4_METHOD_DRAFTED", f"n4_method:{m.method_id}@{m.revision}", {"document_sha256": sha})
        return m

    def approve_method(self, p: Principal, method_id: str, revision: str) -> N4TestMethod:
        with self.db.session() as s:
            rows = list(s.scalars(select(N4MethodRow).where(N4MethodRow.tenant_id == p.tenant_id,
                                                            N4MethodRow.method_id == method_id,
                                                            N4MethodRow.revision == revision)))
            if not rows:
                raise N0Error(404, "METHOD_NOT_FOUND", f"{method_id} rev {revision} not found")
            cur = N4TestMethod.model_validate(max(rows, key=lambda r: r.seq).record)
            if cur.status != "DRAFT":
                raise N0Error(409, "INVALID_TRANSITION", f"{method_id} rev {revision} is {cur.status}")
            if cur.authored_by == p.principal_id:
                raise N0Error(403, "SEPARATION_OF_DUTIES", "the author of an SOP cannot approve it")
            new = cur.model_copy(update={"status": "APPROVED", "approved_by": p.principal_id,
                                         "approved_at": self.clock()})
            s.add(N4MethodRow(method_id=method_id, revision=revision, seq=len(rows) + 1, tenant_id=p.tenant_id,
                              status="APPROVED", record=dump(new)))
            self._audit(s, p, "N4_METHOD_APPROVED", f"n4_method:{method_id}@{revision}", {})
        return new

    def register_instrument(self, p: Principal, inst: N4Instrument) -> N4Instrument:
        if inst.tenant_id != p.tenant_id:
            raise N0Error(403, "CROSS_TENANT_REFERENCE", "cross-tenant access is not permitted")
        with self.db.session() as s:
            if s.get(N4InstrumentRow, (inst.instrument_id, p.tenant_id)) is not None:
                raise N0Error(409, "INSTRUMENT_EXISTS", f"{inst.instrument_id} is already registered")
            if any(i.serial == inst.serial and i.manufacturer == inst.manufacturer
                   for i in self._latest(s, N4InstrumentRow, N4Instrument, p.tenant_id).values()):
                raise N0Error(409, "INSTRUMENT_EXISTS", f"serial {inst.serial} is already registered")
            rec = inst.model_copy(update={"registered_by": p.principal_id, "registered_at": self.clock()})
            s.add(N4InstrumentRow(instrument_id=inst.instrument_id, tenant_id=p.tenant_id, serial=inst.serial,
                                  record=dump(rec)))
            self._audit(s, p, "N4_INSTRUMENT_REGISTERED", f"n4_instrument:{inst.instrument_id}",
                        {"serial": inst.serial, "model": inst.model})
        return rec

    def record_calibration(self, p: Principal, req: CalibrationRequest,
                           certificate: Optional[tuple[str, bytes]] = None) -> N4CalibrationRecord:
        with self.db.session() as s:
            inst = s.get(N4InstrumentRow, (req.instrument_id, p.tenant_id))
            if inst is None:
                raise N0Error(404, "INSTRUMENT_NOT_FOUND", f"{req.instrument_id} is not registered")
            if self._latest(s, N4CalibrationRow, N4CalibrationRecord, p.tenant_id).get(req.calibration_id):
                raise N0Error(409, "CALIBRATION_EXISTS", f"{req.calibration_id} exists")
            sha = uri = None
            if certificate is not None:
                name, data = certificate
                from ..scanning import validate_upload
                screen = validate_upload(data, name, None, self.n0_settings.max_upload_bytes)
                if not screen.ok or not self.scanner.scan(data).clean:
                    raise N0Error(422, "UPLOAD_REJECTED", f"certificate rejected: {'; '.join(screen.problems)}")
                blob = self._store_bytes(Repository(s), p.tenant_id, "n4-registry", name, data, screen.sniffed_mime,
                                         p.principal_id, {"engine": self.scanner.name, "clean": True})
                sha, uri = blob.sha256, f"n0://quarantine/{blob.object_key}"
            rec = N4CalibrationRecord(**req.model_dump(), tenant_id=p.tenant_id, instrument_serial=inst.serial,
                                      certificate_sha256=sha, certificate_uri=uri, status="VALID",
                                      recorded_by=p.principal_id, recorded_at=self.clock())
            s.add(N4CalibrationRow(calibration_id=rec.calibration_id, seq=1, tenant_id=p.tenant_id,
                                   instrument_id=rec.instrument_id, status="VALID", record=dump(rec)))
            self._audit(s, p, "N4_CALIBRATION_RECORDED", f"n4_calibration:{rec.calibration_id}",
                        {"instrument_id": rec.instrument_id, "due": rec.calibration_due.isoformat(),
                         "certificate_sha256": sha})
        return rec

    def revoke_calibration(self, p: Principal, calibration_id: str, reason: str) -> N4CalibrationRecord:
        with self.db.session() as s:
            rows = list(s.scalars(select(N4CalibrationRow).where(N4CalibrationRow.tenant_id == p.tenant_id,
                                                                 N4CalibrationRow.calibration_id == calibration_id)))
            if not rows:
                raise N0Error(404, "CALIBRATION_NOT_FOUND", f"{calibration_id} not found")
            cur = N4CalibrationRecord.model_validate(max(rows, key=lambda r: r.seq).record)
            new = cur.model_copy(update={"status": "REVOKED", "uncertainty_statement":
                                         f"REVOKED by {p.principal_id}: {reason}"})
            s.add(N4CalibrationRow(calibration_id=calibration_id, seq=len(rows) + 1, tenant_id=p.tenant_id,
                                   instrument_id=cur.instrument_id, status="REVOKED", record=dump(new)))
            self._audit(s, p, "N4_CALIBRATION_REVOKED", f"n4_calibration:{calibration_id}", {"reason": reason})
        return new

    def accept_lab(self, p: Principal, lab: N4AcceptedLab) -> N4AcceptedLab:
        with self.db.session() as s:
            n = len(list(s.scalars(select(N4LabRow).where(N4LabRow.tenant_id == p.tenant_id,
                                                          N4LabRow.lab_id == lab.lab_id))))
            rec = lab.model_copy(update={"tenant_id": p.tenant_id, "approved_by": p.principal_id})
            s.add(N4LabRow(lab_id=lab.lab_id, seq=n + 1, tenant_id=p.tenant_id, record=dump(rec)))
            self._audit(s, p, "N4_LAB_ACCEPTED", f"n4_lab:{lab.lab_id}",
                        {"accreditation": lab.accreditation_number, "valid_until": lab.valid_until.isoformat()})
        return rec

    def grant_qualification(self, p: Principal, q: N4Qualification) -> N4Qualification:
        if q.principal_id == p.principal_id:
            raise N0Error(403, "SEPARATION_OF_DUTIES", "a quality lead cannot qualify themselves")
        with self.db.session() as s:
            n = len(list(s.scalars(select(N4QualificationRow).where(N4QualificationRow.tenant_id == p.tenant_id,
                                                                    N4QualificationRow.principal_id == q.principal_id))))
            rec = q.model_copy(update={"tenant_id": p.tenant_id, "granted_by": p.principal_id,
                                       "granted_at": self.clock()})
            s.add(N4QualificationRow(principal_id=q.principal_id, seq=n + 1, tenant_id=p.tenant_id, record=dump(rec)))
            self._audit(s, p, "N4_QUALIFICATION_GRANTED", f"principal:{q.principal_id}",
                        {"methods": q.methods, "valid_until": q.valid_until.isoformat()})
        return rec

    def _labs(self, s, tenant: str) -> list[N4AcceptedLab]:
        return list(self._latest(s, N4LabRow, N4AcceptedLab, tenant).values())

    # ==========================================================================================
    # N4.1 intake
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

    def lock_intake(self, p: Principal, req: IntakeRequest) -> N4DesignIntake:
        t = p.tenant_id
        rels = self.n2.releases(t, req.prd_id)
        if not rels:
            raise N0Error(409, "PRD_NOT_RELEASED", f"{req.prd_id} has no N2 PRDRelease")
        rel = max(rels, key=lambda r: revision_order(r.prd_revision))
        with self.db.session() as s:
            repo = Repository(s)
            prd = repo.get_artifact(t, rel.n0_artifact_id, rel.prd_revision)
            d = repo.latest_decision(t, prd.artifact_id, prd.revision) if prd else None
            if prd is None or d is None or d.decision != "PASS":
                raise N0Error(409, "PRD_NOT_PASS", f"{rel.n0_artifact_id}@{rel.prd_revision} is not an N0 PASS PRD")
            canon = prd.canonical["payload"]
            prd_ref = f"{prd.artifact_id}@{prd.revision}"
            refs, corners, problems = [], [], []
            for scope in canon["design_scopes"]:
                aid = f"DSN-{req.prd_id}-{scope}"
                row = repo.latest_revision(t, aid)
                if row is None:
                    raise N0Error(409, "DESIGN_NOT_RELEASED_TO_N4", f"no N3 design release {aid} for scope {scope}")
                dd = repo.latest_decision(t, aid, row.revision)
                manifest = next((m for m in reversed(repo.releases(t, aid, row.revision))
                                 if m.status == "RELEASED" and "N4" in m.target_nodes), None)
                if dd is None or dd.decision != "PASS" or manifest is None:
                    raise N0Error(409, "DESIGN_NOT_RELEASED_TO_N4", f"{aid}@{row.revision} was not released to N4")
                if prd_ref not in row.canonical.get("source_artifact_ids", []):
                    raise N0Error(409, "DESIGN_PRD_MISMATCH", f"{aid}@{row.revision} was designed against "
                                                              f"{row.canonical.get('source_artifact_ids')}, not "
                                                              f"{prd_ref}")
                problems += self._verify_manifest(repo, row, manifest)
                rights = evaluate_rights("VERIFICATION", row.source_id, repo.get_source(t, row.source_id), self.clock(),
                                         N0Findings())
                if rights.status != "GRANTED":
                    raise N0Error(403, "SOURCE_RIGHTS_DENIED", f"{row.source_id} does not permit VERIFICATION: "
                                                               f"{rights.reasons}")
                payload = row.canonical["payload"]
                if self._n3_release_invalidated(s, t, payload["release_id"]):
                    raise N0Error(409, "DESIGN_INVALIDATED", f"N3 invalidated the release behind {aid}@{row.revision}")
                refs.append(DesignRef(scope=scope, artifact=f"{aid}@{row.revision}", design_revision=row.revision,
                                      payload_sha256=row.payload_sha256, manifest_id=manifest.manifest_id,
                                      approval_id=manifest.approval_id, n3_release_id=payload["release_id"]))
                for c in (payload.get("simulation") or {}).get("corners", []):
                    corners.append({**c, "design_ref": f"{aid}@{row.revision}"})
            if problems:
                raise N0Error(409, "DESIGN_SIGNATURE_INVALID", "; ".join(problems))
            src = repo.get_source(t, req.release_source_id)
            if src is None or src.approval_status != "APPROVED" or "LIFECYCLE" not in src.permitted_use:
                raise N0Error(409, "RELEASE_SOURCE_NOT_APPROVED", f"{req.release_source_id} must be APPROVED and permit "
                                                                  "LIFECYCLE (plus COMMERCIAL / FINANCE for N6 / N7)")
            hazards = canon.get("hazards") or []
            critical = set(canon.get("safety_critical_requirement_ids") or []) | \
                {rid for h in hazards for rid in h["requirement_ids"]}
            reqs = [TraceRequirement(req_id=r["req_id"], parameter=r["parameter"], value=r.get("value"),
                                     unit=r.get("unit"), tolerance=r.get("tolerance"),
                                     value_origin=r.get("value_origin"), safety_critical=r["req_id"] in critical,
                                     hazard_ids=[h["hazard_id"] for h in hazards if r["req_id"] in h["requirement_ids"]])
                    for r in canon["requirements"]]
            pcb = next((x.design_revision for x in refs if x.scope == "schematic_pcb"), None)
            synthetic = prd.synthetic or any(repo.get_artifact(t, *x.artifact.split("@")).synthetic for x in refs)
            for irow in s.scalars(select(N4IntakeRow).where(N4IntakeRow.tenant_id == t, N4IntakeRow.prd_id == req.prd_id,
                                                            N4IntakeRow.prd_revision == prd.revision)):
                old = N4DesignIntake.model_validate(irow.record)
                if [x.artifact for x in old.design_refs] == [x.artifact for x in refs] and \
                        old.release_source_id == req.release_source_id and \
                        self._invalidated(s, t, "intake", old.intake_id) is None:
                    return old
            intake = N4DesignIntake(
                intake_id=new_id("N4I"), tenant_id=t, project_id=req.project_id, prd_id=req.prd_id, prd_artifact=prd_ref,
                prd_revision=prd.revision, prd_payload_sha256=prd.payload_sha256, design_refs=refs,
                design_revision=pcb or refs[0].design_revision, pcb_revision=pcb, requirements=reqs, hazards=hazards,
                simulation_corners=corners, release_source_id=req.release_source_id, synthetic=synthetic,
                locked_by=p.principal_id, locked_at=self.clock())
            intake = _with_hash(intake, "intake_sha256")
            s.add(N4IntakeRow(intake_id=intake.intake_id, tenant_id=t, prd_id=req.prd_id, prd_revision=prd.revision,
                              design_revision=intake.design_revision, record=dump(intake)))
            for x in refs:
                repo.add_edge(t, f"artifact:{x.artifact}", f"n4_intake:{intake.intake_id}", "VERIFIED_BY")
            self._event(s, t, "intake", intake.intake_id, "LOCKED", p.principal_id)
            self._audit(s, p, "N4_INTAKE_LOCKED", f"n4_intake:{intake.intake_id}",
                        {"design_refs": [x.artifact for x in refs], "prd": prd_ref,
                         "safety_critical": sorted(critical), "intake_sha256": intake.intake_sha256})
        return intake

    def _invalidate_intake(self, s, tenant: str, intake_id: str, reason: str) -> None:
        if self._invalidated(s, tenant, "intake", intake_id):
            return
        self._event(s, tenant, "intake", intake_id, "INVALIDATED", "svc-n4", reason=reason)
        for pr in s.scalars(select(N4PlanRow).where(N4PlanRow.tenant_id == tenant, N4PlanRow.intake_id == intake_id)):
            self._event(s, tenant, "plan", pr.plan_id, "INVALIDATED", "svc-n4", reason=reason)
            for rr in s.scalars(select(N4ReleaseRow).where(N4ReleaseRow.tenant_id == tenant,
                                                           N4ReleaseRow.plan_id == pr.plan_id)):
                self._event(s, tenant, "release", rr.release_id, "INVALIDATED", "svc-n4", reason=reason)
        Repository(s).append_audit(tenant_id=tenant, event_type="N4_INTAKE_INVALIDATED", actor_id=f"svc-n4@{tenant}",
                                   actor_roles=["node_service"], subject=f"n4_intake:{intake_id}",
                                   details={"reason": reason})

    def on_prd_release(self, tenant: str, prd_id: str, revision: str) -> None:
        with self.db.session() as s:
            for row in s.scalars(select(N4IntakeRow).where(N4IntakeRow.tenant_id == tenant, N4IntakeRow.prd_id == prd_id)):
                if revision_order(row.prd_revision) < revision_order(revision):
                    self._invalidate_intake(s, tenant, row.intake_id, f"PRD {prd_id} revised {row.prd_revision} → "
                                                                      f"{revision}")

    def on_design_release(self, rel: N3Release) -> None:
        prd_id = rel.prd_artifact.split("@")[0].removeprefix("PRD-")
        aid = f"DSN-{prd_id}-{rel.scope}"
        with self.db.session() as s:
            for row in s.scalars(select(N4IntakeRow).where(N4IntakeRow.tenant_id == rel.tenant_id,
                                                           N4IntakeRow.prd_id == prd_id)):
                intake = N4DesignIntake.model_validate(row.record)
                ref = next((x for x in intake.design_refs if x.artifact.startswith(aid + "@")), None)
                if ref and revision_order(ref.design_revision) < revision_order(rel.design_revision):
                    self._invalidate_intake(s, rel.tenant_id, intake.intake_id,
                                            f"N3 released {aid}@{rel.design_revision} (intake locked to "
                                            f"{ref.design_revision})")

    def _active(self, tenant: str, intake_id: str) -> N4DesignIntake:
        with self.db.session() as s:
            intake = self._get(s, N4IntakeRow, N4DesignIntake, tenant, intake_id, "INTAKE_NOT_FOUND")
            inv = self._invalidated(s, tenant, "intake", intake_id)
            reason = None
            if inv is None:
                revs = [r.revision for r in s.scalars(select(N2ReleaseRow).where(
                    N2ReleaseRow.tenant_id == tenant, N2ReleaseRow.prd_id == intake.prd_id))]
                if revs and revision_order(max(revs, key=revision_order)) > revision_order(intake.prd_revision):
                    reason = f"PRD {intake.prd_id} revised to {max(revs, key=revision_order)}"
                repo = Repository(s)
                for x in intake.design_refs:
                    aid, rev = x.artifact.split("@")
                    top = repo.latest_revision(tenant, aid)
                    if top is not None and revision_order(top.revision) > revision_order(rev):
                        reason = f"{aid} has a newer revision {top.revision}"
                    if self._n3_release_invalidated(s, tenant, x.n3_release_id):
                        reason = f"N3 invalidated the release behind {x.artifact}"
        if inv is not None:
            raise N0Error(409, "INTAKE_INVALIDATED", f"intake {intake_id} was invalidated: {inv.get('reason')}",
                          intake_id=intake_id)
        if reason:
            with self.db.session() as s:
                self._invalidate_intake(s, tenant, intake_id, reason)
            raise N0Error(409, "INTAKE_INVALIDATED", f"intake {intake_id} is no longer valid: {reason}",
                          intake_id=intake_id)
        return intake

    def _plan(self, s, tenant: str, plan_id: str) -> N4TestPlan:
        return self._get(s, N4PlanRow, N4TestPlan, tenant, plan_id, "PLAN_NOT_FOUND")

    def _active_plan(self, tenant: str, plan_id: str) -> tuple[N4TestPlan, N4DesignIntake]:
        with self.db.session() as s:
            plan = self._plan(s, tenant, plan_id)
        return plan, self._active(tenant, plan.intake_id)

    # ==========================================================================================
    # N4.2 plan and traceability
    # ==========================================================================================
    def create_plan(self, p: Principal, intake_id: str, req: TestPlanRequest) -> N4TestPlan:
        intake = self._active(p.tenant_id, intake_id)
        ids = [tc.test_id for tc in req.test_cases]
        if len(set(ids)) != len(ids):
            raise N0Error(422, "DUPLICATE_TEST_ID", "test_id values must be unique within a plan")
        plan = N4TestPlan(plan_id=new_id("N4P"), tenant_id=p.tenant_id, intake_id=intake_id, title=req.title,
                          prd_artifact=intake.prd_artifact, design_revision=intake.design_revision,
                          pcb_revision=intake.pcb_revision, test_cases=req.test_cases, synthetic=intake.synthetic,
                          created_by=p.principal_id, created_at=self.clock())
        plan = _with_hash(plan, "plan_sha256")
        with self.db.session() as s:
            s.add(N4PlanRow(plan_id=plan.plan_id, tenant_id=p.tenant_id, intake_id=intake_id,
                            plan_sha256=plan.plan_sha256, record=dump(plan)))
            Repository(s).add_edge(p.tenant_id, f"n4_intake:{intake_id}", f"n4_plan:{plan.plan_id}", "PLANNED")
            self._event(s, p.tenant_id, "plan", plan.plan_id, "PLANNED", p.principal_id)
            matrix = engine.traceability(plan, intake)
            self._audit(s, p, "N4_PLAN_CREATED", f"n4_plan:{plan.plan_id}",
                        {"tests": len(ids), "plan_sha256": plan.plan_sha256,
                         "safety_critical_coverage_pct": matrix.safety_critical_coverage_pct})
        return plan

    def traceability(self, tenant: str, plan_id: str) -> N4TraceabilityMatrix:
        with self.db.session() as s:
            plan = self._plan(s, tenant, plan_id)
            intake = self._get(s, N4IntakeRow, N4DesignIntake, tenant, plan.intake_id, "INTAKE_NOT_FOUND")
        return engine.traceability(plan, intake)

    # ==========================================================================================
    # N4.3 PFMEA, control plan, CAPA
    # ==========================================================================================
    def _latest_pfmea(self, s, tenant: str, plan_id: str) -> Optional[N4Pfmea]:
        rows = list(s.scalars(select(N4PfmeaRow).where(N4PfmeaRow.tenant_id == tenant, N4PfmeaRow.plan_id == plan_id)))
        return N4Pfmea.model_validate(max(rows, key=lambda r: r.revision).record) if rows else None

    def submit_pfmea(self, p: Principal, plan_id: str, req: PfmeaRequest) -> N4Pfmea:
        plan, intake = self._active_plan(p.tenant_id, plan_id)
        ids = [e.entry_id for e in req.entries]
        if len(set(ids)) != len(ids):
            raise N0Error(422, "DUPLICATE_ENTRY_ID", "PFMEA entry ids must be unique")
        with self.db.session() as s:
            for e in req.entries:
                if e.adopted_from_suggestion:
                    sg = s.get(N4SuggestionRow, e.adopted_from_suggestion)
                    if sg is None or sg.tenant_id != p.tenant_id or sg.plan_id != plan_id:
                        raise N0Error(422, "SUGGESTION_NOT_FOUND", f"{e.adopted_from_suggestion} is not a suggestion "
                                                                   "for this plan")
            prev = self._latest_pfmea(s, p.tenant_id, plan_id)
            pf = N4Pfmea(pfmea_id=new_id("N4FM"), tenant_id=p.tenant_id, plan_id=plan_id,
                         revision=(prev.revision + 1) if prev else 1,
                         entries=[engine.rate_entry(e, p.principal_id, self.ap) for e in req.entries],
                         prioritization_method="AIAG_VDA_AP_LICENSED" if self.ap else "CUSTOM_ATTENTION_HEURISTIC",
                         prioritization_label=(f"AIAG-VDA Action Priority from the licensed table "
                                               f"{self.ap.license_ref}; attention levels remain the custom heuristic"
                                               if self.ap else HEURISTIC_LABEL),
                         ap_license_ref=self.ap.license_ref if self.ap else None, created_by=p.principal_id,
                         created_at=self.clock())
            pf = _with_hash(pf, "pfmea_sha256")
            s.add(N4PfmeaRow(pfmea_id=pf.pfmea_id, tenant_id=p.tenant_id, plan_id=plan_id, revision=pf.revision,
                             record=dump(pf)))
            self._audit(s, p, "N4_PFMEA_REVISED", f"n4_plan:{plan_id}",
                        {"revision": pf.revision, "entries": len(pf.entries), "method": pf.prioritization_method})
        return pf

    def submit_control_plan(self, p: Principal, plan_id: str, req: ControlPlanRequest) -> N4ControlPlan:
        plan, _ = self._active_plan(p.tenant_id, plan_id)
        tests = {tc.test_id for tc in plan.test_cases}
        bad = [i.test_id for i in req.items if i.test_id and i.test_id not in tests]
        if bad:
            raise N0Error(422, "UNKNOWN_TEST", f"control plan references unknown tests {bad}")
        with self.db.session() as s:
            n = len(list(s.scalars(select(N4ControlPlanRow).where(N4ControlPlanRow.tenant_id == p.tenant_id,
                                                                  N4ControlPlanRow.plan_id == plan_id))))
            cp = N4ControlPlan(control_plan_id=new_id("N4CP"), tenant_id=p.tenant_id, plan_id=plan_id, revision=n + 1,
                               items=req.items, created_by=p.principal_id, created_at=self.clock())
            s.add(N4ControlPlanRow(control_plan_id=cp.control_plan_id, tenant_id=p.tenant_id, plan_id=plan_id,
                                   revision=cp.revision, record=dump(cp)))
            self._audit(s, p, "N4_CONTROL_PLAN", f"n4_plan:{plan_id}", {"revision": cp.revision})
        return cp

    def _latest_control_plan(self, s, tenant: str, plan_id: str) -> Optional[N4ControlPlan]:
        rows = list(s.scalars(select(N4ControlPlanRow).where(N4ControlPlanRow.tenant_id == tenant,
                                                             N4ControlPlanRow.plan_id == plan_id)))
        return N4ControlPlan.model_validate(max(rows, key=lambda r: r.revision).record) if rows else None

    def _capas(self, s, tenant: str, plan_id: Optional[str] = None) -> dict[str, N4Capa]:
        return {k: v for k, v in self._latest(s, N4CapaRow, N4Capa, tenant).items()
                if plan_id is None or v.plan_id == plan_id}

    def open_capa(self, p: Principal, req: CapaRequest) -> N4Capa:
        self._active_plan(p.tenant_id, req.plan_id)
        with self.db.session() as s:
            if req.capa_id in self._capas(s, p.tenant_id):
                raise N0Error(409, "CAPA_EXISTS", f"{req.capa_id} exists")
            c = N4Capa(**req.model_dump(), tenant_id=p.tenant_id, status="OPEN", opened_by=p.principal_id,
                       opened_at=self.clock())
            s.add(N4CapaRow(capa_id=c.capa_id, seq=1, tenant_id=p.tenant_id, plan_id=c.plan_id, status="OPEN",
                            record=dump(c)))
            self._audit(s, p, "N4_CAPA_OPENED", f"n4_capa:{c.capa_id}", {"entries": c.pfmea_entry_ids})
        return c

    def verify_capa(self, p: Principal, capa_id: str, req: CapaVerifyRequest) -> N4Capa:
        with self.db.session() as s:
            cur = self._capas(s, p.tenant_id).get(capa_id)
            if cur is None:
                raise N0Error(404, "CAPA_NOT_FOUND", f"{capa_id} not found")
            if cur.status != "OPEN":
                raise N0Error(409, "INVALID_TRANSITION", f"{capa_id} is {cur.status}")
            if p.principal_id in (cur.owner, cur.opened_by):
                raise N0Error(403, "SEPARATION_OF_DUTIES", "the CAPA owner cannot verify its effectiveness")
            ev = s.get(N4EvidenceRow, req.evidence_id)
            if ev is None or ev.tenant_id != p.tenant_id or ev.plan_id != cur.plan_id or ev.status != "ACCEPTED":
                raise N0Error(409, "EVIDENCE_NOT_ACCEPTED", f"{req.evidence_id} is not accepted evidence of "
                                                            f"{cur.plan_id}")
            new = cur.model_copy(update={"status": "VERIFIED_EFFECTIVE" if req.effective else "VERIFIED_INEFFECTIVE",
                                         "verified_by": p.principal_id, "verified_at": self.clock(),
                                         "verification_evidence_id": req.evidence_id,
                                         "verification_evidence_sha256": ev.record.get("evidence_sha256"),
                                         "notes": req.notes})
            n = len(list(s.scalars(select(N4CapaRow).where(N4CapaRow.tenant_id == p.tenant_id,
                                                           N4CapaRow.capa_id == capa_id))))
            s.add(N4CapaRow(capa_id=capa_id, seq=n + 1, tenant_id=p.tenant_id, plan_id=cur.plan_id, status=new.status,
                            record=dump(new)))
            self._audit(s, p, "N4_CAPA_VERIFIED", f"n4_capa:{capa_id}", {"status": new.status,
                                                                         "evidence_id": req.evidence_id})
        return new

    # ==========================================================================================
    # N4.5 assessment
    # ==========================================================================================
    def _evidence(self, s, tenant: str, plan_id: str) -> tuple[list[N4EvidenceIntake], list[N4EvidenceIntake],
                                                                list[N4Measurement], list[N4Adjudication]]:
        rows = list(s.scalars(select(N4EvidenceRow).where(N4EvidenceRow.tenant_id == tenant,
                                                          N4EvidenceRow.plan_id == plan_id)
                              .order_by(N4EvidenceRow.created_at, N4EvidenceRow.evidence_id)))
        ok = [N4EvidenceIntake.model_validate(r.record) for r in rows if r.status == "ACCEPTED"]
        rejected = [N4EvidenceIntake.model_validate(r.record) for r in rows if r.status == "REJECTED"]
        ms = [N4Measurement.model_validate(r.record) for r in s.scalars(
            select(N4MeasurementRow).where(N4MeasurementRow.tenant_id == tenant, N4MeasurementRow.plan_id == plan_id))]
        ok_ids = {e.evidence_id for e in ok}
        adj = [N4Adjudication.model_validate(r.record) for r in s.scalars(
            select(N4AdjudicationRow).where(N4AdjudicationRow.tenant_id == tenant).order_by(N4AdjudicationRow.created_at))
               if r.evidence_id in ok_ids]
        return ok, rejected, [m for m in ms if m.evidence_id in ok_ids], adj

    def _evidence_hash(self, plan: N4TestPlan, intake: N4DesignIntake, intakes: list[N4EvidenceIntake],
                       adj: list[N4Adjudication], pfmea: Optional[N4Pfmea], capas: dict[str, N4Capa]) -> str:
        return sha256_hex(canonical_json({
            "plan_sha256": plan.plan_sha256, "intake_sha256": intake.intake_sha256,
            "design_revision": intake.design_revision, "pcb_revision": intake.pcb_revision,
            "evidence": sorted([e.evidence_id, e.evidence_sha256] for e in intakes),
            "adjudications": sorted(a.adjudication_id for a in adj),
            "pfmea_sha256": pfmea.pfmea_sha256 if pfmea else None,
            "capas": sorted([c.capa_id, c.status] for c in capas.values())}))

    def assess(self, p: Principal, plan_id: str) -> N4VerificationAssessment:
        plan, intake = self._active_plan(p.tenant_id, plan_id)
        t = p.tenant_id
        with self.db.session() as s:
            ok, rejected, ms, adj = self._evidence(s, t, plan_id)
            reg = self._registry(s, t)
            pfmea = self._latest_pfmea(s, t, plan_id)
            capas = self._capas(s, t, plan_id)
        tampered = self._verify_blobs(t, ok)
        findings, verdicts, matrix = engine.assess(
            plan, intake, engine.Evidence(ok, ms, adj, tampered, rejected), reg, pfmea, capas, self.clock(),
            self.settings.min_tur, self.settings.max_clock_skew_s)
        a = N4VerificationAssessment(
            assessment_id=new_id("N4A"), tenant_id=t, plan_id=plan_id, intake_id=intake.intake_id,
            design_revision=intake.design_revision, pcb_revision=intake.pcb_revision,
            decision=engine.decide(findings), findings=findings, test_verdicts=verdicts, traceability=matrix,
            evidence_ids=[e.evidence_id for e in ok], evidence_submitters=sorted({e.submitted_by for e in ok}),
            evidence_sha256=self._evidence_hash(plan, intake, ok, adj, pfmea, capas), plan_sha256=plan.plan_sha256,
            pfmea_id=pfmea.pfmea_id if pfmea else None,
            prioritization_method=pfmea.prioritization_method if pfmea else "CUSTOM_ATTENTION_HEURISTIC",
            unit_serials=sorted({m.unit_serial for m in ms if m.unit_serial}), engine_version=engine.ENGINE_VERSION,
            assessed_by=p.principal_id, assessed_at=self.clock())
        a = _with_hash(a, "assessment_sha256")
        with self.db.session() as s:
            s.add(N4AssessmentRow(assessment_id=a.assessment_id, tenant_id=t, plan_id=plan_id, decision=a.decision,
                                  record=dump(a)))
            Repository(s).add_edge(t, f"n4_plan:{plan_id}", f"n4_assessment:{a.assessment_id}", "ASSESSED_AS",
                                   decision=a.decision)
            self._event(s, t, "plan", plan_id, "ASSESSED", p.principal_id, assessment_id=a.assessment_id,
                        decision=a.decision)
            self._audit(s, p, "N4_ASSESSED", f"n4_assessment:{a.assessment_id}",
                        {"decision": a.decision, "evidence_sha256": a.evidence_sha256,
                         "codes": sorted({f.code for f in findings if f.outcome != "INFO"})})
        return a

    def _assessments(self, s, tenant: str, plan_id: str) -> list[N4VerificationAssessment]:
        return [N4VerificationAssessment.model_validate(r.record) for r in s.scalars(
            select(N4AssessmentRow).where(N4AssessmentRow.tenant_id == tenant, N4AssessmentRow.plan_id == plan_id)
            .order_by(N4AssessmentRow.created_at, N4AssessmentRow.assessment_id))]

    def _reviews(self, s, tenant: str, assessment_id: str) -> list[N4Review]:
        return [N4Review.model_validate(r.record) for r in s.scalars(
            select(N4ReviewRow).where(N4ReviewRow.tenant_id == tenant, N4ReviewRow.assessment_id == assessment_id)
            .order_by(N4ReviewRow.created_at))]

    # ==========================================================================================
    # N4.6 review
    # ==========================================================================================
    def _qualified(self, s, p: Principal, methods: set[str]) -> Optional[str]:
        q = self._latest(s, N4QualificationRow, N4Qualification, p.tenant_id).get(p.principal_id)
        if q is None:
            return "no qualification record"
        if q.valid_until < date.today():
            return f"qualification expired {q.valid_until}"
        missing = sorted(methods - set(q.methods))
        return f"not qualified for {missing}" if missing else None

    def review(self, p: Principal, req: N4ReviewRequest) -> N4Review:
        t = p.tenant_id
        with self.db.session() as s:
            a = self._get(s, N4AssessmentRow, N4VerificationAssessment, t, req.assessment_id, "ASSESSMENT_NOT_FOUND")
            plan = self._plan(s, t, a.plan_id)
        self._active(t, a.intake_id)
        with self.db.session() as s:
            latest = self._assessments(s, t, a.plan_id)[-1]
            if latest.assessment_id != a.assessment_id:
                raise N0Error(409, "STALE_ASSESSMENT", f"{a.assessment_id} is superseded by {latest.assessment_id}")
            if req.evidence_sha256 != a.evidence_sha256:
                raise N0Error(409, "EVIDENCE_HASH_MISMATCH", "the approval must name the assessment's exact evidence "
                                                             f"hash {a.evidence_sha256}")
            if req.design_revision != a.design_revision:
                raise N0Error(409, "DESIGN_REVISION_MISMATCH", f"the assessment is for design revision "
                                                               f"{a.design_revision}")
            ok, *_ = self._evidence(s, t, a.plan_id)
            if req.decision == "APPROVED" and a.decision == "BLOCK":
                raise N0Error(409, "BLOCK_NOT_APPROVABLE", "a hard BLOCK cannot be approved or overridden",
                              codes=sorted({f.code for f in a.findings if f.outcome == "BLOCK"}))
            if req.decision == "APPROVED" and a.decision == "REWORK":
                raise N0Error(409, "REWORK_OPEN", "the assessment has open REWORK findings",
                              codes=sorted({f.code for f in a.findings if f.outcome == "REWORK"}))
            contributors = set(a.evidence_submitters) | {a.assessed_by, plan.created_by}
            if p.principal_id in contributors:
                raise N0Error(403, "SEPARATION_OF_DUTIES", "the approver must be separate from the plan author, the "
                                                           "assessor and every evidence submitter")
            if self.settings.require_qualification:
                why = self._qualified(s, p, {tc.method for tc in plan.test_cases})
                if why:
                    raise N0Error(403, "NOT_QUALIFIED", f"{p.principal_id}: {why}")
        tampered = self._verify_blobs(t, ok)
        if tampered:
            raise N0Error(409, "EVIDENCE_HASH_MISMATCH", f"stored evidence changed since the assessment: {tampered}")
        role = "quality_lead" if p.has("quality_lead") else "test_authority"
        with self.db.session() as s:
            repo = Repository(s)
            ev = repo.append_approval(tenant_id=t, artifact_id=f"n4-assessment:{a.assessment_id}",
                                      exact_revision=a.evidence_sha256[:32], approver_id=p.principal_id,
                                      approver_role=role, decision=req.decision, scope="EVIDENCE_ATTESTATION",
                                      reason=f"{req.reason} [design {a.design_revision}, evidence {a.evidence_sha256}]")
            r = N4Review(review_id=new_id("N4RV"), tenant_id=t, assessment_id=a.assessment_id, plan_id=a.plan_id,
                         reviewer=p.principal_id, reviewer_role=role, decision=req.decision, reason=req.reason,
                         evidence_sha256=a.evidence_sha256, design_revision=a.design_revision,
                         approval_event_id=ev.approval_id, reviewed_at=self.clock())
            s.add(N4ReviewRow(review_id=r.review_id, tenant_id=t, assessment_id=a.assessment_id, decision=r.decision,
                              record=dump(r)))
            repo.add_edge(t, f"approval:{ev.approval_id}", f"n4_assessment:{a.assessment_id}",
                          "APPROVES" if req.decision == "APPROVED" else "REJECTS", evidence_sha256=a.evidence_sha256)
            self._event(s, t, "plan", a.plan_id, "REVIEW_" + req.decision, p.principal_id, review_id=r.review_id)
            self._audit(s, p, "N4_REVIEW_" + req.decision, f"n4_assessment:{a.assessment_id}",
                        {"review_id": r.review_id, "approval_id": ev.approval_id, "evidence_sha256": a.evidence_sha256})
        return r

    # ==========================================================================================
    # N4.7 release
    # ==========================================================================================
    def _payload(self, target: str, release_id: str, plan: N4TestPlan, intake: N4DesignIntake,
                 a: N4VerificationAssessment, r: N4Review, cp: Optional[N4ControlPlan],
                 rel_ill: Optional[N4ReliabilityIllustration], lab_certs: list[dict]) -> dict:
        verdict = {v.test_id: v.verdict for v in a.test_verdicts}
        trace = []
        for row in a.traceability.rows:
            ok = bool(row.test_ids) and all(verdict.get(tid) == "PASS" for tid in row.test_ids)
            trace.append(TraceEntry(requirement_id=row.requirement_id, safety_critical=row.safety_critical,
                                    hazard_ids=row.hazard_ids, test_ids=row.test_ids, methods=row.methods,
                                    verdict="VERIFIED" if ok else "NOT_VERIFIED").model_dump(mode="json"))
        p = {"release_id": release_id, "target": target, "prd_ref": intake.prd_artifact,
             "design_refs": [x.artifact for x in intake.design_refs], "design_revision": intake.design_revision,
             "pcb_revision": intake.pcb_revision, "plan_id": plan.plan_id, "plan_sha256": plan.plan_sha256,
             "assessment_id": a.assessment_id, "evidence_sha256": a.evidence_sha256, "review_id": r.review_id,
             "approved_by": r.reviewer, "evidence_submitters": a.evidence_submitters,
             "verification_status": "VERIFIED" if a.decision == "PASS" else "NOT_VERIFIED",
             "measurements": [m.model_dump(mode="json") for v in a.test_verdicts for m in v.measurements],
             "attribute_inspections": [i.model_dump(mode="json") for v in a.test_verdicts
                                       for i in v.attribute_inspections],
             "traceability": trace, "unit_serials": [u for u in a.unit_serials if not u.startswith("corner:")],
             "prioritization_method": a.prioritization_method,
             "ap_license_ref": self.ap.license_ref if self.ap else None, "approval_scope": APPROVAL_SCOPE,
             "lab_certificates": lab_certs}
        if target == "N8":
            p["control_plan"] = [i.model_dump(mode="json") for i in cp.items] if cp else []
            if rel_ill is not None:
                p["reliability"] = rel_ill.model_dump(mode="json")
        elif target == "N6":
            reqs = {x.req_id: x for x in intake.requirements}
            p["commercial"] = {
                "claims": [f"{t['requirement_id']} {reqs[t['requirement_id']].parameter} = "
                           f"{reqs[t['requirement_id']].value} {reqs[t['requirement_id']].unit or ''} verified by "
                           f"{'/'.join(t['methods'])}".strip() for t in trace if t["verdict"] == "VERIFIED"],
                "not_claimable": ["reliability / MTBF (research-only illustration)",
                                  "safety certification (only the accredited certificates listed, for their scope)"]}
        else:
            ms = p["measurements"]
            p["finance"] = {"test_cases": len(plan.test_cases), "measurements": len(ms),
                            "units_tested": len(p["unit_serials"]), "accredited_lab_reports": len(lab_certs),
                            "cost_data": "not captured by N4; N7 owns verification costing"}
        return p

    def release(self, p: Principal, plan_id: str, req: N4ReleaseRequest) -> N4VerificationRelease:
        plan, intake = self._active_plan(p.tenant_id, plan_id)
        t = p.tenant_id
        with self.db.session() as s:
            hist = self._assessments(s, t, plan_id)
            if not hist:
                raise N0Error(409, "NOT_ASSESSED", f"{plan_id} has no assessment")
            a = hist[-1]
            rv = self._reviews(s, t, a.assessment_id)
            if a.decision != "PASS":
                raise N0Error(409, "NOT_VERIFIED", f"latest assessment {a.assessment_id} is {a.decision}")
            if not rv or rv[-1].decision != "APPROVED" or rv[-1].evidence_sha256 != a.evidence_sha256:
                raise N0Error(409, "APPROVAL_REQUIRED", f"{a.assessment_id} has no approval bound to its evidence hash")
            ok, _, _, adj = self._evidence(s, t, plan_id)
            current = self._evidence_hash(plan, intake, ok, adj, self._latest_pfmea(s, t, plan_id),
                                          self._capas(s, t, plan_id))
            if current != a.evidence_sha256:
                raise N0Error(409, "EVIDENCE_CHANGED", "evidence changed after the approved assessment; re-assess")
            if p.principal_id in set(a.evidence_submitters) | {plan.created_by}:
                raise N0Error(403, "SEPARATION_OF_DUTIES", "an evidence submitter cannot release the package")
            cp = self._latest_control_plan(s, t, plan_id)
            ill = [N4ReliabilityIllustration.model_validate(x.record) for x in s.scalars(
                select(N4ReliabilityRow).where(N4ReliabilityRow.tenant_id == t, N4ReliabilityRow.plan_id == plan_id)
                .order_by(N4ReliabilityRow.created_at))]
            certs = [{"certificate_number": e.metadata.get("certificate_number"), "lab": e.metadata.get("lab"),
                      "accreditation_number": e.metadata.get("accreditation_number"),
                      "pcb_revision": e.metadata.get("pcb_revision"), "report_sha256": e.metadata.get("report_sha256")}
                     for e in ok if e.adapter == "ACCREDITED_LAB"]
        tampered = self._verify_blobs(t, ok)
        if tampered:
            raise N0Error(409, "EVIDENCE_HASH_MISMATCH", f"stored evidence changed since approval: {tampered}")
        review = rv[-1]
        payload_release_id = "N4R-" + sha256_hex(canonical_json({"assessment": a.assessment_id,
                                                                  "review": review.review_id}))[:24]
        packages = []
        for target in req.targets:
            payload = self._payload(target, payload_release_id, plan, intake, a, review, cp,
                                    ill[-1] if ill else None, certs)
            packages.append(self._submit_and_release(p, intake, target, payload))
        released = [x for x in packages if x.status == "RELEASED"]
        status = "RELEASED" if len(released) == len(packages) else "PARTIAL" if released else "REFUSED"
        rel = N4VerificationRelease(release_id=new_id("N4REL"), tenant_id=t, plan_id=plan_id,
                                    assessment_id=a.assessment_id, review_id=review.review_id,
                                    design_revision=intake.design_revision, evidence_sha256=a.evidence_sha256,
                                    payload_release_id=payload_release_id, status=status, packages=packages,
                                    released_by=p.principal_id, released_at=self.clock())
        with self.db.session() as s:
            s.add(N4ReleaseRow(release_id=rel.release_id, tenant_id=t, plan_id=plan_id, record=dump(rel)))
            self._event(s, t, "release", rel.release_id, status, p.principal_id)
            self._audit(s, p, "N4_RELEASE_" + status, f"n4_release:{rel.release_id}",
                        {"packages": [{"target": x.target, "status": x.status, "manifest_id": x.manifest_id,
                                       "codes": x.violated_codes} for x in packages]})
        return rel

    def _submit_and_release(self, p: Principal, intake: N4DesignIntake, target: str, payload: dict) -> N4ReleasePackage:
        aid = f"VER-{intake.prd_id}-{target}"
        sha = sha256_hex(canonical_json(payload))
        with self.db.session() as s:
            top = Repository(s).latest_revision(p.tenant_id, aid)
            if top is None:
                rev = "A"
            elif top.payload_sha256 == sha:
                rev = top.revision
            else:
                n = revision_order(top.revision) + 1
                rev = ""
                while n:
                    n, rr = divmod(n - 1, 26)
                    rev = chr(65 + rr) + rev
        art = Artifact(tenant_id=p.tenant_id, project_id=intake.project_id, artifact_id=aid,
                       artifact_type="VERIFICATION_RELEASE", source_id=intake.release_source_id,
                       processing_purpose=PURPOSE[target], release_stage="CONCEPT" if intake.synthetic else "PROTOTYPE",
                       revision=rev, synthetic=intake.synthetic, source_sha256=sha, payload=payload,
                       source_artifact_ids=[x.artifact for x in intake.design_refs] + [intake.prd_artifact])
        res = self.pipeline.evaluate(self._svc(p), EvaluateRequest(artifact=art, origin_node="N4",
                                                                   requested_destinations=[target]))
        d = res.decision
        rev = res.canonical_artifact.revision if res.canonical_artifact else rev
        base = dict(target=target, artifact_id=aid, revision=rev, n0_decision=d.decision, n0_decision_id=d.decision_id,
                    violated_codes=sorted({v.code for v in d.violated_rules}))
        if d.decision != "PASS":
            return N4ReleasePackage(**base, status="REFUSED", reason=f"N0 decision {d.decision}: {d.explanation}"[:1000])
        try:
            with self.db.session() as s:
                repo = Repository(s)
                ap = record_approval(repo, p, ApprovalRequest(
                    artifact_id=aid, exact_revision=rev, scope="RELEASE", decision="APPROVED",
                    reason=f"N4 verification release to {target} for design {intake.design_revision}"), self.clock(),
                    self.store)
                m = release_handoff(repo, p, HandoffReleaseRequest(artifact_id=aid, revision=rev, source_node="N4",
                                                                   target_nodes=[target],
                                                                   approval_id=ap.approval_event.approval_id),
                                    self.clock())
        except N0Error as err:
            return N4ReleasePackage(**base, status="REFUSED", reason=f"{err.code}: {err.message}"[:1000])
        return N4ReleasePackage(**base, status="RELEASED", manifest_id=m.manifest_id,
                                approval_id=ap.approval_event.approval_id,
                                reason=f"released to {target} via N0 manifest {m.manifest_id}")

    def n8_draft(self, tenant: str, plan_id: str) -> dict:
        """Preview of the N8 package from the latest assessment. Never released, never sent to N0."""
        with self.db.session() as s:
            plan = self._plan(s, tenant, plan_id)
            intake = self._get(s, N4IntakeRow, N4DesignIntake, tenant, plan.intake_id, "INTAKE_NOT_FOUND")
            hist = self._assessments(s, tenant, plan_id)
            cp = self._latest_control_plan(s, tenant, plan_id)
        if not hist:
            raise N0Error(409, "NOT_ASSESSED", f"{plan_id} has no assessment")
        a = hist[-1]
        stub = N4Review(review_id="DRAFT", tenant_id=tenant, assessment_id=a.assessment_id, plan_id=plan_id,
                        reviewer="(none: not approved)", reviewer_role="none", decision="REJECTED",
                        reason="draft preview, not approved", evidence_sha256=a.evidence_sha256,
                        design_revision=a.design_revision, approval_event_id="none", reviewed_at=self.clock())
        payload = self._payload("N8", "N4R-DRAFT", plan, intake, a, stub, cp, None, [])
        return {"status": "DRAFT_NOT_RELEASED", "assessment_decision": a.decision,
                "blocking_findings": [f.model_dump(mode="json") for f in a.findings if f.outcome == "BLOCK"],
                "note": "A draft is never handed to N8. Only an approved, N0-checked VERIFICATION_RELEASE reaches N8.",
                "payload": payload}

    # ==========================================================================================
    # AI (opt-in) and reliability (research only)
    # ==========================================================================================
    def add_corpus_document(self, p: Principal, req: CorpusDocumentRequest) -> dict:
        t = p.tenant_id
        with self.db.session() as s:
            src = Repository(s).get_source(t, req.source_id)
            if src is None or src.approval_status != "APPROVED" or "LLM_EXTRACTION" not in src.permitted_use:
                raise N0Error(403, "SOURCE_NOT_AUTHORIZED_FOR_AI", f"{req.source_id} must be APPROVED and permit "
                                                                   "LLM_EXTRACTION")
            if req.kind == "VERIFIED_CAPA":
                c = self._capas(s, t).get(req.capa_id or "")
                if c is None or c.status != "VERIFIED_EFFECTIVE":
                    raise N0Error(409, "CAPA_NOT_VERIFIED", "only CAPAs verified effective may enter the corpus")
            redacted, pii = self.presidio.redact(req.text)
            doc = {**req.model_dump(), "text": redacted, "pii_redacted": len(pii), "added_by": p.principal_id,
                   "added_at": self.clock().isoformat()}
            if s.get(N4CorpusRow, (req.doc_id, t)) is not None:
                raise N0Error(409, "DOCUMENT_EXISTS", f"{req.doc_id} exists")
            s.add(N4CorpusRow(doc_id=req.doc_id, tenant_id=t, record=doc))
            self._audit(s, p, "N4_CORPUS_ADDED", f"n4_corpus:{req.doc_id}", {"kind": req.kind, "pii_redacted": len(pii)})
        return {k: v for k, v in doc.items() if k != "text"}

    def suggest(self, p: Principal, plan_id: str, req: SuggestionRequest) -> N4Suggestion:
        plan, _ = self._active_plan(p.tenant_id, plan_id)
        if self.provider is None:
            raise N0Error(409, "LLM_DISABLED", "LLM + RAG suggestions are opt-in (N4_LLM_PROVIDER) and disabled")
        with self.db.session() as s:
            docs = [r.record for r in s.scalars(select(N4CorpusRow).where(N4CorpusRow.tenant_id == p.tenant_id))]
        if not docs:
            raise N0Error(409, "CORPUS_EMPTY", "no authorized standards, SOPs or verified CAPA records")
        chunks = [c for d in docs for c in ai.chunk(d["doc_id"], d["text"])]
        hits = ai.retrieve(chunks, req.question, self.settings.rag_top_k)
        if not hits:
            raise N0Error(409, "NO_GROUNDING", "nothing in the authorized corpus matches the question")
        try:
            kept, dropped, res = ai.suggest(self.provider, req.kind, req.question, hits, req.max_items)
        except ai.LLMError as exc:
            raise N0Error(502, "LLM_OUTPUT_INVALID", str(exc)) from None
        sg = N4Suggestion(suggestion_id=new_id("N4SG"), tenant_id=p.tenant_id, plan_id=plan_id, kind=req.kind,
                          items=kept, rejected_items=dropped, provider=res.provider, model=res.model_name,
                          corpus_doc_ids=sorted({c.doc_id for c in hits}), created_by=p.principal_id,
                          created_at=self.clock())
        with self.db.session() as s:
            s.add(N4SuggestionRow(suggestion_id=sg.suggestion_id, tenant_id=p.tenant_id, plan_id=plan_id,
                                  record=dump(sg)))
            self._audit(s, p, "N4_SUGGESTION_DRAFTED", f"n4_plan:{plan_id}",
                        {"kind": req.kind, "kept": len(kept), "dropped": len(dropped), "model": res.model_name})
        return sg

    def reliability(self, p: Principal, plan_id: str, req: ReliabilityRequest) -> N4ReliabilityIllustration:
        self._active_plan(p.tenant_id, plan_id)
        hours = sum(u.hours for u in req.units)
        fails = sum(1 for u in req.units if u.failed)
        point, lower = reliability.mtbf(hours, fails, req.confidence)
        ill = N4ReliabilityIllustration(illustration_id=new_id("N4RL"), tenant_id=p.tenant_id, plan_id=plan_id,
                                        total_unit_hours=hours, failures=fails, mtbf_point_h=point,
                                        mtbf_lower_bound_h=lower, confidence=req.confidence,
                                        missing_prerequisites=reliability.PREREQUISITES, created_by=p.principal_id,
                                        created_at=self.clock())
        with self.db.session() as s:
            s.add(N4ReliabilityRow(illustration_id=ill.illustration_id, tenant_id=p.tenant_id, plan_id=plan_id,
                                   record=dump(ill)))
            self._audit(s, p, "N4_RELIABILITY_ILLUSTRATION", f"n4_plan:{plan_id}", {"release_eligible": False})
        return ill

    # ==========================================================================================
    # views
    # ==========================================================================================
    def intake_view(self, tenant: str, intake_id: str) -> dict:
        with self.db.session() as s:
            i = self._get(s, N4IntakeRow, N4DesignIntake, tenant, intake_id, "INTAKE_NOT_FOUND")
        return {"intake": dump(i), "status": self.status(tenant, "intake", intake_id)["status"]}

    def plan_view(self, tenant: str, plan_id: str) -> dict:
        with self.db.session() as s:
            plan = self._plan(s, tenant, plan_id)
            ok, rejected, ms, adj = self._evidence(s, tenant, plan_id)
            pf = self._latest_pfmea(s, tenant, plan_id)
            cp = self._latest_control_plan(s, tenant, plan_id)
            hist = self._assessments(s, tenant, plan_id)
            capas = self._capas(s, tenant, plan_id)
        return {"plan": dump(plan), "status": self.status(tenant, "plan", plan_id)["status"],
                "traceability": dump(self.traceability(tenant, plan_id)),
                "evidence": [dump(e) for e in ok], "rejected_evidence": [dump(e) for e in rejected],
                "measurements": len(ms), "adjudications": [dump(a) for a in adj],
                "pfmea": dump(pf) if pf else None, "control_plan": dump(cp) if cp else None,
                "capas": [dump(c) for c in capas.values()], "assessments": [a.assessment_id for a in hist]}

    def assessment(self, tenant: str, assessment_id: str) -> N4VerificationAssessment:
        with self.db.session() as s:
            return self._get(s, N4AssessmentRow, N4VerificationAssessment, tenant, assessment_id,
                             "ASSESSMENT_NOT_FOUND")

    def release_view(self, tenant: str, release_id: str) -> dict:
        with self.db.session() as s:
            rel = self._get(s, N4ReleaseRow, N4VerificationRelease, tenant, release_id, "RELEASE_NOT_FOUND")
        return {"release": dump(rel), "status": self.status(tenant, "release", release_id)["status"]}

    def release_status_by_payload(self, tenant: str, payload_release_id: str) -> Optional[str]:
        with self.db.session() as s:
            for r in s.scalars(select(N4ReleaseRow).where(N4ReleaseRow.tenant_id == tenant)):
                if r.record.get("payload_release_id") == payload_release_id:
                    return "INVALIDATED" if self._invalidated(s, tenant, "release", r.release_id) \
                        else r.record["status"]
        return None

    def status(self, tenant: str, kind: str, subject_id: str) -> dict:
        if kind not in ("intake", "plan", "release"):
            raise N0Error(422, "UNKNOWN_SUBJECT", "kind must be intake, plan or release")
        with self.db.session() as s:
            row_cls = {"intake": N4IntakeRow, "plan": N4PlanRow, "release": N4ReleaseRow}[kind]
            row = s.get(row_cls, subject_id)
            if row is None or row.tenant_id != tenant:
                raise N0Error(404, "NOT_FOUND", f"{kind} {subject_id} not found")
            hist = self._events(s, tenant, kind, subject_id)
        status = "INVALIDATED" if any(e["status"] == "INVALIDATED" for e in hist) else \
            (row.record["status"] if kind == "release" else hist[-1]["status"] if hist else "ACTIVE")
        return {"subject": f"{kind}:{subject_id}", "status": status, "history": hist}

    def ai_status(self) -> dict:
        return {**ai.STATUS, "llm_rag_suggestions": "ENABLED" if self.provider else "OPT_IN (disabled)",
                "gate_requirements": ai.GATE_REQUIREMENTS, "pfmea_prioritization": (
                    f"AIAG_VDA_AP_LICENSED ({self.ap.license_ref})" if self.ap else HEURISTIC_LABEL),
                "reliability": "RESEARCH_ONLY_STATISTICAL_ILLUSTRATION"}


__all__ = ["N4Service"]
