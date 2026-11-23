"""Private ground-truth vaults: the 15 ``internal://athena/...`` sources.

Nothing here touches the network (``uses_network = False``; there is no HTTP client). For each
vault the fetcher:

* creates a secure drop zone ``private_ground_truth/<node>/<SRC>/<vault>/{inbox,accepted,rejected}``
  (mode 0700) with ``schema.json`` exported from the Pydantic intake model;
* validates ``inbox/*.jsonl`` record by record — one bad record rejects the whole file — and stages
  accepted files as PRIVATE_GROUND_TRUTH artifacts with provenance;
* can generate deterministic SYNTHETIC_TEST_CASE demo fixtures (``synthetic_fixtures/``), which are
  marked in every record and can never be exported to training (HC-09).
"""
from __future__ import annotations

import json
import os
import random
import shutil
import types
import typing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..policy_gates import PolicyViolation
from .base import BaseFetcher, FetchOutcome

_ID = r"^[A-Za-z0-9][A-Za-z0-9._\-]{2,63}$"
_SHA = r"^[0-9a-f]{64}$"
_CUR = r"^[A-Z]{3}$"


class _Rec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    synthetic: bool = Field(default=False, alias="_synthetic")

    @field_validator("*", mode="after")
    @classmethod
    def _utc(cls, v):
        if isinstance(v, datetime) and (v.tzinfo is None or v.utcoffset() != timedelta(0)):
            raise ValueError("timestamps must be timezone-aware UTC")
        return v


class StewardDecisionRecord(_Rec):
    decision_id: str = Field(pattern=_ID)
    source_id: str = Field(pattern=r"^SRC-\d{3}$")
    artifact_sha256: str = Field(pattern=_SHA)
    decision: Literal["APPROVED", "REJECTED", "PENDING"]
    steward_id: str = Field(pattern=r"^steward\.[a-z0-9.\-]+$")
    decided_at: datetime
    rationale: str = Field(min_length=10)


class RfqWinLossRecord(_Rec):
    rfq_id: str = Field(pattern=_ID)
    customer_segment: str
    country: str = Field(pattern=r"^[A-Z]{2}$")
    product_category: str
    quoted_value: Decimal = Field(ge=0)
    currency: str = Field(pattern=_CUR)
    outcome: Literal["WON", "LOST", "NO_BID"]
    decided_at: datetime
    loss_reason: Optional[str] = None


class PrdRequirementRecord(_Rec):
    prd_id: str = Field(pattern=_ID)
    revision: str
    product_id: str = Field(pattern=_ID)
    requirement_id: str = Field(pattern=_ID)
    text: str = Field(min_length=5)
    critical: bool
    verification_ids: list[str]
    approved_by: str
    approved_at: datetime


class DesignPackageRecord(_Rec):
    design_id: str = Field(pattern=_ID)
    revision: str
    product_id: str = Field(pattern=_ID)
    eda_tool: str
    native_file_sha256: str = Field(pattern=_SHA)
    layer_count: int = Field(ge=1, le=64)
    board_area_mm2: float = Field(gt=0)
    released_at: datetime


class DesignWaiverRecord(_Rec):
    waiver_id: str = Field(pattern=_ID)
    design_id: str = Field(pattern=_ID)
    check: Literal["ERC", "DRC"]
    rule: str
    severity: Literal["WARNING", "ERROR", "CRITICAL"]
    justification: str = Field(min_length=10)
    approved_by: str
    approved_at: datetime


class AoiDefectLabelRecord(_Rec):
    image_sha256: str = Field(pattern=_SHA)
    line_id: str
    station_id: str
    defect_class: Literal["open", "short", "mousebite", "spur", "copper", "pin-hole", "missing", "tombstone",
                          "solder_bridge", "insufficient_solder"]
    bbox_xyxy_px: tuple[int, int, int, int]
    labeled_by: str
    labeled_at: datetime


class TestStationRecord(_Rec):
    __test__ = False
    unit_id: str = Field(pattern=_ID)
    lot_id: str = Field(pattern=_ID)
    station_id: str
    test_code: str
    measured_value: float
    unit: str
    lower_limit: Optional[float] = None
    upper_limit: Optional[float] = None
    result: Literal["PASS", "FAIL"]
    tested_at: datetime


class SupplierQuoteRecord(_Rec):
    quote_id: str = Field(pattern=_ID)
    supplier_id: str = Field(pattern=_ID)
    mpn: str
    quantity: int = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    currency: str = Field(pattern=_CUR)
    valid_until: date
    received_at: datetime


class AvlEcoRecord(_Rec):
    eco_id: str = Field(pattern=_ID)
    product_id: str = Field(pattern=_ID)
    from_mpn: str
    to_mpn: str
    signed: bool
    approved_by: str
    effective_at: datetime


class BidOutcomeRecord(_Rec):
    bid_id: str = Field(pattern=_ID)
    tender_id: str
    buyer: str
    bid_price: Decimal = Field(ge=0)
    currency: str = Field(pattern=_CUR)
    outcome: Literal["WON", "LOST", "DISQUALIFIED", "CANCELLED"]
    awarded_price: Optional[Decimal] = Field(default=None, ge=0)
    decided_at: datetime


