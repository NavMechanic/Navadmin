"""This repository vendors the N0 gateway (from ``athena_n0_starter``) and the N1 package (from
``athena_n1_integrated``). These tests fail as soon as a copy drifts: re-sync the file, or make the
change in the source repository and copy it into both integrated repositories. Skipped when this
directory is used on its own, outside the monorepo."""
from __future__ import annotations

import pytest

from conftest import ROOT

N0 = ROOT.parent / "athena_n0_starter"
N1 = ROOT.parent / "athena_n1_integrated"
OWN = {"schema_export.py", "main.py"}                 # combined entry point and exports are this repo's own


@pytest.mark.skipif(not (N0 / "app").is_dir(), reason="athena_n0_starter not present next to this repo")
def test_vendored_n0_gateway_matches_athena_n0_starter():
    drift = []
    for src in sorted((N0 / "app").glob("*.py")):
        if src.name == "schema_export.py":
            continue
        dst = ROOT / "app" / ("gateway.py" if src.name == "main.py" else src.name)
        if not dst.exists() or dst.read_bytes() != src.read_bytes():
            drift.append(f"{src.name} → app/{dst.name}")
    assert drift == [], f"vendored N0 differs from athena_n0_starter: {drift}"
    mig = "migrations/versions/0001_initial_n0_schema.py"
    assert (ROOT / mig).read_bytes() == (N0 / mig).read_bytes()


@pytest.mark.skipif(not (N1 / "app" / "n1").is_dir(), reason="athena_n1_integrated not present next to this repo")
def test_vendored_n1_package_matches_athena_n1_integrated():
    drift = [p.relative_to(N1).as_posix() for p in sorted((N1 / "app" / "n1").rglob("*"))
             if p.is_file() and "__pycache__" not in p.parts
             and (ROOT / p.relative_to(N1)).read_bytes() != p.read_bytes()]
    assert drift == [], f"vendored N1 differs from athena_n1_integrated: {drift}"
    for name in sorted((N1 / "app").glob("*.py")):
        if name.name not in OWN:
            assert (ROOT / "app" / name.name).read_bytes() == name.read_bytes(), name.name
    mig = "migrations/versions/0002_n1_market_intelligence_tables.py"
    assert (ROOT / mig).read_bytes() == (N1 / mig).read_bytes()
