"""The exact bytes an ingestion path hashes, shared by the RAG pipeline and the ``dq-manifest`` CLI.

A producer's ``output_file_sha256`` must be computed over the same bytes the consumer reads, or
DQ05 quarantines the artifact. A single file is its own bytes; a directory is the deterministic
``tree_bytes`` listing (sorted relpath + SHA-256 per file) restricted to the files that path loads.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .manifest import sha256_bytes

DOC_EXTENSIONS = {".pdf", ".md", ".txt", ".docx"}   # DocumentLoader.SUPPORTED_EXTENSIONS
LOG_EXTENSIONS = {".log", ".txt"}
EXTENSIONS_BY_KIND: dict[str, Optional[set[str]]] = {
    "document": DOC_EXTENSIONS, "telemetry_log": LOG_EXTENSIONS, "fea_mesh": None, "cad": None, "pdf": {".pdf"},
}
AVIATOR_MIRROR_SUBFOLDERS = ("root", "smt", "tester_log")   # what AviatorDriveLoader.load_local_aviator_mirror reads


def bundle_bytes(root: Path, paths: list[Path]) -> tuple[bytes, dict[str, bytes]]:
    files = {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(paths)}
    listing = "".join(f"{name}\t{sha256_bytes(data)}\n" for name, data in sorted(files.items()))
    return listing.encode(), files


def read_source(source: str | Path, extensions: Optional[set[str]]) -> tuple[bytes, dict[str, bytes]]:
    """(content, files): a file's bytes, or a directory's tree listing plus relpath → bytes."""
    p = Path(source)
    if p.is_file():
        data = p.read_bytes()
        return data, {p.name: data}
    return bundle_bytes(p, [f for f in p.rglob("*")
                            if f.is_file() and (extensions is None or f.suffix.lower() in extensions)])


def read_kind(source: str | Path, kind: str) -> tuple[bytes, dict[str, bytes]]:
    if kind == "drive_mirror":
        return aviator_mirror_bundle(source)
    if kind not in EXTENSIONS_BY_KIND:
        raise ValueError(f"unknown source kind {kind!r}; expected one of {sorted(EXTENSIONS_BY_KIND) + ['drive_mirror']}")
    return read_source(source, EXTENSIONS_BY_KIND[kind])


def aviator_mirror_bundle(base_dir: str | Path) -> tuple[bytes, dict[str, bytes]]:
    base = Path(base_dir)
    paths = [f for sub in AVIATOR_MIRROR_SUBFOLDERS if (base / sub).is_dir()
             for f in (base / sub).rglob("*") if f.is_file() and f.suffix.lower() in DOC_EXTENSIONS]
    return bundle_bytes(base, paths)
