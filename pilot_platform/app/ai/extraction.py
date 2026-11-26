"""RFQ field extraction with confidence flags, provenance and schema checks (activity H).

`RuleBasedExtractor` is a deterministic baseline: labelled lines ("Supply voltage: 3.3 V")
score 0.95, keyword-anchored prose ("powered from a 24 V supply") 0.75, bare matches 0.5.
Any extractor (e.g. a fine-tuned SLM behind the LLM gateway) can replace it as long as
it returns the same contract; `evals.evaluate_extraction` scores either against the gold set.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Protocol

from ..contracts import units
from ..contracts.rfq import (CURRENCY_SYMBOLS, FIELDS, KNOWN_CERTIFICATIONS, KNOWN_INTERFACES, LABEL_INDEX,
                             REQUIRED, RFQ_SCHEMA_VERSION)

LABELLED, ANCHORED, BARE = 0.95, 0.75, 0.5
LOW_CONFIDENCE = 0.7
NUM = r"[-+]?\d[\d,]*(?:\.\d+)?"

_LABEL_LINE = re.compile(r"^\s*(?:[-*•]\s*)?(?P<label>[A-Za-z][A-Za-z0-9 /()&\-]{0,40}?)\s*[:=]\s*(?P<value>\S.*?)\s*$")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
_RANGE = re.compile(rf"({NUM})\s*°?\s*([CF])?\s*(?:to|~|…|\.\.|–|—|-)\s*({NUM})\s*°?\s*([CF])\b")
_DIMS = re.compile(r"(\d+(?:\.\d+)?)\s*(mm|cm|in)?\s*[x×X*]\s*(\d+(?:\.\d+)?)\s*(mm|cm|in)\b")
_INT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([kK])?\b")
_QTY_PROSE = re.compile(r"(\d[\d,]*)\s*([kK])?\s*(?:units|pcs|pieces|boards|nos|devices)\b", re.I)
_MONEY = re.compile(r"(USD|EUR|INR|GBP|\$|€|₹|£)\s*(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s*(USD|EUR|INR|GBP)\b")
_LIST_SPLIT = re.compile(r"\s*(?:,|;|/|&|\band\b)\s*")
UNIT_TOKEN = {
    "V": re.compile(rf"(?<![\w.])(?:(\d+)V(\d+)|({NUM})\s*([mkK]?)V)\b"),
    "A": re.compile(rf"(?<![\w.])({NUM})\s*([munµ]?)A\b"),
    "W": re.compile(rf"(?<![\w.])({NUM})\s*([mkK]?)W\b"),
}
ANCHORS = {
    "supply_voltage": re.compile(r"supply|input|powered|rail|battery|\bvin\b", re.I),
    "max_current": re.compile(r"current|draw|consumption|amps", re.I),
    "power_budget": re.compile(r"power|budget|dissipat", re.I),
}


class Extractor(Protocol):
    name: str

    def extract(self, text: str) -> dict: ...


def _segments(text: str):
    """Yield (start_offset, segment) for every line and sentence, preserving offsets."""
    pos = 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        offset = 0
        for part in _SENTENCE_SPLIT.split(stripped):
            idx = stripped.find(part, offset)
            if part.strip():
                yield pos + idx, part.rstrip(".")
            offset = idx + len(part)
        pos += len(line)


def _field_for_label(label: str) -> str | None:
    norm = re.sub(r"\(.*?\)", "", label).strip().lower()
    for synonym, field in LABEL_INDEX:
        if norm == synonym or norm.startswith(synonym + " "):
            return field
    return None


def _parse_unit_value(text: str, unit: str) -> float | None:
    m = UNIT_TOKEN[unit].search(text)
    if not m:
        return None
    if unit == "V" and m.group(1):
        return units.parse_quantity(f"{m.group(1)}V{m.group(2)}").value
    number, prefix = (m.group(3), m.group(4)) if unit == "V" else (m.group(1), m.group(2))
    return units.parse_quantity(f"{number} {prefix}{unit}").value


def _parse_int(text: str) -> int | None:
    m = _INT.search(text)
    if not m:
        return None
    value = units.clean_number(m.group(1)) * (1000 if m.group(2) else 1)
    return int(round(value))


def _parse_list(text: str) -> list[str]:
    return [p.strip(" .") for p in _LIST_SPLIT.split(text) if p.strip(" .")]


def _parse_money(text: str) -> dict | None:
    m = _MONEY.search(text)
    if not m:
        return None
    cur, amt = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
    try:
        amount = Decimal(amt.replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return {"amount": str(amount), "currency": CURRENCY_SYMBOLS.get(cur, cur)}


def _parse_range(text: str):
    m = _RANGE.search(text.replace("−", "-"))
    if not m:
        return None
    lo, lo_scale, hi, hi_scale = m.groups()
    return (units.to_celsius(units.clean_number(lo), lo_scale or hi_scale),
            units.to_celsius(units.clean_number(hi), hi_scale), m.start(), m.end())


def _parse_dims(text: str):
    m = _DIMS.search(text)
    if not m:
        return None
    a, ua, b, ub = m.groups()
    return units.length_to_m(float(a), ua or ub), units.length_to_m(float(b), ub), m.start(), m.end()


class RuleBasedExtractor:
    name = "rule-based-v1"

    def extract(self, text: str) -> dict:
        cands: dict[str, list[dict]] = {}
        unparsed: list[dict] = []

        def add(field, value, conf, start, end):
            entry = {"value": value, "confidence": conf, "source": {"start": start, "end": end, "text": text[start:end]}}
            if FIELDS[field].unit:
                entry["unit"] = FIELDS[field].unit
            cands.setdefault(field, []).append(entry)

        labelled_fields: set[str] = set()
        for start, seg in _segments(text):
            m = _LABEL_LINE.match(seg)
            if not m:
                continue
            field = _field_for_label(m.group("label"))
            if field is None:
                continue
            value_text, v_start = m.group("value"), start + m.start("value")
            v_end = v_start + len(value_text)
            spec = FIELDS[field]
            parsed = True
            if spec.kind == "temperature":
                r = _parse_range(value_text)
                if r:
                    add("operating_temp_min", r[0], LABELLED, v_start, v_end)
                    add("operating_temp_max", r[1], LABELLED, v_start, v_end)
                    labelled_fields |= {"operating_temp_min", "operating_temp_max"}
                parsed = bool(r)
            elif spec.kind == "length":
                d = _parse_dims(value_text)
                if d:
                    add("board_length", d[0], LABELLED, v_start, v_end)
                    add("board_width", d[1], LABELLED, v_start, v_end)
                    labelled_fields |= {"board_length", "board_width"}
                parsed = bool(d)
            else:
                value = {
                    "int": _parse_int, "list": _parse_list, "money": _parse_money, "text": lambda t: t.strip(),
                    "quantity": lambda t: _parse_unit_value(t, spec.unit),
                }[spec.kind](value_text)
                parsed = value not in (None, [], "")
                if parsed:
                    add(field, value, LABELLED, v_start, v_end)
                    labelled_fields.add(field)
            if not parsed:
                unparsed.append({"field": field, "text": value_text})

        self._prose(text, labelled_fields, add)
        return self._finalise(text, cands, unparsed)

    def _prose(self, text: str, skip: set[str], add) -> None:
        """Unlabelled fallbacks, only for fields no labelled line supplied."""
        clean = text.replace("−", "-")
        if "operating_temp_min" not in skip and (r := _parse_range(clean)):
            add("operating_temp_min", r[0], ANCHORED, r[2], r[3])
            add("operating_temp_max", r[1], ANCHORED, r[2], r[3])
        if "board_length" not in skip and (d := _parse_dims(clean)):
            add("board_length", d[0], ANCHORED, d[2], d[3])
            add("board_width", d[1], ANCHORED, d[2], d[3])
        if "quantity" not in skip and (m := _QTY_PROSE.search(clean)):
            add("quantity", int(units.clean_number(m.group(1)) * (1000 if m.group(2) else 1)), ANCHORED, m.start(), m.end())
        for field, unit in (("supply_voltage", "V"), ("max_current", "A"), ("power_budget", "W")):
            if field in skip:
                continue
            for start, seg in _segments(clean):
                for m in UNIT_TOKEN[unit].finditer(seg):
                    value = _parse_unit_value(m.group(0), unit)
                    if value is not None:
                        conf = ANCHORED if ANCHORS[field].search(seg) else BARE
                        add(field, value, conf, start + m.start(), start + m.end())
        for field, vocab in (("interfaces", KNOWN_INTERFACES), ("certifications", KNOWN_CERTIFICATIONS)):
            if field in skip:
                continue
            hits = []
            for token in vocab:
                flags = 0 if token.isupper() else re.I  # "CAN"/"CE"/"UL" must be upper-case words
                for m in re.finditer(rf"(?<![\w-]){re.escape(token)}(?![\w-])", clean, flags):
                    hits.append((m.start(), m.end(), m.group(0)))
            if hits:
                hits.sort()
                add(field, [h[2] for h in hits], 0.6, hits[0][0], hits[-1][1])

    def _finalise(self, text: str, cands: dict[str, list[dict]], unparsed: list[dict]) -> dict:
        fields, conflicts = {}, []
        for field, entries in cands.items():
            if FIELDS[field].kind == "list":
                merged: list[str] = []
                for e in entries:
                    merged.extend(v for v in e["value"] if v.lower() not in {m.lower() for m in merged})
                best = max(entries, key=lambda e: e["confidence"])
                fields[field] = {**best, "value": merged}
                continue
            top = max(e["confidence"] for e in entries)
            strongest = [e for e in entries if e["confidence"] == top]
            distinct = {repr(e["value"]) for e in strongest}
            chosen = dict(strongest[0])
            if len(distinct) > 1:
                conflicts.append({"field": field, "candidates": strongest})
                chosen["confidence"] = min(chosen["confidence"], 0.4)
            fields[field] = chosen

        errors = []
        t_min, t_max = fields.get("operating_temp_min"), fields.get("operating_temp_max")
        if t_min and t_max and t_min["value"] >= t_max["value"]:
            errors.append("operating_temp_min must be below operating_temp_max")
        if "quantity" in fields and fields["quantity"]["value"] <= 0:
            errors.append("quantity must be positive")
        if "supply_voltage" in fields and not 0 < fields["supply_voltage"]["value"] <= 1000:
            errors.append("supply_voltage outside 0-1000 V")
        missing = [f for f in REQUIRED if f not in fields]
        low = sorted(f for f, v in fields.items() if v["confidence"] < LOW_CONFIDENCE)
        return {
            "schema_version": RFQ_SCHEMA_VERSION, "extractor": self.name, "fields": fields,
            "conflicts": conflicts, "missing_required": missing, "low_confidence": low,
            "unparsed": unparsed, "schema_errors": errors,
            "needs_review": bool(conflicts or missing or low or unparsed or errors),
        }


DEFAULT_EXTRACTOR = RuleBasedExtractor()


def extract_rfq(text: str, extractor: Extractor = DEFAULT_EXTRACTOR) -> dict:
    if not text or not text.strip():
        return DEFAULT_EXTRACTOR._finalise("", {}, [])
    return extractor.extract(text)


def field_values(extraction: dict) -> dict:
    return {k: v["value"] for k, v in extraction.get("fields", {}).items()}
