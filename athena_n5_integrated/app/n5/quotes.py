"""N5.3 quotation intake: manual ``athena-quote/1``, the EMS / ODM signed drop zone, and on-demand
distributor lookups (DigiKey, Mouser, Nexar).

Structural problems (bad price breaks, unit of measure, validity window, file hashes, signature,
malware) reject the quote at intake (422, recorded with its hashes). Business guardrails (supplier
approval, expiry, exact MPN, design revision, stock, lead time, capacity, rights) are applied later by
the deterministic engine, so the same quote can be re-assessed when the world changes.
"""
from __future__ import annotations

import json
import posixpath
import re
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from pydantic import ValidationError
from sqlalchemy import select

from ..errors import N0Error
from ..repository import Repository, dump, new_id
from ..rules import canonical_json, sha256_hex
from ..scanning import validate_upload
from ..security import Principal
from .connectors.base import ConnectorError
from .connectors.ems_dropzone import offer_csv, parse_x12_843, verify_signature
from .db import N5ConnectorCallRow, N5QuoteRow
from .models import (ConnectorProvenance, EmsQuoteManifest, LookupRequest, N5ConnectorCall, N5Offer, N5Quote,
                     OfferIn, QuoteFile, QuoteRequest)

FileIn = tuple[str, Optional[str], bytes]


def structural_problems(offers: list[OfferIn], issued_at: datetime, valid_until: datetime) -> list[tuple[str, str]]:
    out = []
    if valid_until <= issued_at:
        out.append(("VALIDITY_INVALID", f"valid_until {valid_until.isoformat()} is not after issued_at"))
    for i, o in enumerate(offers):
        qs = [b.min_qty for b in o.price_breaks]
        if qs != sorted(set(qs)):
            out.append(("PRICE_BREAKS_INVALID", f"offer {i}: break quantities must be unique and ascending, got {qs}"))
        if o.uom.upper() != "EA":
            out.append(("UOM_MISMATCH", f"offer {i}: priced per {o.uom!r}; the BOM is counted in EA (re-quote per EA)"))
        if o.availability == "STOCK" and o.stock_qty is None:
            out.append(("STOCK_MISSING", f"offer {i}: a STOCK offer must state the stock quantity"))
    return out


