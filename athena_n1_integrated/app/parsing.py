"""Document parsing (Docling) and PII detection / redaction (Presidio), with deterministic fallbacks.

``DoclingAdapter`` uses IBM Docling when it is installed (layout-aware PDF/DOCX/XLSX parsing with
table structure). Without it, it falls back to pypdf / python-docx / a minimal XLSX reader and
says so in ``ParsedDocument.parser``. ``PresidioAdapter`` uses Microsoft Presidio when installed,
otherwise regex recognizers (email, phone, payment card with Luhn, IBAN, IP, US SSN, India PAN /
Aadhaar). Both report their mode on ``/health`` so a fallback is never silent.
"""
from __future__ import annotations

import importlib.util
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Optional


class ParseError(Exception):
    pass


@dataclass
class ParsedDocument:
    text: str
    parser: str
    mime: str
    pages: list[str] = field(default_factory=list)
    tables: list[list[list[str]]] = field(default_factory=list)


class DoclingAdapter:
    def __init__(self) -> None:
        self.available = importlib.util.find_spec("docling") is not None

    @property
    def mode(self) -> str:
        return "docling" if self.available else "fallback (pypdf/python-docx/xlsx-xml)"

    def parse(self, data: bytes, mime: str, filename: str) -> ParsedDocument:
        if self.available and mime in ("application/pdf",
                                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"):
            return self._docling(data, mime, filename)
        return self._fallback(data, mime, filename)

    def _docling(self, data: bytes, mime: str, filename: str) -> ParsedDocument:  # pragma: no cover - optional dep
        from docling.datamodel.base_models import DocumentStream
        from docling.document_converter import DocumentConverter
        result = DocumentConverter().convert(DocumentStream(name=filename, stream=io.BytesIO(data)))
        doc = result.document
        tables = []
        for t in getattr(doc, "tables", []):
            df = t.export_to_dataframe()
            tables.append([list(map(str, df.columns))] + [[str(c) for c in row] for row in df.values.tolist()])
        return ParsedDocument(doc.export_to_markdown(), "docling", mime, tables=tables)

    def _fallback(self, data: bytes, mime: str, filename: str) -> ParsedDocument:
        try:
            if mime == "application/pdf":
                from pypdf import PdfReader
                pages = [(p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages]
                return ParsedDocument("\n\n".join(pages), "fallback:pypdf", mime, pages=pages)
            if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
                import docx
                d = docx.Document(io.BytesIO(data))
                paras = [p.text for p in d.paragraphs]
                tables = [[[c.text for c in row.cells] for row in t.rows] for t in d.tables]
                table_text = ["\t".join(r) for t in tables for r in t]
                return ParsedDocument("\n".join(paras + table_text), "fallback:python-docx", mime, tables=tables)
            if mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
                tables = _xlsx_tables(data)
                text = "\n".join("\t".join(r) for t in tables for r in t)
                return ParsedDocument(text, "fallback:xlsx-xml", mime, tables=tables)
            if mime in ("text/plain", "text/csv", "application/json"):
                return ParsedDocument(data.decode("utf-8", errors="replace"), "fallback:text", mime)
        except Exception as exc:
            raise ParseError(f"{filename}: {type(exc).__name__}: {exc}") from exc
        raise ParseError(f"{filename}: no parser for {mime}")


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _xlsx_tables(data: bytes) -> list[list[list[str]]]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")) for si in root.findall("m:si", _NS)]
        tables = []
        for name in sorted(n for n in zf.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)):
            root = ET.fromstring(zf.read(name))
            rows = []
            for row in root.iter(f"{{{_NS['m']}}}row"):
                cells = []
                for c in row.findall("m:c", _NS):
                    v = c.find("m:v", _NS)
                    inline = c.find("m:is/m:t", _NS)
                    val = inline.text if inline is not None else (v.text if v is not None else "")
                    if c.get("t") == "s" and val:
                        val = shared[int(val)]
                    cells.append(val or "")
                rows.append(cells)
            tables.append(rows)
        return tables


# ----------------------------------------------------------------------------------------------
# PII
# ----------------------------------------------------------------------------------------------
@dataclass
class PIIFinding:
    entity_type: str
    start: int
    end: int
    score: float

    def to_dict(self) -> dict:
        return {"entity_type": self.entity_type, "start": self.start, "end": self.end, "score": self.score}


def _luhn(digits: str) -> bool:
    nums = [int(d) for d in digits][::-1]
    total = sum(n if i % 2 == 0 else (n * 2 - 9 if n * 2 > 9 else n * 2) for i, n in enumerate(nums))
    return total % 10 == 0


_RECOGNIZERS = [
    ("EMAIL_ADDRESS", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), 0.95, None),
    ("PHONE_NUMBER", re.compile(r"(?<![\w.\-/])\+?\d{1,3}[\s\-.]?\(?\d{2,5}\)?[\s\-.]?\d{3,4}[\s\-.]?\d{3,5}(?![\w\-/])(?!\.\d)"), 0.6, None),
    ("CREDIT_CARD", re.compile(r"\b(?:\d[ \-]?){13,19}\b"), 0.9, lambda s: _luhn(re.sub(r"\D", "", s))),
    ("IBAN_CODE", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), 0.85, None),
    ("IP_ADDRESS", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), 0.6,
     lambda s: all(0 <= int(x) <= 255 for x in s.split("."))),
    ("US_SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), 0.85, None),
    ("IN_PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), 0.85, None),
    ("IN_AADHAAR", re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b"), 0.7, None),
]


class PresidioAdapter:
    def __init__(self) -> None:
        self.available = (importlib.util.find_spec("presidio_analyzer") is not None and
                          importlib.util.find_spec("presidio_anonymizer") is not None)
        self._engine = None

    @property
    def mode(self) -> str:
        return "presidio" if self.available else "fallback (regex recognizers)"

    def analyze(self, text: str) -> list[PIIFinding]:
        if self.available:  # pragma: no cover - optional dependency
            if self._engine is None:
                from presidio_analyzer import AnalyzerEngine
                self._engine = AnalyzerEngine()
            return [PIIFinding(r.entity_type, r.start, r.end, float(r.score))
                    for r in self._engine.analyze(text=text, language="en") if r.score >= 0.5]
        found: list[PIIFinding] = []
        for etype, rx, score, check in _RECOGNIZERS:
            for m in rx.finditer(text):
                if check is None or check(m.group(0)):
                    if not any(f.start <= m.start() < f.end for f in found):
                        found.append(PIIFinding(etype, m.start(), m.end(), score))
        return sorted(found, key=lambda f: f.start)

    def redact(self, text: str, findings: Optional[list[PIIFinding]] = None) -> tuple[str, list[PIIFinding]]:
        findings = self.analyze(text) if findings is None else findings
        out, last = [], 0
        for f in sorted(findings, key=lambda f: f.start):
            if f.start < last:
                continue
            out += [text[last:f.start], f"<{f.entity_type}>"]
            last = f.end
        out.append(text[last:])
        return "".join(out), findings
