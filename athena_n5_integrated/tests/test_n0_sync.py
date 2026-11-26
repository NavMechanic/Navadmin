"""This repository vendors N0 (from ``athena_n0_starter``), N1 (from ``athena_n1_integrated``), the N2 /
N3 packages (from ``athena_n3_integrated``) and N4 / N8 (from ``athena_n4_integrated``). These tests
fail as soon as a copy drifts: change the source repository and copy the file into every integrated
repository. Skipped when this directory is used on its own, outside the monorepo."""
from __future__ import annotations

import pytest

from conftest import ROOT

N0 = ROOT.parent / "athena_n0_starter"
N1 = ROOT.parent / "athena_n1_integrated"
N3 = ROOT.parent / "athena_n3_integrated"
N4 = ROOT.parent / "athena_n4_integrated"


def _drift(src_dir, dst_dir) -> list[str]:
    return [p.relative_to(src_dir).as_posix() for p in sorted(src_dir.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts
            and (not (dst_dir / p.relative_to(src_dir)).exists()
                 or (dst_dir / p.relative_to(src_dir)).read_bytes() != p.read_bytes())]


@pytest.mark.skipif(not (N0 / "app").is_dir(), reason="athena_n0_starter not present next to this repo")
def test_vendored_n0_gateway_matches_athena_n0_starter():
    drift = [s.name for s in sorted((N0 / "app").glob("*.py")) if s.name != "schema_export.py"
             and (ROOT / "app" / ("gateway.py" if s.name == "main.py" else s.name)).read_bytes() != s.read_bytes()]
    assert drift == [], f"vendored N0 differs from athena_n0_starter: {drift}"
    mig = "migrations/versions/0001_initial_n0_schema.py"
    assert (ROOT / mig).read_bytes() == (N0 / mig).read_bytes()


@pytest.mark.skipif(not (N1 / "app" / "n1").is_dir(), reason="athena_n1_integrated not present next to this repo")
def test_vendored_n1_matches_athena_n1_integrated():
    assert _drift(N1 / "app" / "n1", ROOT / "app" / "n1") == []
    mig = "migrations/versions/0002_n1_market_intelligence_tables.py"
    assert (ROOT / mig).read_bytes() == (N1 / mig).read_bytes()


@pytest.mark.skipif(not (N3 / "app" / "n3").is_dir(), reason="athena_n3_integrated not present next to this repo")
def test_vendored_n2_n3_match_athena_n3_integrated():
    for pkg in ("n2", "n3"):
        assert _drift(N3 / "app" / pkg, ROOT / "app" / pkg) == [], pkg
    mig = "migrations/versions/0003_n2_n3_design_tables.py"
    assert (ROOT / mig).read_bytes() == (N3 / mig).read_bytes()


@pytest.mark.skipif(not (N4 / "app" / "n4").is_dir(), reason="athena_n4_integrated not present next to this repo")
def test_vendored_n4_n8_match_athena_n4_integrated():
    for pkg in ("n4", "n8"):
        assert _drift(N4 / "app" / pkg, ROOT / "app" / pkg) == [], pkg
    mig = "migrations/versions/0004_n4_n8_verification_tables.py"
    assert (ROOT / mig).read_bytes() == (N4 / mig).read_bytes()
