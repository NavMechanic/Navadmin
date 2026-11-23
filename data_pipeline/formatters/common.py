"""Shared helpers for the builders: input readers, the output location guard and the derived manifest.

Builder outputs go to ``<artifact_dir>/formatted/`` — the same partition as the source artifact,
so formatting can never move data across modes (HC-02). ``artifact/`` (the hashed payload) is
never modified; ``formatted/format_manifest.json`` records what was derived from which SHA-256.
"""
from __future__ import annotations

import csv
import io
import json
import re
import shutil
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from ..policy_gates import NONCOMMERCIAL_HARD_BLOCK, PolicyViolation, partition_for
from ..provenance import ProvenanceManifest, load_manifest, sha256_file, write_record_index

csv.field_size_limit(16 * 1024 * 1024)


class FormatError(Exception):
    pass


def prepare_output(lake, artifact_dir: Path, spec) -> tuple[ProvenanceManifest, Path]:
    """Validate the source artifact's location and return (manifest, empty formatted/ dir)."""
    artifact_dir = Path(artifact_dir)
    manifest = load_manifest(artifact_dir)
    if manifest.source_id != spec.source_id:
        raise PolicyViolation("HC-02", f"{artifact_dir} holds {manifest.source_id}, not {spec.source_id}")
    expected = partition_for(spec)
    located = lake.partition_of(artifact_dir)
    if located != expected:
        code = "HC-04" if spec.source_id in NONCOMMERCIAL_HARD_BLOCK else "HC-02"
        raise PolicyViolation(code, f"{spec.source_id} artifact is in {located}; builders only write inside {expected}")
    out = lake.ensure_inside(artifact_dir / "formatted", lake.partition_root(expected))
    if out.exists():
        shutil.rmtree(out)
    out.mkdir()
    return manifest, out


def finish_output(out: Path, manifest: ProvenanceManifest, builder: str, counts: dict, hashes: set[str],
                  extra: Optional[dict] = None) -> dict:
    write_record_index(out, hashes)
    outputs = [{"path": p.relative_to(out).as_posix(), "sha256": sha256_file(p), "bytes": p.stat().st_size}
               for p in sorted(out.rglob("*")) if p.is_file() and p.name != "format_manifest.json"]
    doc = {"builder": builder, "built_at_utc": datetime.now(timezone.utc).isoformat(),
           "derived_from": {"source_id": manifest.source_id, "sha256": manifest.sha256, "version_tag": manifest.version_tag},
           "intended_use": manifest.intended_use, "license_spdx_id": manifest.license_spdx_id,
           "origin_type": manifest.origin_type, "training_allowed": manifest.training_allowed,
           "commercial_training_allowed": manifest.commercial_training_allowed,
           "domain_transfer_warning": manifest.domain_transfer_warning, "counts": counts, "outputs": outputs,
           **(extra or {})}
    (out / "format_manifest.json").write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    return doc


def provenance_meta(manifest: ProvenanceManifest, **extra) -> dict:
    return {"source_id": manifest.source_id, "node_id": manifest.node_id, "artifact_sha256": manifest.sha256,
            "version_tag": manifest.version_tag, "intended_use": manifest.intended_use,
            "license_spdx_id": manifest.license_spdx_id, "origin_type": manifest.origin_type,
            "training_allowed": manifest.training_allowed, **extra}


# ----------------------------------------------------------------------------------------------
# readers
# ----------------------------------------------------------------------------------------------
def payload_files(artifact_dir: Path, suffixes: tuple[str, ...]) -> list[Path]:
    root = Path(artifact_dir) / "artifact"
    return sorted(p for p in root.rglob("*") if p.is_file() and p.name.lower().endswith(suffixes))


def parse_arff(text: str) -> list[dict]:
    attrs, rows, in_data = [], [], False
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("%"):
            continue
        low = s.lower()
        if low.startswith("@attribute"):
            m = re.match(r"@attribute\s+('([^']+)'|\"([^\"]+)\"|(\S+))", s, re.I)
            attrs.append(m.group(2) or m.group(3) or m.group(4))
        elif low.startswith("@data"):
            in_data = True
        elif in_data:
            vals = next(csv.reader([s], quotechar="'", skipinitialspace=True))
            rows.append(dict(zip(attrs, vals)))
    return rows


