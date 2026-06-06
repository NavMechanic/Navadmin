"""Google Drive connector for the RAG pipeline.

Production usage requires google-api-python-client:
    pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib

For environments where Drive access is mediated by an MCP tool or proxy,
use DriveDocumentLoader.from_prefetched_texts() to ingest already-downloaded
text content without requiring a live API connection.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .loaders import Document, DocumentLoader

# ---------------------------------------------------------------------------
# Aviator folder IDs (Google Drive)
# ---------------------------------------------------------------------------

AVIATOR_FOLDER_ID = "1YU-Jtitkx1IXWO1J_K_t1EBBtjb2JSE8"
AVIATOR_SMT_FOLDER_ID = "19sRC9Kf2w9Zny-Gwh__I69Jpyisw0o7P"
AVIATOR_DRAWING_FOLDER_ID = "16QUsfsiZc2MhCyFMrdyHYLA_huW8bX61"
AVIATOR_TESTER_LOG_FOLDER_ID = "1xEGu4yJjDNrJ4cuyR-1ImJeCENFp3KTW"

ALL_AVIATOR_FOLDER_IDS: list[str] = [
    AVIATOR_FOLDER_ID,
    AVIATOR_SMT_FOLDER_ID,
    AVIATOR_TESTER_LOG_FOLDER_ID,
]

# MIME types that can be read as text by the Drive API
READABLE_MIME_TYPES: set[str] = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
    "text/plain",
}


@dataclass
class DriveFileInfo:
    """Metadata for a single Drive file."""

    file_id: str
    title: str
    mime_type: str
    parent_folder: str = ""
    file_size: Optional[int] = None
    created_time: str = ""
    modified_time: str = ""


class DriveDocumentLoader:
    """Load documents from Google Drive into the RAG pipeline.

    Two operating modes:

    1. **Live API** (production):  Requires GOOGLE_APPLICATION_CREDENTIALS or
       OAuth token.  Call ``load_folder(folder_id)`` to stream files directly.

    2. **Pre-fetched text** (MCP / offline): Call
       ``from_prefetched_texts(texts)`` to ingest text already extracted from
       Drive files without needing a live API connection.
    """

    def __init__(self, credentials_path: Optional[str] = None) -> None:
        self._credentials_path = credentials_path or os.environ.get(
            "GOOGLE_APPLICATION_CREDENTIALS"
        )
        self._service = None  # lazy-init on first use

    # ------------------------------------------------------------------
    # Production path (google-api-python-client)
    # ------------------------------------------------------------------

    def _get_service(self):
        if self._service is not None:
            return self._service
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            scopes = ["https://www.googleapis.com/auth/drive.readonly"]
            if self._credentials_path:
                creds = service_account.Credentials.from_service_account_file(
                    self._credentials_path, scopes=scopes
                )
            else:
                import google.auth

                creds, _ = google.auth.default(scopes=scopes)
            self._service = build("drive", "v3", credentials=creds)
        except ImportError as exc:
            raise RuntimeError(
                "google-api-python-client is not installed. "
                "Run: pip install google-api-python-client google-auth-oauthlib"
            ) from exc
        return self._service

    def list_folder(self, folder_id: str, recursive: bool = True) -> list[DriveFileInfo]:
        """Return all readable files in a Drive folder (optionally recursive)."""
        service = self._get_service()
        files: list[DriveFileInfo] = []
        page_token: Optional[str] = None

        while True:
            query = (
                f"'{folder_id}' in parents and trashed = false"
            )
            response = (
                service.files()
                .list(
                    q=query,
                    pageSize=100,
                    fields="nextPageToken, files(id, name, mimeType, size, createdTime, modifiedTime)",
                    pageToken=page_token,
                )
                .execute()
            )
            for item in response.get("files", []):
                mime = item["mimeType"]
                if mime == "application/vnd.google-apps.folder":
                    if recursive:
                        files.extend(self.list_folder(item["id"], recursive=True))
                elif mime in READABLE_MIME_TYPES:
                    files.append(
                        DriveFileInfo(
                            file_id=item["id"],
                            title=item["name"],
                            mime_type=mime,
                            parent_folder=folder_id,
                            file_size=int(item.get("size", 0) or 0),
                            created_time=item.get("createdTime", ""),
                            modified_time=item.get("modifiedTime", ""),
                        )
                    )
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return files

    def download_as_text(self, file_info: DriveFileInfo) -> str:
        """Export/download a Drive file and return its plain text."""
        service = self._get_service()
        from io import BytesIO

        google_export_map = {
            "application/vnd.google-apps.document": "text/plain",
            "application/vnd.google-apps.spreadsheet": "text/csv",
            "application/vnd.google-apps.presentation": "text/plain",
        }
        if file_info.mime_type in google_export_map:
            request = service.files().export_media(
                fileId=file_info.file_id,
                mimeType=google_export_map[file_info.mime_type],
            )
        else:
            request = service.files().get_media(fileId=file_info.file_id)

        buf = BytesIO()
        from googleapiclient.http import MediaIoBaseDownload

        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        raw = buf.getvalue()
        # For PDFs / binary Office docs: save to a temp file and use DocumentLoader
        if file_info.mime_type in {
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }:
            import tempfile

            suffix = {
                "application/pdf": ".pdf",
                "application/msword": ".doc",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            }.get(file_info.mime_type, ".bin")

            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(raw)
                tmp_path = tmp.name

            loader = DocumentLoader()
            try:
                if suffix == ".pdf":
                    docs = loader.load_pdf(tmp_path)
                elif suffix in {".doc", ".docx"}:
                    docs = loader.load_docx(tmp_path)
                else:
                    docs = loader.load_text(tmp_path)
                return "\n\n".join(d.content for d in docs)
            finally:
                os.unlink(tmp_path)

        return raw.decode("utf-8", errors="replace")

    def load_folder(self, folder_id: str, recursive: bool = True) -> list[Document]:
        """Load all readable files from a Drive folder as Document objects."""
        file_list = self.list_folder(folder_id, recursive=recursive)
        documents: list[Document] = []
        for info in file_list:
            try:
                text = self.download_as_text(info)
                if text.strip():
                    documents.append(
                        Document(
                            content=text,
                            metadata={
                                "source": info.title,
                                "drive_file_id": info.file_id,
                                "drive_folder_id": info.parent_folder,
                                "mime_type": info.mime_type,
                                "created_time": info.created_time,
                                "modified_time": info.modified_time,
                                "category": "google_drive",
                            },
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                print(f"[DriveLoader] Skipping {info.title!r}: {exc}")
        return documents

    # ------------------------------------------------------------------
    # Pre-fetched / offline path
    # ------------------------------------------------------------------

    @classmethod
    def from_prefetched_texts(
        cls,
        texts: list[dict],
    ) -> list[Document]:
        """Convert pre-fetched Drive text content into Document objects.

        Each dict in *texts* must have:
          - ``content`` (str): the extracted text
          - ``metadata`` (dict, optional): any additional metadata fields
            such as ``source``, ``drive_file_id``, ``category``.

        Example::

            docs = DriveDocumentLoader.from_prefetched_texts([
                {
                    "content": "Battery spec text …",
                    "metadata": {
                        "source": "battery.docx",
                        "drive_file_id": "1ylULhcBMH3jEa5fqEQXDZiH52nTq8XBa",
                        "category": "component_spec",
                    },
                },
            ])
        """
        documents: list[Document] = []
        for item in texts:
            content = item.get("content", "")
            if not content.strip():
                continue
            meta = dict(item.get("metadata", {}))
            meta.setdefault("category", "google_drive")
            documents.append(Document(content=content, metadata=meta))
        return documents

    # ------------------------------------------------------------------
    # Convenience: load from a local directory that was synced from Drive
    # ------------------------------------------------------------------

    def load_local_mirror(
        self,
        directory: str,
        category: str = "google_drive",
        extensions: Optional[list[str]] = None,
    ) -> list[Document]:
        """Load files from a local directory that mirrors a Drive folder.

        Useful when Drive files have already been downloaded to disk.
        Uses DocumentLoader internally for format-aware parsing.
        """
        loader = DocumentLoader()
        docs = loader.load_directory(directory, extensions=extensions)
        for doc in docs:
            doc.metadata.setdefault("category", category)
            doc.metadata["drive_synced"] = True
        return docs


class AviatorDriveLoader(DriveDocumentLoader):
    """Pre-configured DriveDocumentLoader for the Aviator project folders.

    Aviator folder structure on Google Drive:
      aviator/           (ID: 1YU-Jtitkx1IXWO1J_K_t1EBBtjb2JSE8)
      ├── aviator smt/   (ID: 19sRC9Kf2w9Zny-Gwh__I69Jpyisw0o7P)  – SMT datasheets
      ├── aviator drawing/ (ID: 16QUsfsiZc2MhCyFMrdyHYLA_huW8bX61) – DOI & SOP
      └── tester log/    (ID: 1xEGu4yJjDNrJ4cuyR-1ImJeCENFp3KTW)  – test logs & summary

    Content types:
      - 46 PDFs in root: component datasheets (battery, cables, frame, chips …)
      - 23 PDFs in smt: SMT component datasheets (IC, passives)
      - Build summary XLSX + .log files in tester_log
    """

    FOLDER_IDS: dict[str, str] = {
        "root": AVIATOR_FOLDER_ID,
        "smt": AVIATOR_SMT_FOLDER_ID,
        "tester_log": AVIATOR_TESTER_LOG_FOLDER_ID,
    }

    def load_all_aviator(self) -> list[Document]:
        """Load documents from all aviator Drive folders."""
        all_docs: list[Document] = []
        for folder_name, folder_id in self.FOLDER_IDS.items():
            docs = self.load_folder(folder_id, recursive=False)
            for doc in docs:
                doc.metadata["aviator_folder"] = folder_name
            all_docs.extend(docs)
            print(f"[AviatorDriveLoader] {folder_name}: loaded {len(docs)} documents")
        return all_docs

    def load_local_aviator_mirror(self, base_dir: str = "data/aviator") -> list[Document]:
        """Load from a local mirror of the aviator Drive folder.

        Expected structure:
          data/aviator/root/       – component datasheets
          data/aviator/smt/        – SMT datasheets
          data/aviator/tester_log/ – logs and build summary
        """
        all_docs: list[Document] = []
        for subfolder in ("root", "smt", "tester_log"):
            path = os.path.join(base_dir, subfolder)
            if not os.path.isdir(path):
                continue
            docs = self.load_local_mirror(path, category=f"aviator_{subfolder}")
            print(f"[AviatorDriveLoader] local mirror {subfolder}: {len(docs)} documents")
            all_docs.extend(docs)
        return all_docs
