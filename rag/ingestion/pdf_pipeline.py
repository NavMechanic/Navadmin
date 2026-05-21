"""Dedicated PDF ingestion pipeline for Google Drive manufacturing documents.

Pipeline stages:
  1. Fetch PDF list from Drive folder (or local mirror)
  2. Download PDFs via google-api-python-client or read from disk
  3. Extract text page-by-page with section-heading and table detection
  4. Enrich metadata: doc_type, section_header, has_table, page_range
  5. Section-preserving chunking (split at detected headings)
  6. Embed and index into Qdrant via the shared VectorStore

Doc types inferred from filename keywords:
  datasheet  – component/IC datasheets
  sop        – Standard Operating Procedures
  drawing    – assembly/mechanical drawings (DOI)
  fmea       – failure mode documents
  log        – tester/build logs
  spec       – design/product specifications
  general    – anything else
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rag.ingestion.loaders import Document, DocumentLoader


# ---------------------------------------------------------------------------
# Document-type inference
# ---------------------------------------------------------------------------

_DOC_TYPE_KEYWORDS: dict[str, list[str]] = {
    "sop": ["sop", "procedure", "work instruction", "wi-"],
    "drawing": ["drawing", "doi", "assembly", "layout", "dimension"],
    "fmea": ["fmea", "failure mode", "pfmea", "dfmea"],
    "datasheet": ["datasheet", "data sheet", "ds-", "component", "ic ", "mosfet", "sensor"],
    "log": ["log", "tester", "build summary", "nextest"],
    "spec": ["spec", "specification", "requirement", "prd", "brd"],
}


def infer_doc_type(filename: str) -> str:
    """Infer manufacturing document type from filename."""
    name_lower = filename.lower()
    for doc_type, keywords in _DOC_TYPE_KEYWORDS.items():
        if any(kw in name_lower for kw in keywords):
            return doc_type
    return "general"


# ---------------------------------------------------------------------------
# Section heading detection
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(
    r"""
    ^(?:
        \d+(?:\.\d+)*\s+[A-Z]       # "1.2 SECTION" or "3 Introduction"
        | [A-Z][A-Z\s\-/]{4,50}$    # ALL CAPS lines (≥5 chars)
        | (?:Section|Chapter|Appendix)\s+\w  # explicit section keywords
    )
    """,
    re.VERBOSE | re.MULTILINE,
)


def detect_section_heading(line: str) -> bool:
    """Return True if the line looks like a section heading."""
    stripped = line.strip()
    if len(stripped) < 4 or len(stripped) > 120:
        return False
    return bool(_HEADING_RE.match(stripped))


# ---------------------------------------------------------------------------
# Table detection
# ---------------------------------------------------------------------------

def detect_table_block(text: str) -> bool:
    """Return True if the text block contains tabular data."""
    lines = [l for l in text.split("\n") if l.strip()]
    if len(lines) < 3:
        return False

    # Heuristic 1: many lines with multiple pipe characters
    pipe_lines = sum(1 for l in lines if l.count("|") >= 2)
    if pipe_lines / len(lines) > 0.3:
        return True

    # Heuristic 2: many lines with 3+ consecutive spaces (column alignment)
    col_lines = sum(1 for l in lines if re.search(r"   +", l))
    if col_lines / len(lines) > 0.5:
        return True

    # Heuristic 3: lines with repeated numeric/unit patterns (specs)
    spec_lines = sum(1 for l in lines if re.search(r"\d+\s*(?:mm|mΩ|N|%|°C|V|A|Hz|W|ppm|mA)\b", l))
    if spec_lines >= 3:
        return True

    return False


# ---------------------------------------------------------------------------
# PDF page extractor
# ---------------------------------------------------------------------------

@dataclass
class PageContent:
    """Extracted content from a single PDF page."""
    page_num: int
    text: str
    section_heading: str = ""
    has_table: bool = False
    word_count: int = 0

    def __post_init__(self):
        self.word_count = len(self.text.split())


class PDFExtractor:
    """Extract structured content from PDFs using pypdf."""

    def extract_pages(self, path: str) -> list[PageContent]:
        """Extract all pages from a PDF, annotating each with section/table metadata."""
        from pypdf import PdfReader

        reader = PdfReader(path)
        pages: list[PageContent] = []
        current_section = ""

        for page_num, page in enumerate(reader.pages, start=1):
            raw_text = page.extract_text() or ""
            if not raw_text.strip():
                continue

            lines = raw_text.split("\n")
            page_section = current_section

            # Detect section headings on this page
            for line in lines[:8]:  # headings usually appear in first 8 lines
                if detect_section_heading(line):
                    page_section = line.strip()
                    current_section = page_section
                    break

            has_table = detect_table_block(raw_text)

            pages.append(PageContent(
                page_num=page_num,
                text=raw_text,
                section_heading=page_section,
                has_table=has_table,
            ))

        return pages

    def extract_sections(self, pages: list[PageContent]) -> list[dict]:
        """Group pages by section, returning list of section dicts."""
        sections: list[dict] = []
        current: dict = {"heading": "", "pages": [], "texts": []}

        for page in pages:
            if page.section_heading and page.section_heading != current["heading"]:
                if current["texts"]:
                    sections.append({
                        "heading": current["heading"],
                        "page_range": (current["pages"][0], current["pages"][-1]),
                        "text": "\n\n".join(current["texts"]),
                        "has_table": any(
                            detect_table_block(t) for t in current["texts"]
                        ),
                    })
                current = {
                    "heading": page.section_heading,
                    "pages": [page.page_num],
                    "texts": [page.text],
                }
            else:
                current["pages"].append(page.page_num)
                current["texts"].append(page.text)

        if current["texts"]:
            sections.append({
                "heading": current["heading"],
                "page_range": (current["pages"][0], current["pages"][-1]),
                "text": "\n\n".join(current["texts"]),
                "has_table": any(
                    detect_table_block(t) for t in current["texts"]
                ),
            })

        return sections


# ---------------------------------------------------------------------------
# PDF pipeline
# ---------------------------------------------------------------------------

@dataclass
class PDFIngestionResult:
    """Result of ingesting a single PDF."""
    filename: str
    doc_type: str
    page_count: int
    section_count: int
    chunk_count: int
    has_tables: bool
    ingested_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.error is None


class PDFPipeline:
    """End-to-end pipeline for ingesting PDFs into the RAG vector store.

    Usage — local file::

        pipeline = RAGPipeline()
        pdf_pipeline = PDFPipeline(pipeline)
        result = pdf_pipeline.ingest_file("data/aviator/root/battery_spec.pdf")

    Usage — Drive folder::

        result = pdf_pipeline.ingest_drive_folder(
            folder_id="1YU-Jtitkx1IXWO1J_K_t1EBBtjb2JSE8",
            credentials_path="service_account.json",
        )
    """

    def __init__(self, rag_pipeline) -> None:
        self._rag = rag_pipeline
        self._extractor = PDFExtractor()

    # ------------------------------------------------------------------
    # Single-file ingestion
    # ------------------------------------------------------------------

    def ingest_file(
        self,
        path: str,
        extra_metadata: Optional[dict] = None,
    ) -> PDFIngestionResult:
        """Ingest a single PDF file into the RAG vector store.

        Parameters
        ----------
        path : str
            Absolute or relative path to the PDF.
        extra_metadata : dict, optional
            Additional metadata to merge into every chunk's metadata.

        Returns
        -------
        PDFIngestionResult
        """
        filename = Path(path).name
        doc_type = infer_doc_type(filename)

        try:
            pages = self._extractor.extract_pages(path)
        except Exception as exc:
            return PDFIngestionResult(
                filename=filename, doc_type=doc_type,
                page_count=0, section_count=0, chunk_count=0,
                has_tables=False, error=str(exc),
            )

        if not pages:
            return PDFIngestionResult(
                filename=filename, doc_type=doc_type,
                page_count=0, section_count=0, chunk_count=0,
                has_tables=False, error="No text extracted from PDF",
            )

        sections = self._extractor.extract_sections(pages)
        has_tables = any(s["has_table"] for s in sections)

        # Build Document objects — one per section (or one per page if no sections)
        documents: list[Document] = []
        base_meta = {
            "source": str(Path(path).resolve()),
            "filename": filename,
            "doc_type": doc_type,
            "total_pages": len(pages),
            "category": "pdf_drive",
        }
        if extra_metadata:
            base_meta.update(extra_metadata)

        if sections:
            for sec in sections:
                if not sec["text"].strip():
                    continue
                page_start, page_end = sec["page_range"]
                meta = {
                    **base_meta,
                    "section_heading": sec["heading"],
                    "page_start": page_start,
                    "page_end": page_end,
                    "has_table": sec["has_table"],
                }
                documents.append(Document(content=sec["text"], metadata=meta))
        else:
            # No sections detected — one document per page
            for page in pages:
                meta = {
                    **base_meta,
                    "section_heading": page.section_heading,
                    "page_start": page.page_num,
                    "page_end": page.page_num,
                    "has_table": page.has_table,
                }
                documents.append(Document(content=page.text, metadata=meta))

        chunk_count = self._rag._chunk_and_index_documents(documents)

        return PDFIngestionResult(
            filename=filename,
            doc_type=doc_type,
            page_count=len(pages),
            section_count=len(sections),
            chunk_count=chunk_count,
            has_tables=has_tables,
        )

    # ------------------------------------------------------------------
    # Batch ingestion from local directory
    # ------------------------------------------------------------------

    def ingest_local_directory(
        self,
        directory: str,
        recursive: bool = True,
    ) -> list[PDFIngestionResult]:
        """Ingest all PDFs from a local directory."""
        pattern = "**/*.pdf" if recursive else "*.pdf"
        pdf_files = list(Path(directory).glob(pattern))
        results = []
        for pdf_path in sorted(pdf_files):
            print(f"  [PDF] Ingesting {pdf_path.name}…")
            result = self.ingest_file(str(pdf_path))
            results.append(result)
            status = "OK" if result.success else f"ERROR: {result.error}"
            print(
                f"  [PDF] {pdf_path.name} — {result.page_count}p "
                f"{result.section_count}sec {result.chunk_count}chunks | {status}"
            )
        return results

    # ------------------------------------------------------------------
    # Drive ingestion
    # ------------------------------------------------------------------

    def ingest_drive_folder(
        self,
        folder_id: str,
        credentials_path: Optional[str] = None,
        sync_manifest: Optional["DriveSyncManifest"] = None,
    ) -> list[PDFIngestionResult]:
        """Download and ingest all PDFs from a Google Drive folder.

        Parameters
        ----------
        folder_id : str
            Google Drive folder ID to list and download PDFs from.
        credentials_path : str, optional
            Path to service-account JSON; falls back to GOOGLE_APPLICATION_CREDENTIALS.
        sync_manifest : DriveSyncManifest, optional
            If provided, skips already-indexed files (incremental mode).
        """
        from rag.ingestion.drive_loader import DriveDocumentLoader

        loader = DriveDocumentLoader(credentials_path=credentials_path)
        file_list = loader.list_folder(folder_id, recursive=False)
        pdf_files = [f for f in file_list if f.mime_type == "application/pdf"]
        print(f"[PDFPipeline] Found {len(pdf_files)} PDFs in folder {folder_id}")

        results: list[PDFIngestionResult] = []

        for file_info in pdf_files:
            if sync_manifest and sync_manifest.is_current(
                file_info.file_id, file_info.modified_time
            ):
                print(f"  [PDF] Skipping (already indexed): {file_info.title}")
                continue

            print(f"  [PDF] Downloading: {file_info.title}…")
            try:
                text = loader.download_as_text(file_info)
            except Exception as exc:
                result = PDFIngestionResult(
                    filename=file_info.title, doc_type=infer_doc_type(file_info.title),
                    page_count=0, section_count=0, chunk_count=0,
                    has_tables=False, error=str(exc),
                )
                results.append(result)
                continue

            # download_as_text already uses PDFReader internally — we receive
            # plain text; wrap it into a Document and index directly.
            doc_type = infer_doc_type(file_info.title)
            doc = Document(
                content=text,
                metadata={
                    "source": file_info.title,
                    "filename": file_info.title,
                    "doc_type": doc_type,
                    "drive_file_id": file_info.file_id,
                    "drive_folder_id": folder_id,
                    "modified_time": file_info.modified_time,
                    "category": "pdf_drive",
                },
            )
            chunk_count = self._rag._chunk_and_index_documents([doc])
            result = PDFIngestionResult(
                filename=file_info.title,
                doc_type=doc_type,
                page_count=0,
                section_count=0,
                chunk_count=chunk_count,
                has_tables=False,
            )
            results.append(result)

            if sync_manifest:
                sync_manifest.mark_indexed(
                    file_id=file_info.file_id,
                    filename=file_info.title,
                    modified_time=file_info.modified_time,
                    chunk_count=chunk_count,
                )

        return results

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    @staticmethod
    def format_report(results: list[PDFIngestionResult]) -> str:
        """Format a human-readable ingestion report."""
        lines = ["=" * 70, "PDF Ingestion Report", "=" * 70]
        succeeded = [r for r in results if r.success]
        failed = [r for r in results if not r.success]
        total_chunks = sum(r.chunk_count for r in succeeded)
        total_pages = sum(r.page_count for r in succeeded)

        lines.append(
            f"Files processed : {len(results)}"
            f"  (✓ {len(succeeded)} succeeded, ✗ {len(failed)} failed)"
        )
        lines.append(f"Total pages     : {total_pages}")
        lines.append(f"Total chunks    : {total_chunks}")
        lines.append("")

        if succeeded:
            lines.append("Succeeded:")
            for r in succeeded:
                table_flag = " [TABLE]" if r.has_tables else ""
                lines.append(
                    f"  {r.filename:<45} {r.doc_type:<12} "
                    f"{r.page_count:>3}p  {r.chunk_count:>4} chunks{table_flag}"
                )

        if failed:
            lines.append("")
            lines.append("Failed:")
            for r in failed:
                lines.append(f"  {r.filename:<45}  ERROR: {r.error}")

        lines.append("=" * 70)
        return "\n".join(lines)
