"""N4.4 evidence intake: SCPI / VISA logs and live capture, accredited lab reports, AOI inference, offline CSV.

Every adapter follows the same controlled path:

1. the evidence source is a registered, APPROVED N0 source that permits VERIFICATION;
2. every file passes N0 screening (MIME sniffing / allow-list, malware scan) and is hashed;
3. the adapter checks its own format (identity, revisions, hashes, accreditation, triage);
4. provenance is *derived*, never trusted. A claim of PHYSICAL evidence from a SYNTHETIC / MIXED
   source, a simulated instrument, an unregistered instrument or a mock fixture is rejected as
   ``SYNTHETIC_AS_PHYSICAL``. Evidence honestly declared synthetic is kept, labelled, and can never
   qualify a test;
5. accepted files go write-once into N0 quarantine storage (tenant + SHA-256). Rejected
   submissions are recorded with their hashes and reasons, and none of their bytes are stored.
"""
from __future__ import annotations

import posixpath
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import ValidationError
from sqlalchemy import select

from ..errors import N0Error
from ..parsing import ParseError
from ..repository import Repository, dump, new_id
from ..rules import canonical_json, sha256_hex
from ..scanning import validate_upload
from ..security import Principal
from ..storage import make_uri
from .adapters import aoi, csv_offline, lab_report, scpi
from .db import N4AdjudicationRow, N4EvidenceRow, N4InstrumentRow, N4MeasurementRow
from .models import (AdjudicationRequest, AoiInference, EvidenceFile, LabReportManifest, LiveCaptureRequest,
                     N4Adjudication, N4CalibrationRecord, N4EvidenceIntake, N4Instrument, N4Measurement, ScpiLog)

FileIn = tuple[str, Optional[str], bytes]          # (name, declared content type, bytes)


