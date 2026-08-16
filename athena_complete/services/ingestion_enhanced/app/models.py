"""Storage for ingested documents and the parameters read out of them.

Two tables and one rule: a stored parameter always knows where it came from.

``ExtractedSpec`` carries ``evidence``, ``page`` and ``confidence`` alongside the
value, not as decoration but because the alternative is a table of numbers
nobody can audit. Six months after ingestion, "θJC = 1.8" is a claim; "θJC = 1.8,
read from 'Thermal Resistance, Junction-to-Case RthJC 1.8 °C/W' on page 3 of
AOD4184A.pdf, labelled-text match" is a fact someone can check in ten seconds.

``status`` on the document is the other half of the same idea. A document whose
safety-relevant parameters could not be extracted is ``HELD``, not ``PUBLISHED`` —
it is in the system, it is visible, and nothing downstream consumes it until a
person looks.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy import (Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index,
                        Integer, String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


JSONDict = JSON()


class DocStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PARSING = "PARSING"
    PARSED = "PARSED"
    HELD = "HELD"              # parsed, but safety parameters are missing
    PUBLISHED = "PUBLISHED"    # released to the platform
    FAILED = "FAILED"
    NEEDS_OCR = "NEEDS_OCR"


class Document(Base):
    __tablename__ = "ingest_document"
    __table_args__ = (
        # Content hash AND part number, not content alone. A generic datasheet
        # covering a family of parts is legitimately uploaded once per part, and
        # keying dedup on bytes alone silently attaches the second upload to the
        # first part — the parameters land under a part number nobody declared.
        UniqueConstraint("content_sha256", "part_no", name="uq_document_sha_part"),
        Index("ix_document_part", "part_no"),
        Index("ix_document_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    # SHA-256 of the bytes. Re-uploading the same file returns the existing
    # record instead of re-extracting it — an engineer who uploads a datasheet
    # twice should not get two sets of parameters that may disagree.
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    content_type: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))

    part_no: Mapped[str | None] = mapped_column(String(64), index=True)
    component_type: Mapped[str | None] = mapped_column(String(64))
    supplier: Mapped[str | None] = mapped_column(String(120))
    sku: Mapped[str | None] = mapped_column(String(32), index=True)
    revision: Mapped[str | None] = mapped_column(String(32))

    status: Mapped[str] = mapped_column(String(16), default=DocStatus.RECEIVED,
                                        index=True)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    needs_ocr: Mapped[bool] = mapped_column(Boolean, default=False)

    uploaded_by: Mapped[str | None] = mapped_column(String(64))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  default=utcnow, index=True)
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    task_id: Mapped[str | None] = mapped_column(String(64))

    warnings: Mapped[list[str]] = mapped_column(JSONDict, default=list)
    missing_report: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str | None] = mapped_column(Text)

    specs: Mapped[list["ExtractedSpec"]] = relationship(
        back_populates="document", cascade="all, delete-orphan")


class ExtractedSpec(Base):
    """One parameter, with the evidence it was read from."""
    __tablename__ = "ingest_spec"
    __table_args__ = (
        UniqueConstraint("document_id", "key", name="uq_spec_doc_key"),
        Index("ix_spec_part_key", "part_no", "key"),
        CheckConstraint("confidence IN ('LABELLED_TABLE','LABELLED_TEXT',"
                        "'UNITED_TEXT','WEAK','MANUAL')",
                        name="ck_spec_confidence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("ingest_document.id", ondelete="CASCADE"), index=True)
    part_no: Mapped[str | None] = mapped_column(String(64), index=True)

    key: Mapped[str] = mapped_column(String(48), index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[str] = mapped_column(String(20))
    evidence: Mapped[str] = mapped_column(Text)
    page: Mapped[int | None] = mapped_column(Integer)
    safety_relevant: Mapped[bool] = mapped_column(Boolean, default=False)

    # Set when a human corrects an extraction. The original is never edited —
    # the same reasoning as Node 5's genealogy.
    superseded_by: Mapped[int | None] = mapped_column(Integer)
    corrected_by: Mapped[str | None] = mapped_column(String(64))
    correction_reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)

    document: Mapped[Document] = relationship(back_populates="specs")


__all__ = ["DocStatus", "Document", "ExtractedSpec", "utcnow"]