class ProjectActualRecord(_Rec):
    project_id: str = Field(pattern=_ID)
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    cost_category: Literal["bom", "nre", "tooling", "lab", "labour", "logistics", "finance", "other"]
    amount: Decimal
    currency: str = Field(pattern=_CUR)
    booked_at: datetime


class CashFlowRecord(_Rec):
    entry_id: str = Field(pattern=_ID)
    project_id: str = Field(pattern=_ID)
    direction: Literal["IN", "OUT"]
    amount: Decimal = Field(ge=0)
    currency: str = Field(pattern=_CUR)
    value_date: date
    counterparty_class: Literal["customer", "supplier", "lender", "government", "internal"]


class FieldReturnRecord(_Rec):
    rma_id: str = Field(pattern=_ID)
    unit_id: str = Field(pattern=_ID)
    lot_id: str = Field(pattern=_ID)
    failure_mode: str
    failure_code: str
    field_hours: float = Field(ge=0)
    returned_at: datetime
    root_cause: Optional[str] = None


class PlcTelemetryRecord(_Rec):
    unit_id: str = Field(pattern=_ID)
    lot_id: str = Field(pattern=_ID)
    station_id: str
    sensor_id: str
    measurement: str
    value: float
    unit: str
    event_time: datetime
    sensor_status: Literal["CALIBRATED", "UNCALIBRATED", "SIMULATED"]


class WarrantyClaimRecord(_Rec):
    claim_id: str = Field(pattern=_ID)
    unit_id: str = Field(pattern=_ID)
    claim_date: date
    symptom: str
    cost: Decimal = Field(ge=0)
    currency: str = Field(pattern=_CUR)
    country: str = Field(pattern=r"^[A-Z]{2}$")


VAULT_MODELS: dict[str, type[_Rec]] = {m.__name__: m for m in (
    StewardDecisionRecord, RfqWinLossRecord, PrdRequirementRecord, DesignPackageRecord, DesignWaiverRecord,
    AoiDefectLabelRecord, TestStationRecord, SupplierQuoteRecord, AvlEcoRecord, BidOutcomeRecord, ProjectActualRecord,
    CashFlowRecord, FieldReturnRecord, PlcTelemetryRecord, WarrantyClaimRecord)}
assert len(VAULT_MODELS) == 15