class EvidenceIntakeMixin:
    # ------------------------------------------------------------------ shared steps
    def _evidence_source(self, s, tenant: str, source_id: str) -> str:
        src = Repository(s).get_source(tenant, source_id)
        if src is None or src.approval_status != "APPROVED":
            raise N0Error(409, "EVIDENCE_SOURCE_NOT_APPROVED", f"{source_id} is "
                                                               f"{src.approval_status if src else 'not registered'}")
        if "VERIFICATION" not in src.permitted_use:
            raise N0Error(409, "EVIDENCE_SOURCE_PURPOSE", f"{source_id} does not permit VERIFICATION")
        return src.data_origin

    def _screen(self, files: list[FileIn], codes: list[str], problems: list[str]) -> dict[str, tuple[bytes, str]]:
        if len(files) > self.settings.max_evidence_files:
            raise N0Error(413, "TOO_MANY_FILES", f"at most {self.settings.max_evidence_files} files per submission")
        out = {}
        for raw, ctype, data in files:
            name = (raw or "").replace("\\", "/").lstrip("/")
            if not name or ".." in name.split("/") or name in out:
                codes.append("UPLOAD_REJECTED")
                problems.append(f"{raw!r} is not an acceptable / unique file name")
                continue
            screen = validate_upload(data, posixpath.basename(name), ctype, self.n0_settings.max_upload_bytes)
            if not screen.ok:
                codes.append("UPLOAD_REJECTED")
                problems.append(f"{name}: {'; '.join(screen.problems)}")
                continue
            scan = self.scanner.scan(data)
            if not scan.clean:
                codes.append("MALWARE_DETECTED")
                problems.append(f"{name}: {scan.engine} flagged {scan.signature}")
                continue
            out[name] = (data, screen.sniffed_mime)
        return out

    @staticmethod
    def _derive(claimed: str, origin: str, physical: str, simulated: bool, mock: bool, codes: list[str],
                problems: list[str]) -> str:
        if claimed == "SYNTHETIC":
            return "SYNTHETIC"
        why = []
        if origin != "REAL":
            why.append(f"the evidence source is {origin}")
        if simulated:
            why.append("the instrument identifies itself as simulated / virtual")
        if mock:
            why.append("the file is marked as a mock fixture")
        if why:
            codes.append("SYNTHETIC_AS_PHYSICAL")
            problems.append("claimed PHYSICAL evidence, but " + "; ".join(why))
            return "MOCK_FIXTURE" if mock else "SYNTHETIC"
        return physical

    def _test_cases(self, plan, test_ids: set[str], method: str, codes: list[str], problems: list[str]) -> None:
        cases = {tc.test_id: tc for tc in plan.test_cases}
        for tid in sorted(test_ids):
            tc = cases.get(tid)
            if tc is None:
                codes.append("UNKNOWN_TEST")
                problems.append(f"{tid} is not a test case of plan {plan.plan_id}")
            elif tc.method != method:
                codes.append("METHOD_ADAPTER_MISMATCH")
                problems.append(f"{tid} is a {tc.method} test; this adapter delivers {method} evidence")

    def _persist(self, p: Principal, plan_id: str, adapter: str, source_id: str, claimed: Optional[str],
                 derived: Optional[str], files: dict[str, tuple[bytes, str]], roles: dict[str, str],
                 measurements: list[dict], codes: list[str], problems: list[str], metadata: dict[str, Any],
                 detections: Optional[list] = None) -> N4EvidenceIntake:
        t = p.tenant_id
        eid = new_id("N4EV")
        status = "REJECTED" if codes else "ACCEPTED"
        evfiles: list[EvidenceFile] = []
        with self.db.session() as s:
            repo = Repository(s)
            for name, (data, mime) in files.items():
                digest = sha256_hex(data)
                if status == "ACCEPTED":
                    blob = self._store_bytes(repo, t, source_id, name, data, mime, p.principal_id,
                                             {"engine": self.scanner.name, "clean": True, "evidence_id": eid})
                    evfiles.append(EvidenceFile(path=name, role=roles.get(name, "OTHER"), sha256=digest,
                                                blob_id=blob.blob_id, uri=make_uri(blob.bucket, blob.object_key),
                                                size_bytes=len(data), mime=mime))
                    repo.add_edge(t, f"blob:{blob.blob_id}", f"n4_evidence:{eid}", "EVIDENCE_FOR", role=roles.get(name))
                else:
                    evfiles.append(EvidenceFile(path=name, role=roles.get(name, "OTHER"), sha256=digest,
                                                blob_id="not-stored", uri="not-stored", size_bytes=len(data), mime=mime))
            mids = []
            if status == "ACCEPTED":
                for m in measurements:
                    mm = N4Measurement(measurement_id=new_id("N4M"), tenant_id=t, plan_id=plan_id, evidence_id=eid,
                                       provenance=derived, **m)
                    mids.append(mm.measurement_id)
                    s.add(N4MeasurementRow(measurement_id=mm.measurement_id, tenant_id=t, plan_id=plan_id,
                                           evidence_id=eid, test_id=mm.test_id, record=dump(mm)))
            ev = N4EvidenceIntake(
                evidence_id=eid, tenant_id=t, plan_id=plan_id, adapter=adapter, source_id=source_id,
                submitted_by=p.principal_id, submitted_at=self.clock(), claimed_provenance=claimed,
                derived_provenance=derived, files=evfiles, measurement_ids=mids, detections=detections or [],
                metadata=metadata, evidence_sha256=sha256_hex("\n".join(sorted(f.sha256 for f in evfiles)).encode()),
                status=status, rejection_codes=sorted(set(codes)), problems=problems)
            s.add(N4EvidenceRow(evidence_id=eid, tenant_id=t, plan_id=plan_id, adapter=adapter, status=status,
                                record=dump(ev)))
            repo.add_edge(t, f"n4_plan:{plan_id}", f"n4_evidence:{eid}", "EVIDENCE", status=status, adapter=adapter)
            self._audit(s, p, f"N4_EVIDENCE_{status}", f"n4_evidence:{eid}",
                        {"adapter": adapter, "codes": ev.rejection_codes, "derived_provenance": derived,
                         "files": len(evfiles), "measurements": len(mids)})
        return ev

    @staticmethod
    def _instrument_by_serial(reg, serial: str) -> Optional[N4Instrument]:
        return next((i for i in reg.instruments.values() if i.serial == serial), None)

    @staticmethod
    def _cal_belongs(reg, cal_id: str, inst: Optional[N4Instrument], codes: list[str], problems: list[str]) -> None:
        cal: Optional[N4CalibrationRecord] = reg.calibrations.get(cal_id)
        if inst is not None and (cal is None or cal.instrument_id != inst.instrument_id):
            codes.append("CALIBRATION_MISMATCH")
            problems.append(f"calibration {cal_id} is not a calibration record of {inst.instrument_id}")

    # ------------------------------------------------------------------ SCPI / VISA
    def ingest_scpi(self, p: Principal, plan_id: str, log_name: str, files: list[FileIn], source_id: str
                    ) -> N4EvidenceIntake:
        plan, intake = self._active_plan(p.tenant_id, plan_id)
        codes: list[str] = []
        problems: list[str] = []
        with self.db.session() as s:
            origin = self._evidence_source(s, p.tenant_id, source_id)
            reg = self._registry(s, p.tenant_id)
        screened = self._screen(files, codes, problems)
        if log_name not in screened:
            raise N0Error(422, "LOG_MISSING", f"the SCPI log {log_name!r} was not uploaded or was rejected: "
                                              f"{'; '.join(problems)}")
        try:
            log = ScpiLog.model_validate_json(screened[log_name][0])
        except ValidationError as exc:
            raise N0Error(422, "LOG_INVALID", str(exc)[:2000]) from None
        log_sha = sha256_hex(screened[log_name][0])
        att = {a.path: a for a in log.attachments}
        for name, (data, _) in screened.items():
            if name == log_name:
                continue
            if name not in att:
                codes.append("UNDECLARED_FILE")
                problems.append(f"{name} is not an attachment of the log")
            elif sha256_hex(data) != att[name].sha256:
                codes.append("SHA256_MISMATCH")
                problems.append(f"attachment {name} does not match its SHA-256 in the log")
        for missing in sorted(set(att) - set(screened)):
            codes.append("MISSING_FILE")
            problems.append(f"attachment {missing} was not uploaded")
        serial = log.instrument.idn.split(",")[2].strip() if log.instrument.idn.count(",") >= 2 else ""
        inst = self._instrument_by_serial(reg, serial)
        idn, scpi_problems = scpi.check_log(log, inst)
        for code, msg in scpi_problems:
            codes.append(code)
            problems.append(msg)
        self._cal_belongs(reg, log.instrument.calibration_id, inst, codes, problems)
        self._test_cases(plan, {log.test_id}, "BENCH_ATE", codes, problems)
        physical = "PHYSICAL_BENCH" if log.capture == "LIVE_DRIVER" else "PHYSICAL_ATE"
        derived = self._derive(log.claimed_provenance, origin, physical, bool(idn) and scpi.looks_simulated(idn),
                               False, codes, problems)
        ms = []
        for r in log.records:
            try:
                value = scpi.parse_number(r.response)
            except scpi.ScpiError:
                continue
            ms.append({"test_id": log.test_id, "characteristic": r.characteristic, "unit_serial": log.unit_serial,
                       "value": value, "unit": r.unit, "expanded_uncertainty": r.expanded_uncertainty,
                       "coverage_factor": r.k, "instrument_id": inst.instrument_id if inst else None,
                       "instrument_serial": idn.get("serial"), "instrument_firmware": idn.get("firmware"),
                       "calibration_id": log.instrument.calibration_id, "captured_at": r.t, "method": "BENCH_ATE",
                       "method_ref": log.method_ref, "operator": log.operator, "raw_sha256": log_sha,
                       "source_command": r.command})
        roles = {name: ("RAW_LOG" if name == log_name else att[name].role if name in att else "OTHER")
                 for name in screened}
        return self._persist(p, plan_id, "SCPI_LIVE" if log.capture == "LIVE_DRIVER" else "SCPI_VISA", source_id,
                             log.claimed_provenance, derived, screened, roles, ms, codes, problems,
                             {"test_ids": [log.test_id], "station_id": log.station_id, "unit_serial": log.unit_serial,
                              "idn": idn, "capture": log.capture})

    def capture_live(self, p: Principal, plan_id: str, req: LiveCaptureRequest) -> N4EvidenceIntake:
        self._active_plan(p.tenant_id, plan_id)
        with self.db.session() as s:
            row = s.get(N4InstrumentRow, (req.instrument_id, p.tenant_id))
            reg = self._registry(s, p.tenant_id)
        if row is None:
            raise N0Error(404, "INSTRUMENT_NOT_FOUND", f"{req.instrument_id} is not registered")
        inst = N4Instrument.model_validate(row.record)
        cal = max((c for c in reg.calibrations.values() if c.instrument_id == inst.instrument_id and c.status == "VALID"),
                  key=lambda c: c.calibration_due, default=None)
        try:
            transport = scpi.open_transport(inst, self.settings.scpi_allowed_hosts, self.settings.scpi_timeout_s)
            try:
                idn, records = scpi.capture(transport, req.steps, clock=lambda: datetime.now(timezone.utc))
            finally:
                transport.close()
        except scpi.ScpiError as exc:
            status = 403 if exc.code in ("HOST_NOT_ALLOWED", "COMMAND_NOT_ALLOWED") else 502
            raise N0Error(status, exc.code, exc.message) from None
        except OSError as exc:
            raise N0Error(502, "INSTRUMENT_UNREACHABLE", str(exc)) from None
        log = {"format": "athena-scpi-log/1", "station_id": req.station_id, "operator": p.principal_id,
               "test_id": req.test_id, "unit_serial": req.unit_serial,
               "method_ref": req.method_ref.model_dump() if req.method_ref else None,
               "instrument": {"resource": inst.resource, "idn": idn,
                              "calibration_id": cal.calibration_id if cal else "NONE"},
               "records": records, "attachments": [], "claimed_provenance": "PHYSICAL", "capture": "LIVE_DRIVER"}
        name = f"scpi_live_{req.test_id}_{records[0]['t'].replace(':', '').replace('-', '')}.json"
        return self.ingest_scpi(p, plan_id, name, [(name, "application/json", canonical_json(log))], req.source_id)

    # ------------------------------------------------------------------ accredited lab
    def ingest_lab(self, p: Principal, plan_id: str, manifest_json: str, files: list[FileIn], source_id: str
                   ) -> N4EvidenceIntake:
        plan, intake = self._active_plan(p.tenant_id, plan_id)
        codes: list[str] = []
        problems: list[str] = []
        try:
            manifest = LabReportManifest.model_validate_json(manifest_json)
        except ValidationError as exc:
            raise N0Error(422, "MANIFEST_INVALID", str(exc)[:2000]) from None
        with self.db.session() as s:
            origin = self._evidence_source(s, p.tenant_id, source_id)
            labs = self._labs(s, p.tenant_id)
        mbytes = manifest_json.encode()
        screened = self._screen(files + [("lab_manifest.json", "application/json", mbytes)], codes, problems)
        pdfs = [n for n, (_, mime) in screened.items() if mime == "application/pdf"]
        if len(pdfs) != 1:
            raise N0Error(422, "REPORT_PDF_REQUIRED", f"exactly one lab report PDF is required, got {len(pdfs)}")
        pdf, pdf_bytes = pdfs[0], screened[pdfs[0]][0]
        try:
            text = self.docling.parse(pdf_bytes, "application/pdf", pdf).text
        except ParseError as exc:
            codes.append("PARSE_FAILED")
            problems.append(f"{pdf}: {exc}")
            text = ""
        for code, msg in lab_report.check(manifest, sha256_hex(pdf_bytes), text, intake, labs):
            codes.append(code)
            problems.append(msg)
        self._test_cases(plan, {r.test_id for r in manifest.results}, "ACCREDITED_LAB", codes, problems)
        derived = self._derive(manifest.claimed_provenance, origin, "ACCREDITED_LAB", False, False, codes, problems)
        ms = [{"test_id": r.test_id, "characteristic": r.characteristic, "unit_serial": r.unit_serial,
               "value": r.value, "unit": r.unit, "expanded_uncertainty": r.expanded_uncertainty,
               "coverage_factor": r.k, "instrument_id": r.instrument_id, "calibration_id": r.calibration_id,
               "calibration_due": r.calibration_due, "calibration_source": "ACCREDITED_LAB",
               "captured_at": r.captured_at, "method": "ACCREDITED_LAB", "method_ref": r.method_ref,
               "operator": manifest.lab.name, "raw_sha256": sha256_hex(pdf_bytes)} for r in manifest.results]
        roles = {n: ("LAB_REPORT" if n == pdf else "MANIFEST" if n == "lab_manifest.json" else "OTHER")
                 for n in screened}
        return self._persist(p, plan_id, "ACCREDITED_LAB", source_id, manifest.claimed_provenance, derived, screened,
                             roles, ms, codes, problems,
                             {"test_ids": sorted({r.test_id for r in manifest.results}),
                              "certificate_number": manifest.certificate_number, "lab": manifest.lab.name,
                              "accreditation_number": manifest.lab.accreditation_number,
                              "pcb_revision": manifest.pcb_revision, "report_sha256": manifest.report_sha256})

    # ------------------------------------------------------------------ AOI / vision
    def ingest_aoi(self, p: Principal, plan_id: str, inference_json: str, files: list[FileIn], source_id: str
                   ) -> N4EvidenceIntake:
        plan, intake = self._active_plan(p.tenant_id, plan_id)
        codes: list[str] = []
        problems: list[str] = []
        try:
            inf = AoiInference.model_validate_json(inference_json)
        except ValidationError as exc:
            raise N0Error(422, "INFERENCE_INVALID", str(exc)[:2000]) from None
        with self.db.session() as s:
            origin = self._evidence_source(s, p.tenant_id, source_id)
            reg = self._registry(s, p.tenant_id)
        screened = self._screen(files + [("aoi_inference.json", "application/json", inference_json.encode())], codes,
                                problems)
        images = {n: d for n, (d, _) in screened.items() if n != "aoi_inference.json"}
        for code, msg in aoi.check(inf, images, intake):
            codes.append(code)
            problems.append(msg)
        if inf.instrument_id not in reg.instruments:
            codes.append("INSTRUMENT_UNREGISTERED")
            problems.append(f"AOI station {inf.instrument_id} is not registered")
        self._test_cases(plan, {inf.test_id}, "INSPECTION", codes, problems)
        mock = any(x in inf.model.name.lower() for x in ("mock", "fake", "dummy"))
        derived = self._derive(inf.claimed_provenance, origin, "PHYSICAL_INSPECTION", False, mock, codes, problems)
        dets = aoi.triage(inf, self.settings.aoi_flag_threshold, self.settings.aoi_ambiguous_threshold)
        roles = {n: ("MANIFEST" if n == "aoi_inference.json" else "IMAGE") for n in screened}
        return self._persist(p, plan_id, "AOI_VISION", source_id, inf.claimed_provenance, derived, screened, roles, [],
                             codes, problems,
                             {"test_ids": [inf.test_id], "test_id": inf.test_id, "unit_serial": inf.unit_serial,
                              "pcb_revision": inf.pcb_revision, "model": inf.model.model_dump(),
                              "vision_release_decisions": aoi.VISION_RELEASE_DECISIONS}, dets)

    def adjudicate(self, p: Principal, req: AdjudicationRequest) -> N4Adjudication:
        with self.db.session() as s:
            row = s.get(N4EvidenceRow, req.evidence_id)
            if row is None or row.tenant_id != p.tenant_id:
                raise N0Error(404, "EVIDENCE_NOT_FOUND", f"{req.evidence_id} not found")
            ev = N4EvidenceIntake.model_validate(row.record)
        self._active_plan(p.tenant_id, ev.plan_id)
        if ev.adapter != "AOI_VISION" or ev.status != "ACCEPTED":
            raise N0Error(409, "NOT_ADJUDICABLE", f"{req.evidence_id} is not accepted AOI evidence")
        if p.principal_id == ev.submitted_by:
            raise N0Error(403, "SEPARATION_OF_DUTIES", "the importer of AOI evidence cannot adjudicate it")
        known = {d.detection_id for d in ev.detections}
        bad = [d.detection_id for d in req.decisions if d.detection_id not in known]
        if bad:
            raise N0Error(422, "UNKNOWN_DETECTION", f"{bad} are not detections of {req.evidence_id}")
        a = N4Adjudication(adjudication_id=new_id("N4ADJ"), tenant_id=p.tenant_id, evidence_id=req.evidence_id,
                           adjudicator=p.principal_id, decisions=req.decisions, unit_verdict=req.unit_verdict,
                           notes=req.notes, independent=True, created_at=self.clock())
        with self.db.session() as s:
            s.add(N4AdjudicationRow(adjudication_id=a.adjudication_id, tenant_id=p.tenant_id,
                                    evidence_id=req.evidence_id, record=dump(a)))
            self._audit(s, p, "N4_AOI_ADJUDICATED", f"n4_evidence:{req.evidence_id}",
                        {"unit_verdict": a.unit_verdict, "labels": len(a.decisions)})
        return a

    def training_labels(self, tenant: str) -> dict:
        """Only human adjudications; model-only labels are never exported."""
        with self.db.session() as s:
            evs = {r.evidence_id: N4EvidenceIntake.model_validate(r.record) for r in s.scalars(
                select(N4EvidenceRow).where(N4EvidenceRow.tenant_id == tenant, N4EvidenceRow.adapter == "AOI_VISION",
                                            N4EvidenceRow.status == "ACCEPTED"))}
            adjs = [N4Adjudication.model_validate(r.record) for r in s.scalars(
                select(N4AdjudicationRow).where(N4AdjudicationRow.tenant_id == tenant))]
        labels: dict[str, dict] = {}
        for a in adjs:
            ev = evs.get(a.evidence_id)
            if ev is None or ev.derived_provenance in ("SYNTHETIC", "MOCK_FIXTURE"):
                continue
            dets = {d.detection_id: d for d in ev.detections}
            for dec in a.decisions:
                d = dets[dec.detection_id]
                e = labels.setdefault(dec.detection_id, {"detection_id": d.detection_id, "image_sha256": d.image_sha256,
                                                         "model_class": d.cls, "bbox_xyxy": d.bbox_xyxy, "labels": {}})
                e["labels"][a.adjudicator] = dec.label
        out = []
        for e in labels.values():
            votes = set(e["labels"].values())
            e["double_labeled"] = len(e["labels"]) >= 2 and len(votes) == 1
            e["label"] = votes.pop() if len(votes) == 1 else "DISAGREEMENT"
            e["training_eligible"] = e["double_labeled"]
            out.append(e)
        unreviewed = sum(1 for ev in evs.values() for d in ev.detections if d.detection_id not in labels)
        return {"labels": sorted(out, key=lambda x: x["detection_id"]), "model_only_labels_excluded": unreviewed,
                "policy": "model labels never enter training; only double-labeled, agreeing human adjudications are "
                          "training-eligible"}

    # ------------------------------------------------------------------ offline CSV
    def ingest_csv(self, p: Principal, plan_id: str, files: list[FileIn], source_id: str) -> N4EvidenceIntake:
        plan, intake = self._active_plan(p.tenant_id, plan_id)
        codes: list[str] = []
        problems: list[str] = []
        with self.db.session() as s:
            origin = self._evidence_source(s, p.tenant_id, source_id)
            reg = self._registry(s, p.tenant_id)
        screened = self._screen(files, codes, problems)
        csvs = [n for n, (_, mime) in screened.items() if mime == "text/csv"]
        if len(csvs) != 1:
            raise N0Error(422, "CSV_REQUIRED", f"exactly one CSV file is required, got {len(csvs)}")
        rows, mock, csv_problems = csv_offline.parse(screened[csvs[0]][0])
        for code, msg in csv_problems:
            codes.append(code)
            problems.append(msg)
        cases = {tc.test_id: tc for tc in plan.test_cases}
        methods = {cases[r["test_id"]].method for r in rows if r["test_id"] in cases}
        for r in rows:
            tc = cases.get(r["test_id"])
            if tc is None:
                codes.append("UNKNOWN_TEST")
                problems.append(f"{r['test_id']} is not a test case of plan {plan_id}")
            elif tc.method not in ("BENCH_ATE", "INSPECTION"):
                codes.append("METHOD_ADAPTER_MISMATCH")
                problems.append(f"{r['test_id']} is {tc.method}; use the lab-report adapter for accredited lab "
                                "results and N3 evidence for SIM")
            inst = reg.instruments.get(r["instrument_id"])
            if inst is None:
                codes.append("INSTRUMENT_UNREGISTERED")
                problems.append(f"{r['instrument_id']} is not registered")
            self._cal_belongs(reg, r["calibration_id"], inst, codes, problems)
        physical = "PHYSICAL_INSPECTION" if methods == {"INSPECTION"} else "PHYSICAL_BENCH"
        if len(methods) > 1:
            codes.append("MIXED_METHODS")
            problems.append("one CSV must hold either bench or inspection results, not both")
        derived = self._derive("PHYSICAL", origin, physical, False, mock, codes, problems) if not mock else "MOCK_FIXTURE"
        if mock and origin == "REAL":
            problems.append("mock fixture: imported as MOCK_FIXTURE, never physical evidence")
        sha = sha256_hex(screened[csvs[0]][0])
        ms = [{**{k: r[k] for k in ("test_id", "characteristic", "unit_serial", "value", "unit", "expanded_uncertainty",
                                    "instrument_id", "calibration_id", "captured_at", "method_ref", "operator")},
               "coverage_factor": r["k"], "method": cases[r["test_id"]].method if r["test_id"] in cases else "BENCH_ATE",
               "instrument_serial": reg.instruments[r["instrument_id"]].serial
               if r["instrument_id"] in reg.instruments else None, "raw_sha256": sha} for r in rows]
        roles = {n: ("RAW_LOG" if n == csvs[0] else "OTHER") for n in screened}
        return self._persist(p, plan_id, "CSV_OFFLINE", source_id, "MOCK_FIXTURE" if mock else "PHYSICAL", derived,
                             screened, roles, ms, codes, problems,
                             {"test_ids": sorted({r["test_id"] for r in rows}), "mock_fixture": mock})

    def evidence(self, tenant: str, evidence_id: str) -> N4EvidenceIntake:
        with self.db.session() as s:
            row = s.get(N4EvidenceRow, evidence_id)
            if row is None or row.tenant_id != tenant:
                raise N0Error(404, "EVIDENCE_NOT_FOUND", f"{evidence_id} not found")
            return N4EvidenceIntake.model_validate(row.record)


__all__ = ["EvidenceIntakeMixin"]
