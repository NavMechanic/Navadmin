"""Multi-level BOM: hierarchy, revision resolution, cost roll-up, cycle guard."""
from __future__ import annotations

from decimal import Decimal

import pytest

KIT = "KIT-NPA-01"
PACKED = ["NB-100-PKD", "PB-10K-PKD", "AD-20W-PKD"]


def test_kit_rolls_up_the_three_packed_skus(client, api, gatekeeper):
    """The hierarchy the specification names: KIT-NPA-01 over the three units."""
    exp = client.get(f"{api}/bom/{KIT}/explode", headers=gatekeeper).json()
    level1 = {n["part_number"] for n in exp["tree"]}
    assert set(PACKED) <= level1
    assert exp["max_level"] == 2, "the packed units must appear as sub-assemblies, not leaves"
    for node in exp["tree"]:
        if node["part_number"] in PACKED:
            assert node["children"], f"{node['part_number']} exploded to no children"


def test_cost_rolls_up_through_the_hierarchy(client, api, gatekeeper):
    """The kit's cost is the sum of its children's, computed not stored."""
    kit = Decimal(client.get(f"{api}/bom/{KIT}/explode",
                             headers=gatekeeper).json()["rolled_up_cost_usd"])
    children = sum(Decimal(client.get(f"{api}/bom/{p}/explode",
                                      headers=gatekeeper).json()["rolled_up_cost_usd"])
                   for p in PACKED)
    accessories = Decimal("0.45") + Decimal("0.20") + Decimal("0.05")
    assert kit == pytest.approx(children + accessories, abs=Decimal("0.01"))


def test_extended_quantity_multiplies_down_the_tree(client, api, gatekeeper):
    """A component at qty 2 inside a kit-level qty 1 unit is still qty 2."""
    exp = client.get(f"{api}/bom/{KIT}/explode", headers=gatekeeper).json()
    nb = next(n for n in exp["tree"] if n["part_number"] == "NB-100-PKD")
    driver = next(c for c in nb["children"] if c["part_number"] == "NB-DRV-10N")
    assert Decimal(driver["quantity_per_parent"]) == 2
    assert Decimal(driver["extended_quantity"]) == 2


def test_where_used_reaches_the_kit_two_levels_up(client, api, gatekeeper):
    """This is what makes ECN impact analysis correct rather than approximate."""
    rows = client.get(f"{api}/bom/PB-CEL-10K3S/where-used", headers=gatekeeper).json()
    by_part = {r["part_number"]: r for r in rows}
    assert by_part["PB-10K-PKD"]["level"] == 1
    assert by_part[KIT]["level"] == 2
    assert by_part[KIT]["path"] == "KIT-NPA-01 / PB-10K-PKD / PB-CEL-10K3S"


def test_cost_split_by_supply_mode(client, api, gatekeeper):
    out = client.get(f"{api}/bom/NB-100-PKD/cost", headers=gatekeeper).json()
    modes = out["by_supply_mode"]
    assert {"SKD_IMPORT", "LOCAL_KITTING"} <= set(modes)
    assert Decimal(out["rolled_up_cost_usd"]) == pytest.approx(
        sum(Decimal(v) for v in modes.values()), abs=Decimal("0.01"))


def test_safety_critical_lines_are_pinned_not_floating(client, api, gatekeeper):
    """A floating line would let a new revision enter a certified construction
    without anyone deciding to accept it."""
    lines = client.get(f"{api}/bom/parts/PB-10K-PKD/revisions/A/lines",
                       headers=gatekeeper).json()
    by_part = {ln["child_part_number"]: ln for ln in lines}
    assert by_part["PB-CEL-10K3S"]["revision_pinned"] is True
    assert by_part["PB-BMS-A05"]["revision_pinned"] is True
    # packaging floats — a new carton revision should propagate
    assert by_part["PB-BOX-01"]["revision_pinned"] is False


def test_released_structure_is_immutable(client, api, engineer):
    """Editing a released revision must be refused; that is what ECNs are for."""
    r = client.post(f"{api}/bom/parts/{KIT}/revisions/A/lines", headers=engineer,
                    json={"child_part_number": "KIT-CBL-C50", "quantity": 1,
                          "find_number": 99})
    assert r.status_code == 409
    assert "RELEASED" in r.json()["detail"]
    assert "ECN" in r.json()["detail"]


def test_cycle_is_refused(client, api, engineer):
    """A cyclic BOM is not a slow query, it is an infinite one."""
    client.post(f"{api}/bom/parts/KIT-NPA-01/revisions", headers=engineer,
                json={"rev": "B", "change_summary": "cycle probe"})
    r = client.post(f"{api}/bom/parts/KIT-NPA-01/revisions/B/lines", headers=engineer,
                    json={"child_part_number": "KIT-NPA-01", "quantity": 1})
    assert r.status_code == 409
    assert "cycle" in r.json()["detail"].lower()


def test_unknown_part_is_404_not_500(client, api, gatekeeper):
    assert client.get(f"{api}/bom/NOPE-1/explode", headers=gatekeeper).status_code == 404
    assert client.get(f"{api}/bom/NOPE-1/where-used", headers=gatekeeper).status_code == 404


def test_quantity_must_be_positive(client, api, engineer):
    r = client.post(f"{api}/bom/parts/KIT-NPA-01/revisions/B/lines", headers=engineer,
                    json={"child_part_number": "KIT-CBL-C50", "quantity": 0})
    assert r.status_code == 422