class VaultStubFetcher(BaseFetcher):
    access = ("vault",)
    fetch_method = "vault_intake"
    uses_network = False

    @property
    def model(self) -> type[_Rec]:
        try:
            return VAULT_MODELS[self.spec.vault.schema_model]
        except KeyError:
            raise PolicyViolation("HC-08", f"unknown vault schema {self.spec.vault.schema_model}", self.spec.source_id)

    def vault_dir(self) -> Path:
        return self.lake.source_dir(self.spec) / self.spec.vault.vault_name

    def ensure_stub(self) -> Path:
        v = self.vault_dir()
        for sub in ("inbox", "accepted", "rejected"):
            (v / sub).mkdir(parents=True, exist_ok=True)
            os.chmod(v / sub, 0o700)
        (v / "schema.json").write_text(json.dumps({"$id": self.spec.official_url, "title": self.model.__name__,
                                                   **self.model.model_json_schema(by_alias=True)}, indent=2))
        readme = v / "README.md"
        if not readme.exists():
            readme.write_text(f"# {self.spec.official_url}\n\nPrivate ground truth for {self.spec.name}.\n"
                              "Drop JSONL exports (one record per line) into `inbox/`; every record is validated "
                              "against `schema.json`. Never synced to or from the internet.\n", encoding="utf-8")
        return v

    def plan(self) -> dict:
        return {**super().plan(), "vault_uri": self.spec.official_url, "vault_dir": str(self.vault_dir()),
                "schema": self.spec.vault.schema_model, "network": False}

    def validate_file(self, path: Path) -> list[dict]:
        rows = []
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rec = self.model.model_validate_json(line)
            except ValidationError as exc:
                raise PolicyViolation("HC-08", f"{path.name}:{n} fails {self.model.__name__}: "
                                               f"{exc.errors(include_url=False)[:2]}", self.spec.source_id)
            if rec.synthetic:
                raise PolicyViolation("HC-09", f"{path.name}:{n} is a synthetic fixture record; it cannot enter "
                                               "the private ground truth", self.spec.source_id)
            rows.append(rec.model_dump(mode="json", by_alias=True, exclude={"synthetic"}))
        if not rows:
            raise PolicyViolation("HC-08", f"{path.name} has no records", self.spec.source_id)
        return rows

    def fetch(self) -> FetchOutcome:
        sid = self.spec.source_id
        try:
            self.preflight()
        except PolicyViolation as exc:
            return FetchOutcome(sid, "REJECTED", reasons=[str(exc)])
        if self.dry_run:
            return FetchOutcome(sid, "PLANNED", plan=self.plan())
        v = self.ensure_stub()
        files = sorted((v / "inbox").glob("*.jsonl"))
        if not files:
            return FetchOutcome(sid, "AWAITING_DROP", reasons=[f"vault stub ready: drop JSONL into {v / 'inbox'}"],
                                plan=self.plan())
        outs = [self.intake(p) for p in files]
        ok = [o for o in outs if o.status in ("STAGED", "UNCHANGED")]
        agg = ok[-1] if ok else outs[-1]
        agg.plan = {"accepted": len(ok), "rejected": [r for o in outs if o not in ok for r in o.reasons]}
        return agg

    def intake(self, path: Path) -> FetchOutcome:
        sid, v = self.spec.source_id, self.vault_dir()
        try:
            rows = self.validate_file(path)
        except PolicyViolation as exc:
            shutil.move(str(path), str(v / "rejected" / path.name))
            (v / "rejected" / f"{path.name}.reason.json").write_text(json.dumps({"code": exc.code, "reason": exc.message}))
            return FetchOutcome(sid, "REJECTED", reasons=[str(exc)])
        staging = self.lake.staging_dir(sid)
        try:
            payload = staging / "artifact"
            payload.mkdir()
            with open(payload / path.name, "w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, sort_keys=True) + "\n")
            out = self._finalize(staging, {"fetch_method": "vault_intake", "origin_type": "PRIVATE_GROUND_TRUTH",
                                           "version_tag": f"{self.spec.version_tag}-{path.stem}",
                                           "license": (self.spec.license_text, "registry:license_text")},
                                 v / "accepted")
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        if out.status in ("STAGED", "UNCHANGED"):
            path.unlink(missing_ok=True)
        return out

    # -- synthetic demo fixtures ---------------------------------------------------------------
    def generate_synthetic_fixtures(self, n: int = 20, seed: int = 7) -> FetchOutcome:
        """Deterministic SYNTHETIC_TEST_CASE records shaped like the vault schema (for demos and tests)."""
        rng = random.Random(f"{self.spec.source_id}:{seed}")
        rows = [synthetic_record(self.model, i, rng) for i in range(n)]
        for r in rows:
            self.model.model_validate(r)
        staging = self.lake.staging_dir(self.spec.source_id)
        try:
            payload = staging / "artifact"
            payload.mkdir()
            with open(payload / "fixtures.jsonl", "w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, sort_keys=True, default=str) + "\n")
            parent = self.ensure_stub() / "synthetic_fixtures"
            return self._finalize(staging, {"fetch_method": "synthetic_fixture", "origin_type": "SYNTHETIC_TEST_CASE",
                                            "version_tag": f"synthetic-seed{seed}-n{n}",
                                            "fetched_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                                            "license": ("SYNTHETIC TEST CASE generated by athena-data-pipeline. Not "
                                                        "real data; never field evidence or training data.",
                                                        "generator")}, parent)
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def _base_type(ann):
    """Unwrap Optional[...] / Annotated[...] to the underlying type."""
    args = [a for a in typing.get_args(ann) if a is not type(None)]
    origin = typing.get_origin(ann)
    if origin in (typing.Union, types.UnionType) and len(args) == 1:
        return _base_type(args[0])
    return ann


def synthetic_record(model: type[_Rec], i: int, rng: random.Random) -> dict:
    base_t = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i)
    out: dict = {"_synthetic": True}
    for name, field in model.model_fields.items():
        if name == "synthetic":
            continue
        t = _base_type(field.annotation)
        origin = typing.get_origin(t)
        meta = " ".join(str(m) for m in field.metadata)
        if origin is typing.Literal:
            out[name] = rng.choice(list(typing.get_args(t)))
        elif t is datetime:
            out[name] = base_t.isoformat()
        elif t is date:
            out[name] = (base_t.date() + timedelta(days=30)).isoformat()
        elif t is bool:
            out[name] = bool(i % 2)
        elif origin is tuple:
            x, y = rng.randint(0, 500), rng.randint(0, 500)
            out[name] = [x, y, x + rng.randint(5, 60), y + rng.randint(5, 60)]
        elif origin is list:
            out[name] = [f"VER-{i:04d}"]
        elif t is int:
            out[name] = rng.randint(1, 8) if name == "layer_count" else rng.randint(1, 5000)
        elif t in (float, Decimal):
            out[name] = round(rng.uniform(1, 1000), 2)
        elif name == "currency":
            out[name] = "INR"
        elif name == "country":
            out[name] = "IN"
        elif "0-9a-f]{64}" in meta:
            out[name] = f"{rng.getrandbits(256):064x}"
        elif name == "period":
            out[name] = f"2026-{(i % 12) + 1:02d}"
        elif name == "source_id":
            out[name] = "SRC-001"
        elif name == "steward_id":
            out[name] = "steward.n0-governance"
        elif name in ("rationale", "justification", "text"):
            out[name] = f"Synthetic demo {name} number {i:04d} for pipeline testing."
        else:
            out[name] = f"SYN-{name.upper().replace('_', '-')[:12]}-{i:04d}"
    return out
