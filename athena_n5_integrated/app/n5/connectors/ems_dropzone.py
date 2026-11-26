"""EMS / ODM controlled drop zone: signed quotations, capacity allocations, NRE and service terms.

A submission is one ``athena-ems-quote/1`` manifest plus the files it lists:

* the quotation document (signed PDF, CSV or X12 843 EDI) whose SHA-256 is in the manifest, and a
  detached signature over its bytes made with a key registered on the supplier record
  (Ed25519, RSA-PSS-SHA256 or RSA-PKCS1v1.5-SHA256);
* optional capacity-allocation evidence, service terms and other attachments, each hash-listed.

Offers come from the manifest, from a CSV quotation (``offer_csv``) or from an X12 843 subset
(``parse_x12_843``). NRE, tooling and service charges are recorded and passed to N7 as *excluded*
items: they are never part of the indicative material total.
"""
from __future__ import annotations

import base64
import csv
import io
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from ..models import OfferIn, SigningKey
from .base import dec

CSV_COLUMNS = ("line", "quoted_mpn", "manufacturer", "currency", "moq", "pack_multiple", "availability",
               "stock_qty", "lead_time_days", "price_breaks")


def verify_signature(key: SigningKey, data: bytes, signature_b64: str) -> bool:
    try:
        sig = base64.b64decode(signature_b64, validate=True)
        pub = serialization.load_pem_public_key(key.public_key_pem.encode())
        if key.algorithm == "ED25519":
            if not isinstance(pub, ed25519.Ed25519PublicKey):
                return False
            pub.verify(sig, data)
        else:
            if not isinstance(pub, rsa.RSAPublicKey):
                return False
            pad = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH) \
                if key.algorithm == "RSA_PSS_SHA256" else padding.PKCS1v15()
            pub.verify(sig, data, pad, hashes.SHA256())
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def _breaks(text: str) -> list[dict]:
    out = []
    for part in (text or "").replace(",", ";").split(";"):
        if ":" in part:
            q, p = part.split(":", 1)
            out.append({"min_qty": int(q.strip()), "unit_price": dec(p.strip())})
    return out


def offer_csv(data: bytes) -> tuple[list[dict], list[str]]:
    """CSV quotation: one row per offer; ``price_breaks`` as ``qty:price;qty:price``."""
    rows, problems = [], []
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
    missing = [c for c in CSV_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        return [], [f"quotation CSV is missing columns {missing}"]
    for i, r in enumerate(reader, 2):
        try:
            rows.append(OfferIn.model_validate({
                "line": int(r["line"]) if r["line"].strip() else None, "quoted_mpn": r["quoted_mpn"].strip(),
                "manufacturer": r["manufacturer"].strip() or None, "currency": r["currency"].strip().upper(),
                "moq": int(r["moq"]), "pack_multiple": int(r["pack_multiple"]),
                "availability": r["availability"].strip().upper() or "STOCK",
                "stock_qty": int(r["stock_qty"]) if r["stock_qty"].strip() else None,
                "lead_time_days": int(r["lead_time_days"]), "price_breaks": _breaks(r["price_breaks"])})
                .model_dump(mode="json"))
        except (ValueError, KeyError, TypeError) as exc:
            problems.append(f"quotation CSV row {i}: {str(exc)[:200]}")
    return rows, problems


def _d8(v: str) -> Optional[datetime]:
    try:
        return datetime.strptime(v, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_x12_843(data: bytes) -> tuple[dict[str, Any], list[dict], list[str]]:
    """ATHENA X12 843 (Response to Request for Quotation) profile, a documented subset:

        BQT*00*<quote ref>*<CCYYMMDD issued>        CUR*SE*<currency>       DTM*036*<CCYYMMDD valid until>
        PO1*<BOM line>*<qty>*EA*<unit price>*PE*MG*<MPN>[*VP*<supplier SKU>]
        CTP**PRP*<unit price>*<min qty>             one per price break (defaults to the PO1 price at 1)
        PO4*<pack multiple>      QTY*MO*<MOQ>      LDT*AE*<days>*DA      QTY*33*<stock>

    Segment terminator ``~`` (or newline), element separator ``*``."""
    text = data.decode("ascii", errors="replace")
    segs = [s.strip() for s in text.replace("\n", "~").split("~") if s.strip()]
    header: dict[str, Any] = {}
    offers: list[dict] = []
    problems: list[str] = []
    cur: Optional[dict] = None
    currency = None

    def close() -> None:
        if cur is not None:
            if not cur["price_breaks"]:
                cur["price_breaks"] = [{"min_qty": 1, "unit_price": cur.pop("_po1_price")}]
            cur.pop("_po1_price", None)
            try:
                offers.append(OfferIn.model_validate({**cur, "currency": currency}).model_dump(mode="json"))
            except ValueError as exc:
                problems.append(f"PO1 line {cur.get('line')}: {str(exc)[:200]}")

    if not segs or not any(s.startswith("ST*843") for s in segs):
        return {}, [], ["not an X12 843 transaction set (ST*843 missing)"]
    for s in segs:
        el = s.split("*")
        tag = el[0]
        if tag == "BQT" and len(el) >= 4:
            header["quote_ref"], header["issued_at"] = el[2], _d8(el[3])
        elif tag == "CUR" and len(el) >= 3:
            currency = el[2].strip().upper()
        elif tag == "DTM" and len(el) >= 3 and el[1] == "036":
            header["valid_until"] = _d8(el[2])
        elif tag == "PO1":
            close()
            try:
                mpn = el[el.index("MG") + 1]
                sku = el[el.index("VP") + 1] if "VP" in el else None
                cur = {"line": int(el[1]) if el[1] else None, "quoted_mpn": mpn, "moq": 1, "pack_multiple": 1,
                       "lead_time_days": 0, "availability": "FACTORY_ORDER", "price_breaks": [],
                       "_po1_price": dec(el[4]), "supplier_sku": sku}
            except (ValueError, IndexError):
                problems.append(f"malformed PO1 segment {s[:60]!r}")
                cur = None
        elif cur is not None and tag == "CTP" and len(el) >= 5:
            cur["price_breaks"].append({"min_qty": int(el[4]), "unit_price": dec(el[3])})
        elif cur is not None and tag == "PO4" and len(el) >= 2:
            cur["pack_multiple"] = int(el[1])
        elif cur is not None and tag == "QTY" and len(el) >= 3 and el[1] == "MO":
            cur["moq"] = int(el[2])
        elif cur is not None and tag == "QTY" and len(el) >= 3 and el[1] == "33":
            cur["stock_qty"], cur["availability"] = int(el[2]), "STOCK"
        elif cur is not None and tag == "LDT" and len(el) >= 3:
            cur["lead_time_days"] = int(el[2])
    close()
    if currency is None:
        problems.append("CUR segment missing")
    return header, offers, problems


def to_decimal(v: Any) -> Optional[Decimal]:
    return dec(v)
