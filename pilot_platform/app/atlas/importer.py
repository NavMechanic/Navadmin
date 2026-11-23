"""Vendor Atlas workbook import (VENDOR MASTER sheet).

Follows the reference ingestion rules for partner/batch files (ING-06, ING-10):
  * validate the template header; reject the whole file with row-level feedback on any bad row
  * record every import (filename, SHA-256, size, outcome); identical files are idempotent
  * upsert by atlas ID: source columns are replaced, ATHENA-managed state (outreach, due
    diligence, engagements) is kept; rows missing from a newer file are flagged, not deleted

The atlas is a candidate discovery database: nothing imported here is a qualified vendor,
and price text is stored verbatim — no number is ever derived from it.
"""
from __future__ import annotations

import hashlib
import io
import re

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.audit import canonical_json
from ..core.errors import ValidationFailed
from ..models import AtlasImport, AtlasVendor

SHEET = "VENDOR MASTER"
COLUMNS = {
    "ID": "atlas_id", "Vendor / Platform": "name", "Parent / Provisional Entity": "parent_entity",
    "Lifecycle segment": "lifecycle_segment", "ATHENA N1-N8 mapping": "node_mapping",
    "Vendor workflow deliverables": "vendor_deliverables", "ATHENA net output": "athena_net_output",
    "AI feature evidence / status": "ai_evidence", "Public base price / start terms": "price_terms",
    "Billing unit / pricing basis": "billing_basis", "Price evidence classification": "price_evidence",
    "Official price reference URL": "price_url", "Research / discovery URL": "research_url",
    "Research evidence category": "research_category", "ATHENA selection": "selection",
    "API/export accessibility": "api_access", "Commercial/IP rights": "rights_status",
    "Partner outreach status": "outreach_status_source", "Qualification notes": "qualification_notes",
}
SELECTIONS = {"Research", "Pilot"}
NUMERIC_PRICE = "Published numerical entry price"
DIRECTORY = "AI manufacturing directory"
MAX_ROWS = 20_000
_ID = re.compile(r"^AT-\d{4,}$")
_NODE = re.compile(r"^N([1-8])$")
_RANGE = re.compile(r"^N([1-8])\s*-\s*N([1-8])$")


def parse_nodes(raw: str) -> list[str]:
    nodes: list[str] = []
    for token in (t.strip() for t in str(raw or "").split(",")):
        if m := _RANGE.match(token):
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                raise ValueError(f"descending node range {token!r}")
            nodes.extend(f"N{i}" for i in range(lo, hi + 1))
        elif _NODE.match(token):
            nodes.append(token)
        else:
            raise ValueError(f"invalid node token {token!r}")
    return sorted(set(nodes), key=lambda n: int(n[1:]))


def _text(v) -> str:
    return "" if v is None else str(v).strip()


def parse_workbook(data: bytes, max_bytes: int) -> dict:
    """Return {"rows": [...], "errors": [...], "source_label": str}. Raises ValidationFailed if unusable."""
    if len(data) > max_bytes:
        raise ValidationFailed(f"file exceeds {max_bytes} bytes")
    if not data.startswith(b"PK\x03\x04"):
        raise ValidationFailed("not an .xlsx workbook (expected a zip container)")
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # corrupt zip, bad XML, unsupported content
        raise ValidationFailed(f"unreadable workbook: {type(exc).__name__}")
    if SHEET not in wb.sheetnames:
        raise ValidationFailed(f"sheet {SHEET!r} not found", sheets=wb.sheetnames)
    label = ""
    if "START HERE" in wb.sheetnames:
        top = [r for r in wb["START HERE"].iter_rows(min_row=1, max_row=3, values_only=True)]
        label = " | ".join(_text(r[0]) for r in top if r and r[0])
    rows_iter = wb[SHEET].iter_rows(values_only=True)
    header = None
    for r in rows_iter:
        if any(c not in (None, "") for c in r):
            header = [_text(c) for c in r]
            break
    if header is None:
        raise ValidationFailed("VENDOR MASTER is empty")
    missing = [h for h in COLUMNS if h not in header]
    if missing:
        raise ValidationFailed("template header mismatch", missing_columns=missing)
    index = {COLUMNS[h]: header.index(h) for h in COLUMNS}

    rows, errors, seen = [], [], set()
    for line_no, r in enumerate(rows_iter, start=2):
        if not any(c not in (None, "") for c in r):
            continue
        if len(rows) + len(errors) >= MAX_ROWS:
            raise ValidationFailed(f"more than {MAX_ROWS} rows")
        rec = {key: _text(r[i]) if i < len(r) else "" for key, i in index.items()}
        problems = []
        if not _ID.match(rec["atlas_id"]):
            problems.append(f"ID {rec['atlas_id']!r} is not AT-nnnn")
        elif rec["atlas_id"] in seen:
            problems.append(f"duplicate ID {rec['atlas_id']}")
        else:
            seen.add(rec["atlas_id"])  # even if the row has other errors, so duplicates surface in one pass
        if not rec["name"]:
            problems.append("Vendor / Platform is blank")
        if not rec["lifecycle_segment"]:
            problems.append("Lifecycle segment is blank")
        try:
            nodes = parse_nodes(rec["node_mapping"])
        except ValueError as exc:
            problems.append(str(exc))
            nodes = []
        if rec["selection"] not in SELECTIONS:
            problems.append(f"ATHENA selection must be one of {sorted(SELECTIONS)}")
        for url_key in ("price_url", "research_url"):
            if rec[url_key] and not rec[url_key].startswith(("https://", "http://")):
                problems.append(f"{url_key} must be an http(s) URL")
        if problems:
            errors.append({"row": line_no, "id": rec["atlas_id"], "problems": problems})
            continue
        rows.append({"row": line_no, "nodes": nodes, "source": rec})
    if not rows and not errors:
        raise ValidationFailed("VENDOR MASTER has no data rows")
    return {"rows": rows, "errors": errors, "source_label": label}


