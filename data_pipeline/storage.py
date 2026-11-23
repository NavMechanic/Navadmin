"""Data-lake layout and mode isolation (HC-02).

    data_lake/
      train_candidates/<node>/<SRC>/<version>__<sha12>/   TRAIN-CANDIDATE
      rag_reference/<node>/<SRC>/...                      RAG/REFERENCE + LIVE-LOOKUP (live_cache/)
      eval_benchmarks/<node>/<SRC>/...                    EVAL/BENCHMARK
      eval_noncommercial/<node>/<SRC>/...                 NC / unasserted licenses (MVTec AD, AD 2, ...)
      private_ground_truth/<node>/<SRC>/<vault>/...       internal://athena/* vaults + synthetic fixtures
      _staging/  _governance/  _reports/  _exports/       pipeline-owned, never partitions

Every write path is resolved (symlinks included) and must stay inside the expected partition.
The lake root may not sit inside the production vector store (``rag/vectorstore``) or a Qdrant
persistence directory.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Iterable, Optional

from .policy_gates import (NODES, PARTITIONS, PolicyViolation, partition_for, safe_component, sanitize_tag,
                           TrainingRightsLedger)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTECTED = (REPO_ROOT / "rag" / "vectorstore", REPO_ROOT / ".qdrant_db", REPO_ROOT / "data" / "qdrant_db")


class DataLake:
    def __init__(self, root: str | Path = REPO_ROOT / "data_lake",
                 protected_paths: Iterable[str | Path] = DEFAULT_PROTECTED) -> None:
        self.root = Path(root).resolve()
        for p in protected_paths:
            prot = Path(p).resolve()
            if self.root.is_relative_to(prot) or prot.is_relative_to(self.root):
                raise PolicyViolation("HC-02", f"data lake {self.root} overlaps the production vector store {prot}")
        self.protected = tuple(Path(p).resolve() for p in protected_paths)
        self.ledger = TrainingRightsLedger(self.root / "_governance" / "training_rights_ledger.jsonl")

    # -- partitions ----------------------------------------------------------------------------
    def partition_root(self, partition: str) -> Path:
        if partition not in PARTITIONS:
            raise PolicyViolation("HC-02", f"unknown partition {partition!r}")
        return self.root / partition

    def source_dir(self, spec) -> Path:
        """``<partition>/<node>/<SRC>`` for a registry entry (created on demand)."""
        part = partition_for(spec)
        d = self.partition_root(part) / safe_component(spec.node_id, "node_id") / safe_component(spec.source_id, "source_id")
        return self.ensure_inside(d, self.partition_root(part))

    def version_dir(self, spec, version_tag: str, sha256: str) -> Path:
        return self.ensure_inside(self.source_dir(spec) / f"{sanitize_tag(version_tag)}__{sha256[:12]}",
                                  self.partition_root(partition_for(spec)))

    def ensure_inside(self, path: Path, base: Path) -> Path:
        """Resolve ``path`` (following any existing symlinks) and require it to stay under ``base``."""
        base_r = Path(base).resolve()
        resolved = Path(os.path.realpath(path))
        if resolved != base_r and not resolved.is_relative_to(base_r):
            raise PolicyViolation("HC-02", f"{path} resolves to {resolved}, outside {base_r}")
        for prot in self.protected:
            if resolved.is_relative_to(prot):
                raise PolicyViolation("HC-02", f"{path} resolves into the production vector store {prot}")
        return path

    def partition_of(self, path: Path) -> str:
        resolved = Path(os.path.realpath(path))
        if not resolved.is_relative_to(self.root):
            raise PolicyViolation("HC-02", f"{path} is outside the data lake")
        part = resolved.relative_to(self.root).parts[0] if resolved != self.root else ""
        if part not in PARTITIONS:
            raise PolicyViolation("HC-02", f"{path} is not inside a data partition")
        return part

    def node_of(self, path: Path) -> Optional[str]:
        rel = Path(os.path.realpath(path)).relative_to(self.root).parts
        return rel[1] if len(rel) > 1 and rel[1] in NODES else None

    # -- pipeline-owned areas ------------------------------------------------------------------
    def staging_dir(self, source_id: str) -> Path:
        base = self.root / "_staging"
        base.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=f"{safe_component(source_id, 'source_id')}-", dir=base))

    def reports_dir(self) -> Path:
        d = self.root / "_reports"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def exports_dir(self) -> Path:
        d = self.root / "_exports" / "training"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def promote(self, staging: Path, final: Path) -> bool:
        """Atomically move a staged artifact into place. Returns False if an identical one exists."""
        final.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_inside(final.parent, self.root)
        if final.exists():
            shutil.rmtree(staging, ignore_errors=True)
            return False
        tmp = final.parent / f".incoming-{uuid.uuid4().hex[:8]}"
        shutil.move(str(staging), str(tmp))
        os.replace(tmp, final)
        return True

    def artifact_dirs(self, partitions: Iterable[str] = PARTITIONS) -> list[Path]:
        out = []
        for part in partitions:
            base = self.root / part
            if base.exists():
                out += sorted(p.parent for p in base.rglob("provenance_manifest.json")
                              if "_rejected" not in p.parts and "formatted" not in p.parts)
        return out
