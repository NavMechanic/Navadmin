"""Parameter extraction from datasheet text.

The rule this module is built around
------------------------------------
**A failed extraction returns nothing. It never returns a default.**

The reference implementation this replaces did:

    rth_match = re.search(r'...thermal resistance...([0-9.]+)\\s*°?C/W', text)
    rth_jc = float(rth_match.group(1)) if rth_match else 2.5   # Default fallback

That fallback is the same failure mode as Node 5's provisional Hi-Pot recipe,
and it is worse because it is invisible. A datasheet whose thermal resistance
could not be parsed — wrong layout, scanned image, table split across a page
break — silently becomes a part with θJC = 2.5 K/W. That number flows into Node
4's thermal twin, which computes a junction temperature from it, which decides
whether the adapter passes its temperature qualification. Nobody ever sees the
regex that did not match.

So every extraction here returns a ``Spec`` or nothing, and a ``Spec`` carries:

* ``value`` and ``unit`` — parsed, never assumed
* ``evidence`` — the substring it was read from, so a human can check it in
  seconds rather than reopening the PDF
* ``confidence`` — how the match was made, because a labelled table cell and a
  loose regex over body text are not equally trustworthy
* ``page`` — where to look

A document with no θJC is reported as a document with no θJC. The caller decides
what to do about that; this module will not decide for it by inventing a number.

On why this is regex and not an LLM
-----------------------------------
An LLM extractor would handle more layouts. It would also hallucinate a
plausible θJC for a datasheet that does not contain one, which is exactly the
failure this module exists to prevent, and it would do it with high confidence
and no evidence string. The right architecture is a model that proposes and a
rule that verifies against the source text — the ``evidence`` field is the seam
where that lands. Until then, a narrow extractor that misses is safer than a
broad one that invents.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable

log = logging.getLogger(__name__)


class Confidence(StrEnum):
    """How the value was found, not how sure we feel about it."""
    LABELLED_TABLE = "LABELLED_TABLE"   # a named column/row in a parsed table
    LABELLED_TEXT = "LABELLED_TEXT"     # "θJC = 1.8 °C/W" with the symbol present
    UNITED_TEXT = "UNITED_TEXT"         # a number with the right unit nearby
    WEAK = "WEAK"                       # a number that fits, with little context


# Ranked worst to best, so `max()` picks the strongest evidence.
CONFIDENCE_RANK = {Confidence.WEAK: 0, Confidence.UNITED_TEXT: 1,
                   Confidence.LABELLED_TEXT: 2, Confidence.LABELLED_TABLE: 3}


@dataclass(slots=True)
class Spec:
    key: str
    value: float
    unit: str
    confidence: Confidence
    evidence: str
    page: int | None = None
    source: str = "regex"
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value, "unit": self.unit,
                "confidence": self.confidence.value, "evidence": self.evidence,
                "page": self.page, "source": self.source, "note": self.note}


@dataclass(slots=True)
class SpecPattern:
    key: str
    unit: str
    patterns: tuple[tuple[str, Confidence], ...]
    # Values outside this range are refused, not clamped. A parse that produces
    # a 900 K/W thermal resistance has matched the wrong number on the page, and
    # accepting it because it is "a number" is how a page number becomes a
    # thermal parameter.
    plausible: tuple[float, float] = (-1e9, 1e9)
    safety_relevant: bool = False
    description: str = ""


# ---------------------------------------------------------------------
# What this extractor knows how to find.
# ---------------------------------------------------------------------
NUM = r"([0-9]+(?:\.[0-9]+)?)"

PATTERNS: tuple[SpecPattern, ...] = (
    SpecPattern(
        key="rth_jc_k_per_w", unit="K/W", plausible=(0.01, 200.0),
        safety_relevant=True,
        description="Junction-to-case thermal resistance. Feeds Node 4's twin.",
        patterns=(
            (rf"(?:R\s*(?:th)?\s*[\(\[]?\s*(?:θ|theta)?\s*JC\s*[\)\]]?|"
             rf"θ\s*JC|theta\s*JC|R\s*θ\s*JC)\s*[=:]?\s*{NUM}\s*(?:°|deg)?\s*C\s*/\s*W",
             Confidence.LABELLED_TEXT),
            (rf"junction[\s-]*to[\s-]*case[^\n]{{0,40}}?{NUM}\s*(?:°|deg)?\s*C\s*/\s*W",
             Confidence.LABELLED_TEXT),
            (rf"thermal\s+resistance[^\n]{{0,60}}?{NUM}\s*(?:°|deg)?\s*C\s*/\s*W",
             Confidence.UNITED_TEXT),
        )),
    SpecPattern(
        key="rth_ja_k_per_w", unit="K/W", plausible=(0.1, 500.0),
        description="Junction-to-ambient thermal resistance.",
        patterns=(
            (rf"(?:R\s*(?:th)?\s*[\(\[]?\s*(?:θ|theta)?\s*JA\s*[\)\]]?|θ\s*JA)"
             rf"\s*[=:]?\s*{NUM}\s*(?:°|deg)?\s*C\s*/\s*W", Confidence.LABELLED_TEXT),
            (rf"junction[\s-]*to[\s-]*ambient[^\n]{{0,40}}?{NUM}\s*(?:°|deg)?\s*C\s*/\s*W",
             Confidence.LABELLED_TEXT),
        )),
    SpecPattern(
        key="v_ds_max_v", unit="V", plausible=(1.0, 2000.0), safety_relevant=True,
        description="Drain-source breakdown voltage.",
        patterns=(
            (rf"V\s*[\(\[]?\s*(?:DS|BR\)?DSS)\s*[\)\]]?\s*(?:\(max\))?\s*[=:]?\s*{NUM}\s*V",
             Confidence.LABELLED_TEXT),
            (rf"drain[\s-]*source\s+voltage[^\n]{{0,40}}?{NUM}\s*V",
             Confidence.LABELLED_TEXT),
        )),
    SpecPattern(
        key="v_rms_working_v", unit="V", plausible=(1.0, 1000.0),
        safety_relevant=True,
        description="Working voltage across an insulation barrier.",
        patterns=(
            (rf"working\s+voltage[^\n]{{0,40}}?{NUM}\s*V", Confidence.LABELLED_TEXT),
            (rf"V\s*rms[^\n]{{0,20}}?{NUM}\s*V", Confidence.UNITED_TEXT),
        )),
    SpecPattern(
        key="creepage_mm", unit="mm", plausible=(0.1, 50.0), safety_relevant=True,
        description="Creepage distance. Cross-checked against Nodes 2, 4 and 5.",
        patterns=(
            (rf"creepage(?:\s+distance)?\s*[=:]?\s*{NUM}\s*mm",
             Confidence.LABELLED_TEXT),
            (rf"creepage[^\n]{{0,40}}?{NUM}\s*mm", Confidence.UNITED_TEXT),
        )),
    SpecPattern(
        key="clearance_mm", unit="mm", plausible=(0.1, 50.0), safety_relevant=True,
        description="Clearance distance through air.",
        patterns=(
            (rf"clearance(?:\s+distance)?\s*[=:]?\s*{NUM}\s*mm",
             Confidence.LABELLED_TEXT),
            (rf"clearance[^\n]{{0,40}}?{NUM}\s*mm", Confidence.UNITED_TEXT),
        )),
    SpecPattern(
        key="operating_temp_max_c", unit="°C", plausible=(-60.0, 250.0),
        safety_relevant=True,
        description="Maximum operating or junction temperature.",
        patterns=(
            (rf"(?:T\s*[\(\[]?\s*J\s*[\)\]]?\s*(?:\(?max\)?)?|junction\s+temperature)"
             rf"[^\n]{{0,30}}?{NUM}\s*(?:°|deg)?\s*C", Confidence.LABELLED_TEXT),
            (rf"(?:maximum\s+)?operating\s+temperature[^\n]{{0,40}}?{NUM}\s*(?:°|deg)?\s*C",
             Confidence.LABELLED_TEXT),
        )),
    SpecPattern(
        key="lead_time_weeks", unit="weeks", plausible=(0.0, 104.0),
        description="Supplier lead time. Feeds Node 3's risk model.",
        patterns=(
            (rf"lead\s*time\s*[=:]?\s*{NUM}\s*(?:weeks?|wks?)",
             Confidence.LABELLED_TEXT),
            (rf"{NUM}\s*(?:weeks?|wks?)\s+lead\s*time", Confidence.LABELLED_TEXT),
        )),
    SpecPattern(
        key="capacity_mah", unit="mAh", plausible=(1.0, 100_000.0),
        description="Rated cell or pack capacity. Feeds Node 6's SoH denominator.",
        patterns=(
            (rf"(?:rated\s+)?capacity\s*[=:]?\s*{NUM}\s*mAh", Confidence.LABELLED_TEXT),
            (rf"{NUM}\s*mAh", Confidence.WEAK),
        )),
    SpecPattern(
        key="i_max_a", unit="A", plausible=(0.001, 500.0),
        description="Maximum continuous current.",
        patterns=(
            (rf"(?:I\s*[\(\[]?\s*(?:D|max|out)\s*[\)\]]?|continuous\s+current)"
             rf"[^\n]{{0,30}}?{NUM}\s*A", Confidence.LABELLED_TEXT),
        )),
)

BY_KEY = {p.key: p for p in PATTERNS}
SAFETY_KEYS = frozenset(p.key for p in PATTERNS if p.safety_relevant)


def _clean(text: str) -> str:
    """Normalise the characters PDF extraction mangles.

    A PDF that renders "θJC" often extracts as "qJC" or "\\u03b8JC", and a
    non-breaking space between number and unit is extremely common. Normalising
    here rather than adding alternatives to ten regexes keeps the patterns
    readable.
    """
    replacements = {
        " ": " ", " ": " ", " ": " ",   # spaces
        "–": "-", "—": "-", "−": "-",   # dashes and minus
        "°": "°",
        "θ": "θ", "Θ": "θ",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    # Collapse runs of whitespace but keep line structure, because several
    # patterns are anchored with [^\n] to avoid crossing rows in a table.
    text = re.sub(r"[ \t]+", " ", text)
    return text


def extract_one(text: str, spec: SpecPattern, *, page: int | None = None
                ) -> Spec | None:
    """Find one parameter, or return None. Never a default."""
    cleaned = _clean(text)
    best: Spec | None = None
    for pattern, confidence in spec.patterns:
        for match in re.finditer(pattern, cleaned, re.IGNORECASE):
            try:
                value = float(match.group(1))
            except (TypeError, ValueError):
                continue
            lo, hi = spec.plausible
            if not (lo <= value <= hi):
                log.debug("%s: %s outside %s-%s, refused", spec.key, value, lo, hi)
                continue
            start = max(0, match.start() - 45)
            evidence = cleaned[start:match.end() + 25].strip().replace("\n", " ")
            candidate = Spec(key=spec.key, value=value, unit=spec.unit,
                             confidence=confidence, evidence=evidence, page=page)
            if best is None or (CONFIDENCE_RANK[confidence]
                                > CONFIDENCE_RANK[best.confidence]):
                best = candidate
    return best


def extract_all(text: str, *, page: int | None = None,
                keys: Iterable[str] | None = None) -> dict[str, Spec]:
    wanted = [BY_KEY[k] for k in keys] if keys else list(PATTERNS)
    found: dict[str, Spec] = {}
    for spec in wanted:
        hit = extract_one(text, spec, page=page)
        if hit is not None:
            found[spec.key] = hit
    return found


def extract_pages(pages: list[str], keys: Iterable[str] | None = None
                  ) -> dict[str, Spec]:
    """Extract across a paginated document, keeping the strongest evidence."""
    best: dict[str, Spec] = {}
    for i, page_text in enumerate(pages, start=1):
        for key, spec in extract_all(page_text, page=i, keys=keys).items():
            current = best.get(key)
            if current is None or (CONFIDENCE_RANK[spec.confidence]
                                   > CONFIDENCE_RANK[current.confidence]):
                best[key] = spec
    return best


def missing_report(found: dict[str, Spec], *, requested: Iterable[str] | None = None
                   ) -> dict[str, Any]:
    """What was NOT found, and what that blocks.

    This is the half of the result that the defaulting implementation threw
    away. It is the more important half: it is the difference between "this
    part has a θJC of 2.5" and "nobody knows this part's θJC".
    """
    wanted = set(requested) if requested else {p.key for p in PATTERNS}
    missing = sorted(wanted - set(found))
    safety_missing = sorted(k for k in missing if k in SAFETY_KEYS)

    consequences: list[str] = []
    if "rth_jc_k_per_w" in safety_missing:
        consequences.append(
            "rth_jc_k_per_w was not found. Node 4's thermal twin needs it to "
            "compute a junction temperature, and a junction temperature computed "
            "from an assumed thermal resistance is a number about an imaginary "
            "part. The twin will use its own documented default and say so; this "
            "service will not supply one.")
    if "creepage_mm" in safety_missing:
        consequences.append(
            "creepage_mm was not found. The platform already holds four "
            "conflicting creepage figures for the adapter (Node 2: 3.0 mm "
            "required / 2.4 mm measured, Node 4: 5.0 mm computed, Node 5: 6.4 mm "
            "gauge). A fifth silent default would not help.")
    if "operating_temp_max_c" in safety_missing:
        consequences.append(
            "operating_temp_max_c was not found, so there is no device limit to "
            "check a computed junction temperature against.")

    return {
        "missing": missing,
        "safety_relevant_missing": safety_missing,
        "found": sorted(found),
        "complete": not missing,
        "consequences": consequences,
        "policy": ("A parameter that could not be extracted is reported missing. "
                   "It is never defaulted. A defaulted safety parameter is "
                   "indistinguishable from a measured one once it is in the "
                   "database, and every downstream calculation inherits it "
                   "without knowing."),
    }
