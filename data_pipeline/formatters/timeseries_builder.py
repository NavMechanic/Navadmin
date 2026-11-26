"""N7 / N8 series → SI units, UTC timestamps, leak-free train/val/test Parquet splits.

* **Units** — each value column declares its unit in the registry (``timeseries.value_columns``).
  Values are converted with the shared SI registry (``rag.dq_gateway.units``) plus a few exact
  conversions for legacy engineering units (°R, psia, rpm, lbm/s, Ah, MW). Unknown units fail.
* **Time** — timestamps are parsed timezone-aware and converted to UTC. Naive timestamps are only
  accepted when the registry declares ``source_timezone``; otherwise the build fails. The output
  states ``time_standard: UTC`` and that the source clock is assumed NTP-disciplined.
* **Splits** — ``index_kind: timestamp``: contiguous time ranges (train < val < test, optional
  embargo gap). ``index_kind: sequence`` (cycle-indexed run-to-failure data such as C-MAPSS):
  whole entities per split, so no unit appears in two splits. Every split set is checked with
  ``policy_gates.check_temporal_split`` (HC-12) before anything is written.
* RAG/REFERENCE and LIVE-LOOKUP sources produce one ``series.parquet`` (no training splits).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from ..policy_gates import check_temporal_split
from ..provenance import record_hash
from .common import FormatError, finish_output, payload_files, prepare_output, provenance_meta

# exact conversions for units outside the SI-compatible registry: unit -> (canonical, factor, offset)
LEGACY_UNITS = {"degR": ("degC", 5 / 9, -273.15), "psia": ("Pa", 6894.757293168361, 0.0), "psi": ("Pa", 6894.757293168361, 0.0),
                "rpm": ("Hz", 1 / 60, 0.0), "lbm/s": ("kg/s", 0.45359237, 0.0), "Ah": ("C", 3600.0, 0.0),
                "mAh": ("C", 3.6, 0.0), "MW": ("W", 1e6, 0.0), "GW": ("W", 1e9, 0.0), "MWh": ("J", 3.6e9, 0.0),
                "m/s^2": ("m/s^2", 1.0, 0.0), "g0": ("m/s^2", 9.80665, 0.0)}


def unit_conversion(unit: str) -> tuple[str, float, float]:
    from rag.dq_gateway.units import UNITS
    if unit in UNITS:
        u = UNITS[unit]
        return u.canonical, u.factor, u.offset
    if unit in LEGACY_UNITS:
        return LEGACY_UNITS[unit]
    raise FormatError(f"unit {unit!r} has no SI conversion")


# ----------------------------------------------------------------------------------------------
# loading
# ----------------------------------------------------------------------------------------------
def _load_records(artifact_dir: Path, kind: Optional[str]) -> Optional[pd.DataFrame]:
    path = Path(artifact_dir) / "artifact" / "records.jsonl"
    if not path.exists():
        return None
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if kind == "fred_observation":
        df = pd.DataFrame([{"date": r.get("date"), "value": r.get("value")} for r in rows])
    elif kind == "bls_series":
        flat = []
        for series in rows:
            for d in series.get("data", []):
                period = str(d.get("period", ""))
                if period.startswith("M") and period != "M13":
                    flat.append({"series_id": series.get("seriesID"), "timestamp": f"{d['year']}-{period[1:]}-01T00:00:00Z",
                                 "value": d.get("value")})
        df = pd.DataFrame(flat)
    else:
        df = pd.DataFrame(rows)
    return df


def _load_tables(artifact_dir: Path, ts) -> pd.DataFrame:
    frames = []
    for p in payload_files(artifact_dir, (".csv", ".parquet", ".txt", ".mat")):
        if p.name in ("request.json",) or p.name.startswith("readme") or p.name.lower().startswith("readme"):
            continue
        rel = p.relative_to(Path(artifact_dir) / "artifact").as_posix()
        if p.suffix == ".mat":
            try:
                import scipy.io  # noqa: F401
            except ImportError:
                raise FormatError(f"{rel}: MATLAB files need scipy (pip install scipy)")
            raise FormatError(f"{rel}: MATLAB struct layouts need a source-specific adapter; export to CSV first")
        if p.suffix == ".parquet":
            df = pd.read_parquet(p)
        elif ts.delimiter == "whitespace":
            if not ts.columns:
                raise FormatError(f"{rel}: whitespace tables need timeseries.columns")
            df = pd.read_csv(p, sep=r"\s+", header=None, engine="python")
            df = df.iloc[:, :len(ts.columns)]
            df.columns = ts.columns[:df.shape[1]]
        else:
            if p.suffix == ".txt":
                continue
            df = pd.read_csv(p, low_memory=False)
        df["_file"] = rel
        frames.append(df)
    if not frames:
        raise FormatError("no tabular series files (CSV / Parquet / whitespace text) in the artifact")
    return pd.concat(frames, ignore_index=True)


def to_utc(series: pd.Series, source_tz: Optional[str]) -> pd.Series:
    parsed = pd.to_datetime(series, errors="coerce", utc=False, format="mixed")
    if parsed.isna().all():
        raise FormatError("timestamp column could not be parsed")
    if getattr(parsed.dt, "tz", None) is not None:
        return parsed.dt.tz_convert("UTC")
    if not source_tz:
        raise FormatError("naive timestamps and no timeseries.source_timezone declared; refusing to guess the zone")
    return parsed.dt.tz_localize(source_tz, ambiguous="NaT", nonexistent="NaT").dt.tz_convert("UTC")


# ----------------------------------------------------------------------------------------------
# splits
# ----------------------------------------------------------------------------------------------
def temporal_split(df: pd.DataFrame, fractions: dict, embargo: float) -> tuple[dict, dict]:
    times = np.sort(df["timestamp_utc"].dropna().unique())
    if len(times) < 3:
        raise FormatError("need at least 3 distinct timestamps to split")
    n = len(times)
    t_end = int(n * fractions.get("train", 0.7))
    v_end = int(n * (fractions.get("train", 0.7) + fractions.get("val", 0.15)))
    gap = int(n * embargo)
    bounds = {"train": (0, max(t_end, 1)), "val": (max(t_end, 1) + gap, max(v_end, t_end + 1 + gap)),
              "test": (max(v_end, t_end + 1 + gap) + gap, n)}
    parts, meta = {}, {}
    for name, (a, b) in bounds.items():
        if a >= b or a >= n:
            continue
        lo, hi = times[a], times[min(b, n) - 1]
        part = df[(df["timestamp_utc"] >= lo) & (df["timestamp_utc"] <= hi)]
        parts[name] = part
        meta[name] = {"start": pd.Timestamp(lo).isoformat(), "end": pd.Timestamp(hi).isoformat(), "rows": int(len(part))}
    return parts, meta


def group_split(df: pd.DataFrame, group: str, seq: Optional[str], fractions: dict) -> tuple[dict, dict]:
    groups = sorted(df[group].dropna().unique().tolist(), key=lambda g: (str(type(g)), g))
    n = len(groups)
    if n < 3:
        raise FormatError(f"need at least 3 distinct {group} values for an entity split")
    t_end = max(1, int(n * fractions.get("train", 0.7)))
    v_end = max(t_end + 1, int(n * (fractions.get("train", 0.7) + fractions.get("val", 0.15))))
    chunks = {"train": groups[:t_end], "val": groups[t_end:v_end], "test": groups[v_end:]}
    parts, meta = {}, {}
    for i, (name, gs) in enumerate(chunks.items()):
        if not gs:
            continue
        part = df[df[group].isin(gs)]
        parts[name] = part
        meta[name] = {"start": i * 2, "end": i * 2 + 1, "groups": [str(g) for g in gs], "rows": int(len(part)),
                      "group_column": group, "sequence_column": seq}
    return parts, meta


# ----------------------------------------------------------------------------------------------
def build(lake, spec, artifact_dir: Path) -> dict:
    if spec.timeseries is None:
        raise FormatError(f"{spec.source_id}: no timeseries config in the registry")
    ts = spec.timeseries
    manifest, out = prepare_output(lake, artifact_dir, spec)
    df = _load_records(artifact_dir, spec.record_kind)
    if df is None or df.empty:
        df = _load_tables(artifact_dir, ts)

    # values → SI
    units_si = {}
    for col, unit in ts.value_columns.items():
        if col not in df.columns:
            raise FormatError(f"declared value column {col!r} not in the data (columns: {list(df.columns)[:12]})")
        canonical, factor, offset = unit_conversion(unit)
        df[col] = pd.to_numeric(df[col], errors="coerce") * factor + offset
        units_si[col] = {"source_unit": unit, "si_unit": canonical}
    value_cols = list(ts.value_columns)
    dropped_na = int(df[value_cols].isna().all(axis=1).sum())
    df = df[~df[value_cols].isna().all(axis=1)].copy()

    # time → UTC
    if ts.index_kind == "timestamp":
        if not ts.timestamp_column or ts.timestamp_column not in df.columns:
            raise FormatError(f"timestamp column {ts.timestamp_column!r} missing")
        df["timestamp_utc"] = to_utc(df[ts.timestamp_column], ts.source_timezone)
        bad_ts = int(df["timestamp_utc"].isna().sum())
        df = df[df["timestamp_utc"].notna()].copy()
        keys = ([ts.group_column] if ts.group_column else []) + ["timestamp_utc"]
        df = df.drop_duplicates(subset=keys + value_cols)
        dup = df.duplicated(subset=keys, keep=False)
        if dup.any():
            raise FormatError(f"{int(dup.sum())} rows share a timestamp but disagree on values; refusing to pick one")
        df = df.sort_values(keys).reset_index(drop=True)
    else:
        bad_ts = 0
        if ts.group_column not in df.columns:
            raise FormatError(f"sequence data needs group column {ts.group_column!r}")
        df = df.sort_values([ts.group_column] + ([ts.sequence_column] if ts.sequence_column in df.columns else [])) \
            .reset_index(drop=True)

    reference_only = spec.intended_use in ("RAG/REFERENCE", "LIVE-LOOKUP")
    keep = [c for c in df.columns if c != "_file"]
    meta_doc = {"source_id": spec.source_id, "units_si": units_si, "time_standard": "UTC",
                "clock_source": "source-declared, assumed NTP-disciplined", "index_kind": ts.index_kind,
                "provenance": provenance_meta(manifest)}
    hashes = {record_hash(r) for r in df[keep].astype(str).head(1_000_000).to_dict("records")}
    counts = {"rows": int(len(df)), "dropped_all_null": dropped_na, "dropped_bad_timestamp": bad_ts}
    if reference_only:
        _write_parquet(out / "series.parquet", df[keep], meta_doc)
        return finish_output(out, manifest, "timeseries_builder", counts, hashes, {"units_si": units_si})

    if ts.index_kind == "timestamp":
        parts, split_meta = temporal_split(df, ts.splits, ts.embargo)
    else:
        parts, split_meta = group_split(df, ts.group_column, ts.sequence_column, ts.splits)
    check_temporal_split(split_meta)                      # HC-12 before anything is written
    for name, part in parts.items():
        _write_parquet(out / f"{name}.parquet", part[keep], {**meta_doc, "split": name, "split_bounds": split_meta[name]})
        counts[f"{name}_rows"] = int(len(part))
    (out / "splits.json").write_text(json.dumps(split_meta, indent=2, default=str), encoding="utf-8")
    return finish_output(out, manifest, "timeseries_builder", counts, hashes, {"units_si": units_si, "splits": split_meta})


def _write_parquet(path: Path, df: pd.DataFrame, meta: dict) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq
    table = pa.Table.from_pandas(df, preserve_index=False)
    existing = table.schema.metadata or {}
    table = table.replace_schema_metadata({**existing, b"athena": json.dumps(meta, default=str).encode()})
    pq.write_table(table, path)