def import_atlas(db: Session, tenant_id: str, actor: str, data: bytes, filename: str, settings) -> tuple[AtlasImport, bool]:
    """Returns (import record, duplicate). A rejected file is recorded with its errors and changes nothing."""
    digest = hashlib.sha256(data).hexdigest()
    previous = db.scalars(select(AtlasImport).where(AtlasImport.tenant_id == tenant_id, AtlasImport.sha256 == digest,
                                                    AtlasImport.status == "applied")).first()
    if previous is not None:
        return previous, True
    parsed = parse_workbook(data, settings.atlas_max_bytes)
    imp = AtlasImport(tenant_id=tenant_id, filename=filename[:300], sha256=digest, size_bytes=len(data),
                      source_label=parsed["source_label"][:300], imported_by=actor, status="applied")
    if parsed["errors"]:
        imp.status = "rejected"
        imp.errors = parsed["errors"][:500]
        imp.stats = {"rows": len(parsed["rows"]) + len(parsed["errors"]), "rejected_rows": len(parsed["errors"])}
        db.add(imp)
        db.flush()
        audit.record(db, tenant_id, actor, "atlas.import_rejected", "atlas_import", imp.id,
                     {"sha256": digest, "rejected_rows": len(parsed["errors"])})
        return imp, False

    db.add(imp)
    db.flush()
    existing = {v.atlas_id: v for v in db.scalars(select(AtlasVendor).where(AtlasVendor.tenant_id == tenant_id)).all()}
    stats = {"rows": len(parsed["rows"]), "inserted": 0, "updated": 0, "unchanged": 0, "withdrawn": 0}
    incoming = set()
    for row in parsed["rows"]:
        src = row["source"]
        incoming.add(src["atlas_id"])
        h = hashlib.sha256(canonical_json(src).encode()).hexdigest()
        typed = dict(name=src["name"], parent_entity=src["parent_entity"], lifecycle_segment=src["lifecycle_segment"],
                     nodes=row["nodes"], selection=src["selection"], research_category=src["research_category"],
                     ai_evidence=src["ai_evidence"], price_evidence=src["price_evidence"], source=src,
                     content_hash=h, source_row=row["row"], import_id=imp.id, in_latest_import=True)
        v = existing.get(src["atlas_id"])
        if v is None:
            db.add(AtlasVendor(tenant_id=tenant_id, atlas_id=src["atlas_id"], **typed))
            stats["inserted"] += 1
        elif v.content_hash == h and v.in_latest_import:
            stats["unchanged"] += 1
        else:
            for k, val in typed.items():
                setattr(v, k, val)
            stats["updated"] += 1
    for atlas_id, v in existing.items():
        if atlas_id not in incoming and v.in_latest_import:
            v.in_latest_import = False
            stats["withdrawn"] += 1
    imp.stats = stats
    db.flush()
    audit.record(db, tenant_id, actor, "atlas.imported", "atlas_import", imp.id, {"sha256": digest, **stats})
    return imp, False


def import_view(imp: AtlasImport) -> dict:
    return {"id": imp.id, "filename": imp.filename, "sha256": imp.sha256, "size_bytes": imp.size_bytes,
            "source_label": imp.source_label, "status": imp.status, "stats": imp.stats, "errors": imp.errors[:50],
            "error_count": len(imp.errors), "imported_by": imp.imported_by, "at": imp.created_at.isoformat()}
