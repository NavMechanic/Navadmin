"""Document loading utilities for various file formats."""

import glob
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Document:
    content: str
    metadata: dict = field(default_factory=dict)


class DocumentLoader:
    """Loads documents from various file formats."""

    SUPPORTED_EXTENSIONS = {".pdf", ".md", ".txt", ".docx"}

    def load_pdf(self, path: str) -> list[Document]:
        """Load a PDF file page by page using pypdf."""
        from pypdf import PdfReader

        reader = PdfReader(path)
        total_pages = len(reader.pages)
        documents = []

        for page_number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            if text.strip():
                documents.append(
                    Document(
                        content=text,
                        metadata={
                            "source": os.path.abspath(path),
                            "page_number": page_number,
                            "total_pages": total_pages,
                        },
                    )
                )

        return documents

    def load_markdown(self, path: str) -> list[Document]:
        """Load a Markdown file as raw text."""
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        return [
            Document(
                content=content,
                metadata={"source": os.path.abspath(path)},
            )
        ]

    def load_text(self, path: str) -> list[Document]:
        """Load a plain text file."""
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        return [
            Document(
                content=content,
                metadata={"source": os.path.abspath(path)},
            )
        ]

    def load_docx(self, path: str) -> list[Document]:
        """Load a Word document using python-docx, extracting paragraphs."""
        from docx import Document as DocxDocument

        doc = DocxDocument(path)
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        content = "\n\n".join(paragraphs)

        return [
            Document(
                content=content,
                metadata={"source": os.path.abspath(path)},
            )
        ]

    def load_directory(
        self,
        directory: str,
        extensions: Optional[list[str]] = None,
    ) -> list[Document]:
        """Walk a directory and load all supported files."""
        if extensions is None:
            extensions = list(self.SUPPORTED_EXTENSIONS)

        # Normalize extensions to include leading dot
        extensions = [
            ext if ext.startswith(".") else f".{ext}" for ext in extensions
        ]

        documents = []
        for ext in extensions:
            pattern = os.path.join(directory, "**", f"*{ext}")
            for file_path in glob.glob(pattern, recursive=True):
                try:
                    loaded = self._load_file(file_path)
                    documents.extend(loaded)
                except Exception as e:
                    print(f"Warning: Failed to load {file_path}: {e}")

        return documents

    def _load_file(self, path: str) -> list[Document]:
        """Dispatch to the appropriate loader based on file extension."""
        ext = Path(path).suffix.lower()
        if ext == ".pdf":
            return self.load_pdf(path)
        elif ext == ".md":
            return self.load_markdown(path)
        elif ext == ".txt":
            return self.load_text(path)
        elif ext == ".docx":
            return self.load_docx(path)
        else:
            raise ValueError(f"Unsupported file extension: {ext}")