class QuoteIntakeMixin:
    # ------------------------------------------------------------------ shared
    def _quote_source(self, s, tenant: str, source_id: str) -> str:
        src = Repository(s).get_source(tenant, source_id)
        if src is None or src.approval_status != "APPROVED":
            raise N0Error(409, "QUOTE_SOURCE_NOT_APPROVED", f"{source_id} is "
                                                            f"{src.approval_status if src else 'not registered'}")
        if "SOURCING" not in src.permitted_use:
            raise N0Error(409, "QUOTE_SOURCE_PURPOSE", f"{source_id} does not permit SOURCING")
        return src.data_origin

    def _persist_quote(self, p: Principal, q: N5Quote) -> N5Quote:
        body = {k: v for k, v in dump(q).items() if k not in ("quote_sha256", "submitted_at")}
        q = q.model_copy(update={"quote_sha256": sha256_hex(canonical_json(body))})
        with self.db.session() as s:
            s.add(N5QuoteRow(quote_id=q.quote_id, tenant_id=p.tenant_id, bom_intake_id=q.bom_intake_id,
                             supplier_id=q.supplier_id, source_id=q.source_id, status=q.status, record=dump(q)))
            self._event(s, p.tenant_id, "quote", q.quote_id, q.status, p.principal_id, codes=q.rejection_codes)
            repo = Repository(s)
            repo.add_edge(p.tenant_id, f"source:{q.source_id}", f"n5_quote:{q.quote_id}", "QUOTED_VIA",
                          channel=q.channel)
            repo.add_edge(p.tenant_id, f"n5_quote:{q.quote_id}", f"n5_intake:{q.bom_intake_id}", "QUOTES_FOR",
                          sha256=q.quote_sha256)
            self._audit(s, p, f"N5_QUOTE_{q.status}", f"n5_quote:{q.quote_id}",
                        {"supplier_id": q.supplier_id, "channel": q.channel, "quote_sha256": q.quote_sha256,
                         "codes": q.rejection_codes})
        return q

    @staticmethod
    def _offers(quote_id: str, offers: list[dict]) -> list[N5Offer]:
        return [N5Offer.model_validate({**o, "offer_id": f"{quote_id}-O{i + 1}", "quote_id": quote_id})
                for i, o in enumerate(offers)]

    def _intake_for_quote(self, tenant: str, intake_id: str):
        with self.db.session() as s:
            return self._intake(s, tenant, intake_id)

    def _supplier_or_404(self, tenant: str, supplier_id: str):
        sup = self.suppliers(tenant).get(supplier_id)
        if sup is None:
            raise N0Error(404, "SUPPLIER_NOT_FOUND", f"{supplier_id} is not registered in N5")
        return sup

    # ------------------------------------------------------------------ manual quotes
    def submit_quote(self, p: Principal, req: QuoteRequest) -> N5Quote:
        intake = self._intake_for_quote(p.tenant_id, req.bom_intake_id)
        self._supplier_or_404(p.tenant_id, req.supplier_id)
        with self.db.session() as s:
            origin = self._quote_source(s, p.tenant_id, req.source_id)
        problems = structural_problems(req.offers, req.issued_at, req.valid_until)
        qid = new_id("N5Q")
        q = N5Quote(quote_id=qid, tenant_id=p.tenant_id, bom_intake_id=intake.intake_id, supplier_id=req.supplier_id,
                    channel="MANUAL", source_id=req.source_id, quote_ref=req.quote_ref,
                    design_revision=req.design_revision, issued_at=req.issued_at, valid_until=req.valid_until,
                    offers=self._offers(qid, [o.model_dump(mode="json") for o in req.offers]),
                    synthetic=origin != "REAL", status="REJECTED" if problems else "ACCEPTED",
                    rejection_codes=sorted({c for c, _ in problems}), problems=[m for _, m in problems],
                    submitted_by=p.principal_id, submitted_at=self.clock())
        return self._persist_quote(p, q)

    # ------------------------------------------------------------------ EMS / ODM signed drop zone
    def submit_ems_quote(self, p: Principal, manifest_raw: str, source_id: str, files: list[FileIn]) -> N5Quote:
        try:
            m = EmsQuoteManifest.model_validate_json(manifest_raw)
        except ValidationError as exc:
            raise N0Error(422, "MANIFEST_INVALID", str(exc)[:1500]) from None
        intake = self._intake_for_quote(p.tenant_id, m.bom_intake_id)
        sup = self._supplier_or_404(p.tenant_id, m.supplier_id)
        if len(files) > self.settings.max_quote_files:
            raise N0Error(413, "TOO_MANY_FILES", f"at most {self.settings.max_quote_files} files per quotation")
        with self.db.session() as s:
            origin = self._quote_source(s, p.tenant_id, source_id)
        codes: list[str] = []
        problems: list[str] = []
        screened: dict[str, tuple[bytes, str]] = {}
        for raw_name, ctype, data in files:
            name = (raw_name or "").replace("\\", "/").lstrip("/")
            if not name or ".." in name.split("/") or name in screened:
                codes.append("UPLOAD_REJECTED")
                problems.append(f"{raw_name!r} is not an acceptable / unique file name")
                continue
            scr = validate_upload(data, posixpath.basename(name), ctype, self.n0_settings.max_upload_bytes)
            if not scr.ok:
                codes.append("UPLOAD_REJECTED")
                problems.append(f"{name}: {'; '.join(scr.problems)}")
                continue
            scan = self.scanner.scan(data)
            if not scan.clean:
                codes.append("MALWARE_DETECTED")
                problems.append(f"{name}: {scan.engine} flagged {scan.signature}")
                continue
            screened[name] = (data, scr.sniffed_mime)
        declared = {m.quote_document: ("QUOTE_DOCUMENT", m.quote_document_sha256)}
        for a in m.attachments:
            declared[a.get("path", "")] = (a.get("role", "OTHER"), a.get("sha256", ""))
        for name, (role, sha) in declared.items():
            if name not in screened:
                if "UPLOAD_REJECTED" not in codes and "MALWARE_DETECTED" not in codes:
                    codes.append("FILE_MISSING")
                    problems.append(f"{name} is listed in the manifest but was not uploaded")
            elif sha256_hex(screened[name][0]) != sha:
                codes.append("SHA256_MISMATCH")
                problems.append(f"{name}: SHA-256 differs from the manifest")
        undeclared = sorted(set(screened) - set(declared))
        if undeclared:
            codes.append("FILE_UNDECLARED")
            problems.append(f"uploaded files not in the manifest: {undeclared}")
        doc = screened.get(m.quote_document, (None, None))[0]
        key = next((k for k in sup.signing_keys if k.key_id == m.signature.get("key_id")), None)
        if key is None or key.algorithm != m.signature.get("algorithm"):
            codes.append("SIGNATURE_INVALID")
            problems.append(f"signing key {m.signature.get('key_id')!r} is not registered for {sup.supplier_id}")
        elif doc is not None and not verify_signature(key, doc, m.signature.get("value", "")):
            codes.append("SIGNATURE_INVALID")
            problems.append(f"the signature over {m.quote_document} does not verify with key {key.key_id}")
        offers = [o.model_dump(mode="json") for o in m.offers]
        valid_until = m.valid_until
        if not offers and doc is not None:
            text_head = doc[:64].lstrip()
            if text_head.startswith(b"ISA") or b"ST*843" in doc[:400]:
                header, offers, x12_problems = parse_x12_843(doc)
                problems += x12_problems
                if x12_problems:
                    codes.append("EDI_INVALID")
                if header.get("quote_ref") and header["quote_ref"] != m.quote_ref:
                    codes.append("EDI_INVALID")
                    problems.append(f"EDI quote ref {header['quote_ref']} ≠ manifest {m.quote_ref}")
                valid_until = header.get("valid_until") or valid_until
            elif screened[m.quote_document][1] == "text/csv":
                offers, csv_problems = offer_csv(doc)
                if csv_problems:
                    codes.append("CSV_INVALID")
                    problems += csv_problems
        if not offers and not codes:
            codes.append("NO_OFFERS")
            problems.append("the quotation carries no offers (manifest, CSV or X12 843)")
        cap = m.capacity
        if cap is not None and cap.document_sha256 and cap.document_sha256 not in \
                {sha256_hex(d) for n, (d, _) in screened.items() if n != m.quote_document}:
            codes.append("CAPACITY_EVIDENCE_UNVERIFIED")
            problems.append("capacity allocation document_sha256 does not match an uploaded attachment")
        for o in offers:
            if o.get("capacity") is None and cap is not None:
                o["capacity"] = cap.model_dump(mode="json")
        try:
            parsed = [OfferIn.model_validate(o) for o in offers]
        except ValidationError as exc:
            parsed = []
            codes.append("OFFER_INVALID")
            problems.append(str(exc)[:800])
        for c, msg in structural_problems(parsed, m.issued_at, valid_until):
            codes.append(c)
            problems.append(msg)
        qid = new_id("N5Q")
        qfiles: list[QuoteFile] = []
        if not codes:
            with self.db.session() as s:
                repo = Repository(s)
                for name, (data, mime) in screened.items():
                    role = declared.get(name, ("OTHER", ""))[0]
                    role = role if role in ("QUOTE_DOCUMENT", "MANIFEST", "CAPACITY_EVIDENCE", "SERVICE_TERMS", "EDI",
                                            "OTHER") else "OTHER"
                    self._store_bytes(repo, p.tenant_id, source_id, name, data, mime, p.principal_id,
                                      {"engine": self.scanner.name, "clean": True})
                    qfiles.append(QuoteFile(path=name, sha256=sha256_hex(data), role=role, size_bytes=len(data)))
        else:
            qfiles = [QuoteFile(path=n, sha256=sha256_hex(d), role=declared.get(n, ("OTHER", ""))[0]
                                if declared.get(n, ("OTHER", ""))[0] in ("QUOTE_DOCUMENT", "CAPACITY_EVIDENCE",
                                                                         "SERVICE_TERMS", "EDI") else "OTHER",
                                size_bytes=len(d)) for n, (d, _) in screened.items()]
        q = N5Quote(quote_id=qid, tenant_id=p.tenant_id, bom_intake_id=intake.intake_id, supplier_id=m.supplier_id,
                    channel="EMS_DROPZONE", source_id=source_id, quote_ref=m.quote_ref,
                    design_revision=m.design_revision, issued_at=m.issued_at, valid_until=valid_until,
                    offers=self._offers(qid, [o.model_dump(mode="json") for o in parsed]), nre=m.nre,
                    service_terms=m.service_terms, files=sorted(qfiles, key=lambda f: f.path),
                    signature={"key_id": m.signature.get("key_id"), "algorithm": m.signature.get("algorithm"),
                               "verified": "SIGNATURE_INVALID" not in codes},
                    synthetic=origin != "REAL", status="REJECTED" if codes else "ACCEPTED",
                    rejection_codes=sorted(set(codes)), problems=problems, submitted_by=p.principal_id,
                    submitted_at=self.clock())
        return self._persist_quote(p, q)

    # ------------------------------------------------------------------ distributor APIs (on demand)
    def _log_call(self, p: Principal, connector: str, operation: str, key: str, *, intake=None, line=None,
                  cache_hit=False, http_status=None, response_sha256=None, outcome="OK", error=None) -> N5ConnectorCall:
        call = N5ConnectorCall(call_id=new_id("N5C"), tenant_id=p.tenant_id, connector=connector, operation=operation,
                               project_id=intake.project_id if intake else None,
                               bom_intake_id=intake.intake_id if intake else None, line=line, key=key,
                               requested_by=p.principal_id, at=self.clock(), cache_hit=cache_hit,
                               http_status=http_status, response_sha256=response_sha256, outcome=outcome,
                               error=error)
        with self.db.session() as s:
            s.add(N5ConnectorCallRow(call_id=call.call_id, tenant_id=p.tenant_id, connector=connector,
                                     record=dump(call)))
        return call

    def _within_line_budget(self, tenant: str, intake_id: str, connector: str, line: int) -> bool:
        now = time.monotonic()
        window = max(self.settings.cache_ttl_s, 60)
        key = (tenant, intake_id, connector, line)
        hits = [t for t in self._line_calls.get(key, []) if now - t < window]
        if len(hits) >= self.settings.max_lookups_per_line:
            self._line_calls[key] = hits
            return False
        self._line_calls[key] = hits + [now]
        return True

    def _seller_supplier(self, tenant: str, seller: Optional[str], fallback: Optional[str]) -> str:
        from .engine import normalize_manufacturer
        if seller:
            want = normalize_manufacturer(seller)
            for sup in self.suppliers(tenant).values():
                if normalize_manufacturer(sup.legal_name) == want:
                    return sup.supplier_id
            return "UNREGISTERED-" + re.sub(r"[^A-Za-z0-9]+", "-", seller).strip("-")[:60]
        return fallback or "UNREGISTERED"

    def lookup(self, p: Principal, intake_id: str, req: LookupRequest) -> dict[str, Any]:
        with self.db.session() as s:
            intake = self._intake(s, p.tenant_id, intake_id)
        stale = self._stale_reason(p.tenant_id, intake)
        if stale:
            raise N0Error(409, "BOM_INTAKE_STALE", stale)
        lines = {ln.line: ln for ln in intake.lines}
        wanted = req.lines if req.lines is not None else sorted(lines)
        out_of_scope = [x for x in wanted if x not in lines]
        if out_of_scope:
            for x in out_of_scope:
                self._log_call(p, req.connectors[0], "lookup", f"line {x}", intake=intake, line=x,
                               outcome="OUT_OF_SCOPE", error="not a sourcing line of the active BOM intake")
            raise N0Error(403, "LOOKUP_OUT_OF_SCOPE", f"lines {out_of_scope} are not sourcing lines of {intake_id}; "
                                                      "lookups are limited to the active project BOM")
        if not self.settings.enable_live_apis:
            self._log_call(p, req.connectors[0], "lookup", ",".join(lines[x].mpn for x in wanted), intake=intake,
                           outcome="DISABLED", error="N5_ENABLE_LIVE_APIS is false")
            raise N0Error(409, "LIVE_APIS_DISABLED", "live distributor APIs are disabled (N5_ENABLE_LIVE_APIS=false)")
        origins: dict[str, str] = {}
        for name in dict.fromkeys(req.connectors):              # validate every connector before any call
            if not self.connectors[name].configured():
                raise N0Error(409, "CONNECTOR_NOT_CONFIGURED", f"{name} credentials are not configured")
            with self.db.session() as s:
                origins[name] = self._quote_source(s, p.tenant_id, self.settings.connector_sources[name])
        quotes: list[N5Quote] = []
        calls: list[N5ConnectorCall] = []
        for name, origin in origins.items():
            conn = self.connectors[name]
            source_id = self.settings.connector_sources[name]
            ttl = min(self.settings.cache_ttl_s, conn.policy.max_ttl_s)
            for x in wanted:
                ln = lines[x]
                ckey = (p.tenant_id, intake.project_id, name, ln.mpn)
                res = self.cache.get(ckey)
                if res is None:
                    if not self._within_line_budget(p.tenant_id, intake.intake_id, name, x):
                        calls.append(self._log_call(p, name, "lookup", ln.mpn_as_released, intake=intake, line=x,
                                                    outcome="RATE_LIMITED", error="per-line lookup budget used"))
                        continue
                    try:
                        self.limiters[name].acquire()
                        res = conn.lookup(ln.mpn_as_released)
                    except ConnectorError as exc:
                        calls.append(self._log_call(p, name, "lookup", ln.mpn_as_released, intake=intake, line=x,
                                                    http_status=exc.status,
                                                    outcome="RATE_LIMITED" if exc.code == "RATE_LIMITED" else "ERROR",
                                                    error=f"{exc.code}: {exc.message}"[:500]))
                        continue
                    self.cache.put(ckey, res, ttl)
                    hit = False
                else:
                    hit = True
                call = self._log_call(p, name, "lookup", ln.mpn_as_released, intake=intake, line=x, cache_hit=hit,
                                      http_status=res.http_status, response_sha256=res.response_sha256,
                                      outcome="OK" if res.offers else "NOT_FOUND")
                calls.append(call)
                by_seller: dict[str, list[dict]] = {}
                for o in res.offers:
                    sid = self._seller_supplier(p.tenant_id, o.get("seller"), req.supplier_ids.get(name))
                    by_seller.setdefault(sid, []).append(o)
                for sid, offers in sorted(by_seller.items()):
                    qid = new_id("N5Q")
                    lead = self.settings.distributor_stock_lead_days
                    clean = [{"line": x, "quoted_mpn": o["quoted_mpn"], "manufacturer": o.get("manufacturer"),
                              "currency": o["currency"], "price_breaks": o["price_breaks"], "moq": o["moq"],
                              "pack_multiple": o["pack_multiple"], "availability": "STOCK",
                              "stock_qty": o.get("stock_qty"), "lead_time_days": lead,
                              "line_charges": o.get("line_charges") or [], "supplier_sku": o.get("supplier_sku")}
                             for o in offers]
                    q = N5Quote(quote_id=qid, tenant_id=p.tenant_id, bom_intake_id=intake.intake_id, supplier_id=sid,
                                channel=f"{name}_API", source_id=source_id,
                                quote_ref=f"{name}:{ln.mpn_as_released}:{res.retrieved_at:%Y%m%dT%H%M%SZ}",
                                design_revision=intake.design_revision, issued_at=res.retrieved_at,
                                valid_until=res.retrieved_at + timedelta(hours=self.settings.api_offer_validity_h),
                                offers=self._offers(qid, json.loads(json.dumps(clean, default=str))),
                                provenance=ConnectorProvenance(connector=name, operation="lookup", call_id=call.call_id,
                                                               retrieved_at=res.retrieved_at,
                                                               response_sha256=res.response_sha256, cache_hit=hit,
                                                               terms=conn.policy.terms),
                                synthetic=origin != "REAL", status="ACCEPTED", submitted_by=p.principal_id,
                                submitted_at=self.clock())
                    quotes.append(self._persist_quote(p, q))
        return {"quotes": [dump(q) for q in quotes], "calls": [dump(c) for c in calls],
                "note": "Live distributor data: on-demand, scoped to this BOM, cached in memory only; never "
                        "bulk-downloaded, aggregated into a database or used for training."}

    # ------------------------------------------------------------------ vault re-hash
    def _verify_quote_files(self, tenant: str, quotes: list[N5Quote]) -> dict[str, str]:
        bad: dict[str, str] = {}
        with self.db.session() as s:
            repo = Repository(s)
            for q in quotes:
                for f in q.files:
                    blob = repo.find_blob(tenant, f.sha256, "UPLOAD")
                    try:
                        data = self.store.get(blob.bucket, blob.object_key) if blob else None
                    except Exception:      # noqa: BLE001 - a missing object is a tampering signal
                        data = None
                    if data is None or sha256_hex(data) != f.sha256:
                        bad[q.quote_id] = f"stored bytes of {f.path} no longer match SHA-256 {f.sha256[:12]}…"
                        break
        return bad

    def quotes_for(self, tenant: str, intake_id: str, accepted_only: bool = True) -> list[N5Quote]:
        with self.db.session() as s:
            rows = s.scalars(select(N5QuoteRow).where(N5QuoteRow.tenant_id == tenant,
                                                      N5QuoteRow.bom_intake_id == intake_id)
                             .order_by(N5QuoteRow.created_at, N5QuoteRow.quote_id))
            qs = [N5Quote.model_validate(r.record) for r in rows]
        return [q for q in qs if q.status == "ACCEPTED"] if accepted_only else qs
