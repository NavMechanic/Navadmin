"""Google Drive PDF sync with incremental indexing manifest.

The manifest is a JSON file stored at data/.drive_manifest.json by default.
Each entry tracks a Drive file by ID so re-runs skip already-indexed PDFs
and only process new or modified files.

Manifest schema::

    {
        "<drive_file_id>": {
            "filename": "battery_spec.pdf",
            "modified_time": "2024-01-15T10:30:00.000Z",
            "indexed_at": "2024-05-21T08:00:00+00:00",
            "chunk_count": 42,
            "folder_id": "1YU-..."
        },
        ...
    }
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rag.ingestion.pdf_pipeline import PDFIngestionResult, PDFPipeline


# ---------------------------------------------------------------------------
# Sync manifest
# ---------------------------------------------------------------------------

class DriveSyncManifest:
    """Persistent record of which Drive files have been indexed and when."""

    DEFAULT_PATH = "data/.drive_manifest.json"

    def __init__(self, path: str = DEFAULT_PATH) -> None:
        self._path = Path(path)
        self._data: dict = self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def save(self) -> None:
        """Persist manifest to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def is_current(self, file_id: str, modified_time: str) -> bool:
        """Return True if this file has already been indexed at the same version."""
        entry = self._data.get(file_id)
        if not entry:
            return False
        return entry.get("modified_time") == modified_time

    def get_entry(self, file_id: str) -> Optional[dict]:
        return self._data.get(file_id)

    def all_entries(self) -> list[dict]:
        return list(self._data.values())

    def total_chunks(self) -> int:
        return sum(e.get("chunk_count", 0) for e in self._data.values())

    # ------------------------------------------------------------------
    # Updates
    # ------------------------------------------------------------------

    def mark_indexed(
        self,
        file_id: str,
        filename: str,
        modified_time: str,
        chunk_count: int,
        folder_id: str = "",
    ) -> None:
        """Record that a Drive file has been indexed."""
        self._data[file_id] = {
            "filename": filename,
            "modified_time": modified_time,
            "indexed_at": datetime.now(timezone.utc).isoformat(),
            "chunk_count": chunk_count,
            "folder_id": folder_id,
        }
        self.save()

    def remove(self, file_id: str) -> None:
        self._data.pop(file_id, None)
        self.save()

    def clear(self) -> None:
        self._data = {}
        self.save()

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def status_report(self) -> str:
        """Human-readable sync status."""
        entries = self.all_entries()
        if not entries:
            return "Drive sync manifest is empty — no PDFs indexed yet."

        lines = [
            "=" * 70,
            f"Drive PDF Sync Status  ({len(entries)} files indexed)",
            f"Total chunks in index : {self.total_chunks()}",
            "=" * 70,
        ]
        for entry in sorted(entries, key=lambda e: e.get("filename", "")):
            indexed_at = entry.get("indexed_at", "")[:10]
            lines.append(
                f"  {entry['filename']:<50} "
                f"{entry.get('chunk_count', 0):>4} chunks  "
                f"indexed {indexed_at}"
            )
        lines.append("=" * 70)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Drive sync orchestrator
# ---------------------------------------------------------------------------

