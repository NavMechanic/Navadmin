"""Turn an uploaded file into text and tables.

Three formats, three failure modes worth naming:

**PDF.** ``pypdf`` extracts the text layer. A scanned datasheet has no text
layer, so extraction returns empty pages — and the correct response is to say
"this PDF has no extractable text, it needs OCR", not to run the regexes over an
empty string and report that no parameters were found. Those are different
answers and only one of them tells the user what to do.

**CSV.** Encoding and delimiter are guessed, and both guesses are reported. A
semicolon-delimited European export read as comma-delimited parses as one column
per row without raising anything.

**XLSX.** Every sheet is read, because the parameters are rarely on the first
one.

Nothing here decides what a value *means*; that is ``specs.py``. This module's
only job is to produce text faithfully or explain why it could not.
"""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

MAX_BYTES = 40 * 1024 * 1024
SUPPORTED = (".pdf", ".csv", ".xlsx", ".xls", ".txt", ".md", ".json")


class DocumentError(ValueError):
    """The document cannot be read, with a reason the caller can act on."""


@dataclass(slots=True)
class ParsedDocument:
    filename: str
    kind: str
    pages: list[str] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    page_count: int = 0
    char_count: int = 0
    encoding: str | None = None
    delimiter: str | None = None
    sheet_names: list[str] = field(default_factory=list)
    needs_ocr: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.pages)

    def as_dict(self) -> dict[str, Any]:
        return {"filename": self.filename, "kind": self.kind,
                "page_count": self.page_count, "char_count": self.char_count,
                "encoding": self.encoding, "delimiter": self.delimiter,
                "sheet_names": self.sheet_names, "table_count": len(self.tables),
                "needs_ocr": self.needs_ocr, "warnings": self.warnings}


def _parse_pdf(blob: bytes, filename: str) -> ParsedDocument:
    try:
        import pypdf
    except ImportError as exc:  # pragma: no cover
        raise DocumentError("pypdf is not installed; PDFs cannot be read.") from exc

    try:
        reader = pypdf.PdfReader(io.BytesIO(blob))
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(
            f"Not a readable PDF: {type(exc).__name__}: {exc}") from exc

    doc = ParsedDocument(filename=filename, kind="pdf")
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            raise DocumentError(
                "This PDF is encrypted and cannot be opened without a password.")
        doc.warnings.append(
            "The PDF was encrypted with an empty password and was opened. Check "
            "it is the document you expected.")

    for page in reader.pages:
        try:
            doc.pages.append(page.extract_text() or "")
        except Exception as exc:  # noqa: BLE001
            doc.pages.append("")
            doc.warnings.append(f"A page could not be extracted: {exc}")

    doc.page_count = len(doc.pages)
    doc.char_count = sum(len(p) for p in doc.pages)

    if doc.page_count and doc.char_count < 40 * doc.page_count:
        doc.needs_ocr = True
        doc.warnings.append(
            f"This PDF yielded {doc.char_count} characters across {doc.page_count} "
            "page(s) — effectively no text layer. It is almost certainly a scan. "
            "Running parameter extraction over it would report 'no parameters "
            "found', which is true and useless: the correct answer is that this "
            "document needs OCR before it can be read at all.")
    return doc


def _parse_csv(blob: bytes, filename: str) -> ParsedDocument:
    doc = ParsedDocument(filename=filename, kind="csv")
    text = None
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            text = blob.decode(encoding)
            doc.encoding = encoding
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise DocumentError("Could not decode this CSV in any common encoding.")
    if doc.encoding != "utf-8":
        doc.warnings.append(
            f"Decoded as {doc.encoding} rather than UTF-8. Check any non-ASCII "
            "characters (µ, °, Ω) survived — they are exactly the characters a "
            "datasheet uses and a mis-decode turns them into noise the extractor "
            "will not match.")

    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        doc.delimiter = dialect.delimiter
    except csv.Error:
        doc.delimiter = ","
        doc.warnings.append(
            "The delimiter could not be sniffed; assumed a comma. A "
            "semicolon-delimited export read as comma-delimited parses as one "
            "column per row and looks like a file with no data in it.")

    rows = list(csv.DictReader(io.StringIO(text), delimiter=doc.delimiter))
    if rows and len(rows[0]) == 1 and doc.delimiter == ",":
        doc.warnings.append(
            "Every row parsed into a single column. That usually means the "
            "delimiter is wrong, not that the file has one column.")

    doc.tables = [{"sheet": "csv", "rows": rows[:5000],
                   "row_count": len(rows),
                   "columns": list(rows[0].keys()) if rows else []}]
    doc.pages = [text]
    doc.page_count = 1
    doc.char_count = len(text)
    return doc


def _parse_xlsx(blob: bytes, filename: str) -> ParsedDocument:
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover
        raise DocumentError("openpyxl is not installed; XLSX cannot be read.") from exc

    doc = ParsedDocument(filename=filename, kind="xlsx")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(blob), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(f"Not a readable workbook: {exc}") from exc

    doc.sheet_names = list(wb.sheetnames)
    chunks: list[str] = []
    for name in wb.sheetnames:
        sheet = wb[name]
        rows = [[("" if c is None else str(c)) for c in row]
                for row in sheet.iter_rows(values_only=True)]
        if not rows:
            continue
        header = rows[0]
        records = [dict(zip(header, r)) for r in rows[1:5001]]
        doc.tables.append({"sheet": name, "rows": records,
                           "row_count": max(0, len(rows) - 1), "columns": header})
        chunks.append(f"# sheet: {name}\n" +
                      "\n".join(" ".join(r) for r in rows))
    wb.close()

    doc.pages = chunks or [""]
    doc.page_count = len(doc.pages)
    doc.char_count = sum(len(c) for c in chunks)
    if len(doc.sheet_names) > 1:
        doc.warnings.append(
            f"{len(doc.sheet_names)} sheets were read ({', '.join(doc.sheet_names)}). "
            "Parameters are frequently not on the first one.")
    return doc


def _parse_text(blob: bytes, filename: str, kind: str) -> ParsedDocument:
    doc = ParsedDocument(filename=filename, kind=kind)
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            text = blob.decode(encoding)
            doc.encoding = encoding
            break
        except UnicodeDecodeError:
            continue
    else:
        raise DocumentError("Could not decode this file as text.")
    doc.pages = [text]
    doc.page_count = 1
    doc.char_count = len(text)
    return doc


def parse(blob: bytes, filename: str) -> ParsedDocument:
    """Dispatch on extension. Refuses unknown types rather than guessing."""
    if not blob:
        raise DocumentError("The uploaded file is empty.")
    if len(blob) > MAX_BYTES:
        raise DocumentError(
            f"File is {len(blob) / 1e6:.1f} MB, above the {MAX_BYTES / 1e6:.0f} MB "
            "cap.")

    lower = filename.lower()
    if lower.endswith(".pdf"):
        return _parse_pdf(blob, filename)
    if lower.endswith(".csv"):
        return _parse_csv(blob, filename)
    if lower.endswith((".xlsx", ".xlsm")):
        return _parse_xlsx(blob, filename)
    if lower.endswith(".xls"):
        raise DocumentError(
            "Legacy .xls is not supported — openpyxl reads .xlsx only. Re-save as "
            ".xlsx. (Silently misreading a legacy binary workbook would be worse "
            "than refusing it.)")
    if lower.endswith((".txt", ".md")):
        return _parse_text(blob, filename, "text")
    if lower.endswith(".json"):
        return _parse_text(blob, filename, "json")

    raise DocumentError(
        f"Unsupported file type {filename!r}. Supported: {', '.join(SUPPORTED)}.")
