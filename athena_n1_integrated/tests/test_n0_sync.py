"""The integrated repo carries a copy of the N0 gateway. This test fails as soon as the copy drifts
from ``athena_n0_starter``: re-sync the file (N0's ``main.py`` is ``app/gateway.py`` here) or move
the change into both. It is skipped when this directory is used on its own, outside the monorepo."""
from __future__ import annotations

import pytest

from conftest import ROOT

N0 = ROOT.parent / "athena_n0_starter"
OWN = {"schema_export.py"}                    # intentionally different: also exports the N1 schemas
RENAMED = {"main.py": "gateway.py"}


@pytest.mark.skipif(not (N0 / "app").is_dir(), reason="athena_n0_starter not present next to this repo")
def test_vendored_n0_gateway_matches_athena_n0_starter():
    drift = []
    for src in sorted((N0 / "app").glob("*.py")):
        if src.name in OWN:
            continue
        dst = ROOT / "app" / RENAMED.get(src.name, src.name)
        if not dst.exists() or dst.read_bytes() != src.read_bytes():
            drift.append(f"{src.name} → app/{dst.name}")
    assert drift == [], f"vendored N0 differs from athena_n0_starter: {drift}"
    mig = "migrations/versions/0001_initial_n0_schema.py"
    assert (ROOT / mig).read_bytes() == (N0 / mig).read_bytes()