class DrivePDFSync:
    """Orchestrate incremental PDF sync from Google Drive Aviator folders.

    Usage::

        sync = DrivePDFSync(rag_pipeline, credentials_path="service_account.json")
        results = sync.sync_all_aviator()
        print(sync.manifest.status_report())

    Or for a single folder::

        results = sync.sync_folder(folder_id="1YU-...", folder_name="root")
    """

    AVIATOR_FOLDERS: dict[str, str] = {
        "root": "1YU-Jtitkx1IXWO1J_K_t1EBBtjb2JSE8",
        "smt": "19sRC9Kf2w9Zny-Gwh__I69Jpyisw0o7P",
        "drawings": "16QUsfsiZc2MhCyFMrdyHYLA_huW8bX61",
        "tester_log": "1xEGu4yJjDNrJ4cuyR-1ImJeCENFp3KTW",
    }

    def __init__(
        self,
        rag_pipeline,
        credentials_path: Optional[str] = None,
        manifest_path: str = DriveSyncManifest.DEFAULT_PATH,
    ) -> None:
        self._rag = rag_pipeline
        self._credentials = credentials_path or os.environ.get(
            "GOOGLE_APPLICATION_CREDENTIALS"
        )
        self.manifest = DriveSyncManifest(manifest_path)
        self._pdf_pipeline = PDFPipeline(rag_pipeline)

    # ------------------------------------------------------------------
    # Sync methods
    # ------------------------------------------------------------------

    def sync_folder(
        self,
        folder_id: str,
        folder_name: str = "",
    ) -> list[PDFIngestionResult]:
        """Sync all PDFs from a single Drive folder (incremental)."""
        print(f"\n[DrivePDFSync] Syncing folder: {folder_name or folder_id}")
        from rag.ingestion.drive_loader import DriveDocumentLoader

        loader = DriveDocumentLoader(credentials_path=self._credentials)

        try:
            file_list = loader.list_folder(folder_id, recursive=False)
        except Exception as exc:
            print(f"[DrivePDFSync] ERROR listing folder {folder_id}: {exc}")
            return []

        pdf_files = [f for f in file_list if f.mime_type == "application/pdf"]
        print(f"[DrivePDFSync] {len(pdf_files)} PDFs found")

        results: list[PDFIngestionResult] = []

        for file_info in pdf_files:
            if self.manifest.is_current(file_info.file_id, file_info.modified_time):
                print(f"  SKIP (up-to-date): {file_info.title}")
                continue

            print(f"  DOWNLOAD: {file_info.title}…")
            try:
                # Use a temp file so PDFExtractor can do section detection
                import tempfile as _tf
                from io import BytesIO
                from googleapiclient.http import MediaIoBaseDownload

                service = loader._get_service()
                request = service.files().get_media(fileId=file_info.file_id)
                buf = BytesIO()
                downloader = MediaIoBaseDownload(buf, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()

                with _tf.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp.write(buf.getvalue())
                    tmp_path = tmp.name

                result = self._pdf_pipeline.ingest_file(
                    tmp_path,
                    extra_metadata={
                        "drive_file_id": file_info.file_id,
                        "drive_folder_id": folder_id,
                        "aviator_folder": folder_name,
                        "modified_time": file_info.modified_time,
                        "original_filename": file_info.title,
                    },
                )
                # Override filename (temp path was used)
                result.filename = file_info.title

                os.unlink(tmp_path)

            except Exception as exc:
                result = PDFIngestionResult(
                    filename=file_info.title,
                    doc_type="general",
                    page_count=0, section_count=0, chunk_count=0,
                    has_tables=False, error=str(exc),
                )

            results.append(result)

            if result.success:
                self.manifest.mark_indexed(
                    file_id=file_info.file_id,
                    filename=file_info.title,
                    modified_time=file_info.modified_time,
                    chunk_count=result.chunk_count,
                    folder_id=folder_id,
                )
                print(
                    f"  OK: {file_info.title} — "
                    f"{result.page_count}p / {result.section_count}sec / "
                    f"{result.chunk_count} chunks"
                )
            else:
                print(f"  ERROR: {file_info.title} — {result.error}")

        return results

    def sync_all_aviator(
        self,
        folders: Optional[list[str]] = None,
    ) -> list[PDFIngestionResult]:
        """Sync all (or selected) Aviator Drive folders.

        Parameters
        ----------
        folders : list[str], optional
            Subset of folder names to sync (e.g. ["root", "smt"]).
            Defaults to all: root, smt, drawings, tester_log.
        """
        target_folders = {
            name: fid
            for name, fid in self.AVIATOR_FOLDERS.items()
            if folders is None or name in folders
        }

        all_results: list[PDFIngestionResult] = []
        for folder_name, folder_id in target_folders.items():
            results = self.sync_folder(folder_id, folder_name=folder_name)
            all_results.extend(results)

        return all_results

    def sync_local_mirror(
        self,
        base_dir: str = "data/aviator",
    ) -> list[PDFIngestionResult]:
        """Sync from a local directory mirror of the Drive folder structure.

        Expected layout::

            data/aviator/root/       – component datasheets
            data/aviator/smt/        – SMT datasheets
            data/aviator/drawings/   – DOI/SOP drawings
            data/aviator/tester_log/ – build logs

        This mode works without Google credentials and is useful for
        offline development or when PDFs have been pre-downloaded.
        """
        results: list[PDFIngestionResult] = []
        for folder_name in ("root", "smt", "drawings", "tester_log"):
            folder_path = Path(base_dir) / folder_name
            if not folder_path.is_dir():
                continue
            print(f"\n[DrivePDFSync] Local mirror — {folder_name}: {folder_path}")
            folder_results = self._pdf_pipeline.ingest_local_directory(str(folder_path))
            results.extend(folder_results)
        return results
