"""Manual / licensed drop-zone intake (India CPPP SRC-013/055, GeM SRC-059, IEC/UL/IS standards SRC-027,
TI/ST/Infineon datasheets SRC-050, MVTec AD / AD 2 SRC-038/039).

These sources are never scraped. A steward places each file in::

    <partition>/<node>/<SRC>/dropzone/inbox/<file>
    <partition>/<node>/<SRC>/dropzone/inbox/<file>.entitlement.json

and the adapter verifies the file's SHA-256, the entitlement record (source, steward, validity
window, permitted use, no training grant) and the file type, then stages one artifact per file
with the entitlement in its provenance manifest. Failures move to ``rejected/`` with a reason file.
This class has no HTTP client at all.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..policy_gates import PolicyViolation, sanitize_tag
from ..provenance import sha256_file
from .base import BaseFetcher, FetchOutcome, mime_compatible, safe_extract, sniff_mime

_EXT_MIME = {".pdf": ["application/pdf"], ".html": ["text/html"], ".htm": ["text/html"], ".csv": ["text/csv", "text/plain"],
             ".json": ["application/json"], ".zip": ["application/zip"], ".xlsx": ["application/zip"],
             ".tar": ["application/x-tar"], ".tar.xz": ["application/x-xz"], ".xz": ["application/x-xz"]}
_ARCHIVES = (".zip", ".tar", ".tar.xz", ".xz", ".tar.gz")


class Entitlement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(pattern=r"^SRC-\d{3}$")
    file_name: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entitlement_id: str = Field(min_length=3, max_length=96)
    licensee: str = Field(min_length=2)
    license_type: Literal["purchased-single-user", "purchased-multi-user", "site-license", "public-portal-terms",
                          "vendor-download-terms", "research-license"]
    permitted_uses: list[Literal["RAG/REFERENCE", "EVAL/BENCHMARK"]] = Field(min_length=1)
    valid_from: datetime
    valid_until: Optional[datetime] = None
    acquired_via: Literal["manual-download", "purchase", "portal-export"]
    acquired_by: str = Field(min_length=2)
    steward_id: str
    license_snapshot_text: str = Field(min_length=20)
    training_permitted: Literal[False] = False

    @field_validator("valid_from", "valid_until")
    @classmethod
    def _utc(cls, v):
        if v is not None and (v.tzinfo is None or v.utcoffset() != timedelta(0)):
            raise ValueError("entitlement dates must be timezone-aware UTC")
        return v


def _suffix(name: str) -> str:
    low = name.lower()
    for ext in sorted(_EXT_MIME, key=len, reverse=True):
        if low.endswith(ext):
            return ext
    return Path(low).suffix


class DropZoneIntake(BaseFetcher):
    access = ("drop_zone",)
    fetch_method = "manual_drop_zone"
    uses_network = False

    # -- layout --------------------------------------------------------------------------------
    def zone(self) -> Path:
        return self.lake.source_dir(self.spec) / "dropzone"

    def ensure_zone(self) -> Path:
        z = self.zone()
        for sub in ("inbox", "accepted", "rejected"):
            (z / sub).mkdir(parents=True, exist_ok=True)
        readme = z / "README.md"
        if not readme.exists():
            readme.write_text(
                f"# Manual intake — {self.spec.source_id} {self.spec.name}\n\n"
                "This source is never scraped. Place each licensed file in `inbox/` together with\n"
                "`<file>.entitlement.json` (schema: `entitlement.schema.json`). The SHA-256 in the\n"
                "entitlement must match the file. Accepted files are staged with full provenance.\n", encoding="utf-8")
            (z / "entitlement.schema.json").write_text(json.dumps(Entitlement.model_json_schema(), indent=2))
        return z

    def plan(self) -> dict:
        inbox = self.zone() / "inbox"
        waiting = [p.name for p in inbox.iterdir() if not p.name.endswith(".entitlement.json")] if inbox.exists() else []
        return {**super().plan(), "drop_zone": str(self.zone()), "files_waiting": waiting, "network": False}

    # -- intake --------------------------------------------------------------------------------
    def validate(self, path: Path) -> Entitlement:
        s = self.spec
        exts = [e.lower() for e in s.drop_zone.accepted_extensions]
        if not any(path.name.lower().endswith(e) for e in exts):
            raise PolicyViolation("HC-05", f"{path.name}: extension not accepted for {s.source_id} ({exts})", s.source_id)
        side = path.with_name(path.name + ".entitlement.json")
        if not side.exists():
            raise PolicyViolation("HC-05", f"{path.name}: no entitlement metadata ({side.name})", s.source_id)
        try:
            ent = Entitlement.model_validate_json(side.read_bytes())
        except ValidationError as exc:
            raise PolicyViolation("HC-05", f"{side.name}: invalid entitlement: {exc.errors(include_url=False)[:2]}",
                                  s.source_id)
        if ent.source_id != s.source_id:
            raise PolicyViolation("HC-05", f"entitlement is for {ent.source_id}, not {s.source_id}", s.source_id)
        if ent.file_name != path.name:
            raise PolicyViolation("HC-05", f"entitlement names {ent.file_name}, not {path.name}", s.source_id)
        if ent.steward_id != s.data_steward:
            raise PolicyViolation("HC-05", f"entitlement signed by {ent.steward_id}, steward is {s.data_steward}", s.source_id)
        now = self.now()
        if ent.valid_from > now or (ent.valid_until is not None and ent.valid_until < now):
            raise PolicyViolation("HC-05", f"entitlement {ent.entitlement_id} is not valid at {now.isoformat()}", s.source_id)
        if s.intended_use not in ent.permitted_uses:
            raise PolicyViolation("HC-05", f"entitlement does not permit {s.intended_use}", s.source_id)
        digest = sha256_file(path)
        if digest != ent.sha256:
            raise PolicyViolation("HC-05", f"{path.name}: SHA-256 {digest} does not match the entitlement {ent.sha256}",
                                  s.source_id)
        with open(path, "rb") as fh:
            mime = sniff_mime(fh.read(8192), path.name)
        expected = _EXT_MIME.get(_suffix(path.name), [])
        if expected and not mime_compatible(mime, expected):
            raise PolicyViolation("HC-11", f"{path.name} looks like {mime}, not {expected}", s.source_id)
        return ent

    def fetch(self) -> FetchOutcome:
        sid = self.spec.source_id
        try:
            self.preflight()
        except PolicyViolation as exc:
            return FetchOutcome(sid, "REJECTED", reasons=[str(exc)])
        if self.dry_run:
            return FetchOutcome(sid, "PLANNED", plan=self.plan())
        zone = self.ensure_zone()
        files = sorted(p for p in (zone / "inbox").iterdir() if p.is_file() and not p.name.endswith(".entitlement.json"))
        if not files:
            return FetchOutcome(sid, "AWAITING_DROP", reasons=[f"place licensed files in {zone / 'inbox'}"], plan=self.plan())
        outcomes = [self.intake(p) for p in files]
        staged = [o for o in outcomes if o.status in ("STAGED", "UNCHANGED")]
        agg = staged[-1] if staged else outcomes[-1]
        agg.plan = {"accepted": [o.artifact_dir.name for o in staged if o.artifact_dir],
                    "rejected": [r for o in outcomes if o.status not in ("STAGED", "UNCHANGED") for r in o.reasons]}
        return agg

    def intake(self, path: Path) -> FetchOutcome:
        sid = self.spec.source_id
        zone = self.zone()
        try:
            ent = self.validate(path)
        except PolicyViolation as exc:
            dest = zone / "rejected" / path.name
            shutil.move(str(path), str(dest))
            side = path.with_name(path.name + ".entitlement.json")
            if side.exists():
                shutil.move(str(side), str(zone / "rejected" / side.name))
            (zone / "rejected" / f"{path.name}.reason.json").write_text(json.dumps({"code": exc.code, "reason": exc.message}))
            return FetchOutcome(sid, "REJECTED", reasons=[str(exc)])
        staging = self.lake.staging_dir(sid)
        try:
            payload = staging / "artifact"
            payload.mkdir()
            shutil.copy2(path, payload / path.name)
            if path.name.lower().endswith(_ARCHIVES):
                safe_extract(payload / path.name, payload / f"{path.name}.d", self.max_bytes)
            info = {"fetch_method": "manual_drop_zone", "version_tag": f"manual-{sanitize_tag(ent.entitlement_id)}",
                    "license": (ent.license_snapshot_text, f"entitlement:{ent.entitlement_id}"),
                    "entitlement": ent.model_dump(mode="json", exclude={"license_snapshot_text"})}
            out = self._finalize(staging, info)
        except PolicyViolation as exc:
            return FetchOutcome(sid, "REJECTED", reasons=[str(exc)])
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        if out.status in ("STAGED", "UNCHANGED"):
            shutil.move(str(path), str(zone / "accepted" / path.name))
            side = path.with_name(path.name + ".entitlement.json")
            shutil.move(str(side), str(zone / "accepted" / side.name))
        return out