def iter_records(artifact_dir: Path) -> Iterator[tuple[str, dict]]:
    """Structured records from the payload: JSONL, JSON (list/dict), CSV, ARFF and requirement XML."""
    for p in payload_files(artifact_dir, (".jsonl", ".json", ".csv", ".arff", ".xml")):
        rel = p.relative_to(Path(artifact_dir) / "artifact").as_posix()
        if p.name == "request.json":
            continue
        low = p.name.lower()
        try:
            if low.endswith(".jsonl"):
                for line in p.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        yield rel, json.loads(line)
            elif low.endswith(".json"):
                data = json.loads(p.read_text(encoding="utf-8"))
                for rec in (data if isinstance(data, list) else [data]):
                    if isinstance(rec, dict):
                        yield rel, rec
            elif low.endswith(".csv"):
                with open(p, newline="", encoding="utf-8", errors="replace") as fh:
                    for rec in csv.DictReader(fh):
                        yield rel, rec
            elif low.endswith(".arff"):
                for rec in parse_arff(p.read_text(encoding="utf-8", errors="replace")):
                    yield rel, rec
            elif low.endswith(".xml"):
                yield from ((rel, r) for r in requirement_xml_records(p))
        except (json.JSONDecodeError, ET.ParseError, UnicodeDecodeError) as exc:
            raise FormatError(f"{rel}: cannot parse ({exc})")


def requirement_xml_records(path: Path) -> list[dict]:
    """PURE-style XML: any element whose tag contains 'req' with an id and text content."""
    tree = ET.parse(path)
    out = []
    for el in tree.iter():
        tag = el.tag.split("}")[-1].lower()
        if tag in ("req", "requirement"):
            text = " ".join(t.strip() for t in el.itertext() if t.strip())
            if text:
                out.append({"requirement_id": el.get("id") or el.get("ID") or f"{path.stem}-{len(out) + 1}", "text": text})
    return out


def document_texts(artifact_dir: Path) -> Iterator[tuple[str, str, dict]]:
    """(relpath, text, location) for unstructured documents: PDF pages, HTML, XML, TTL, TXT, MD, CSV/JSON."""
    from ..provenance import html_to_text
    for p in payload_files(artifact_dir, (".pdf", ".html", ".htm", ".xml", ".ttl", ".txt", ".md", ".json", ".jsonl",
                                          ".csv", ".schema.json", ".rst")):
        rel = p.relative_to(Path(artifact_dir) / "artifact").as_posix()
        low = p.name.lower()
        if p.name == "request.json":
            continue
        if low.endswith(".pdf"):
            try:
                from pypdf import PdfReader
            except ImportError as exc:  # pragma: no cover
                raise FormatError("pypdf is required for PDF text extraction") from exc
            reader = PdfReader(str(p))
            for i, page in enumerate(reader.pages):
                text = (page.extract_text() or "").strip()
                if text:
                    yield rel, text, {"page": i + 1}
        elif low.endswith((".html", ".htm")):
            yield rel, html_to_text(p.read_text(encoding="utf-8", errors="replace")), {}
        else:
            yield rel, p.read_text(encoding="utf-8", errors="replace"), {}


def chunk_text(text: str, size: int = 1500, overlap: int = 200) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) <= size:
        return [text] if text else []
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        cut = text.rfind("\n", start + size // 2, end) if end < len(text) else end
        cut = cut if cut > start else end
        chunks.append(text[start:cut].strip())
        if cut >= len(text):
            break
        start = max(cut - overlap, start + 1)
    return [c for c in chunks if c]


def get_path(obj: Any, path: str) -> Any:
    """Dot path with ``*`` fan-out over lists, e.g. ``authorships.*.countries``."""
    cur = [obj]
    for part in path.split("."):
        nxt = []
        for c in cur:
            if part == "*" and isinstance(c, list):
                nxt += c
            elif isinstance(c, dict) and part in c:
                nxt.append(c[part])
            elif isinstance(c, list) and part.isdigit() and int(part) < len(c):
                nxt.append(c[int(part)])
        cur = nxt
    if not cur:
        return None
    if "*" in path:
        flat = []
        for c in cur:
            flat += c if isinstance(c, list) else [c]
        return [x for x in flat if x is not None]
    return cur[0]
