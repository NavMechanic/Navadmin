"""The CPM engine must reproduce every number in the source HTML network."""
import json

import pytest

from app.programme import cpm
from app.programme.service import SEED_PLAN

from .conftest import FIXTURES

PLAN = json.loads(SEED_PLAN.read_text())["activities"]
EXPECTED = json.loads((FIXTURES / "source_cpm.json").read_text())
NODES = [cpm.Node(a["code"], tuple(a["predecessors"]), a["optimistic"], a["most_likely"], a["pessimistic"]) for a in PLAN]


def test_reproduces_every_source_value():
    sched = cpm.compute_schedule(NODES)
    assert len(sched.activities) == 26
    for code, exp in EXPECTED.items():
        a = sched.activities[code]
        for mine, theirs in ((a.duration, exp["pert"]), (a.variance, exp["var"]), (a.es, exp["es"]), (a.ef, exp["ef"]),
                             (a.ls, exp["ls"]), (a.lf, exp["lf"]), (a.total_float, max(0.0, exp["float"]))):
            assert mine == pytest.approx(theirs, abs=1e-6), code
        assert a.critical == (exp["path"] == "CRITICAL"), code


def test_summary_matches_source_headline_stats():
    s = cpm.compute_schedule(NODES).summary()
    assert s["expected_duration_days"] == pytest.approx(124.6667, abs=1e-4)
    assert s["expected_duration_weeks"] == 25
    assert s["critical_path"] == list("ABCEGHKMNRUVWXYZ")
    assert s["critical_sigma_days"] == pytest.approx(5.3697, abs=1e-4)
    assert s["p80_days_normal_approx"] == pytest.approx(124.6667 + 0.8416 * 5.3697, abs=1e-3)
    assert s["near_critical"] == ["D", "F", "O", "Q", "T"]


def test_link_classification_matches_source_notes():
    links = {(l["from"], l["to"]): l["kind"] for l in cpm.compute_schedule(NODES).links()}
    assert links[("A", "B")] == "critical"
    for slack_link in (("L", "M"), ("S", "U"), ("I", "P")):
        assert links[slack_link] == "non_driving"
    # The source's reading notes list J→P as a slack link, but by its own data J.EF == P.ES == 48.5,
    # so J→P is driving (P merely has 16.7 d total float); the source's own drawing code agrees.
    assert links[("J", "P")] == "driving"


def test_trace_upstream_and_downstream():
    assert cpm.trace(NODES, "C", "up") == ["A", "B"]
    assert cpm.trace(NODES, "X", "down") == ["Y", "Z"]
    assert set(cpm.trace(NODES, "L", "down")) >= {"M", "O", "T", "U", "Z"}


def test_actual_durations_shift_the_schedule():
    sched = cpm.compute_schedule(NODES, {"A": 8.0})
    assert sched.duration == pytest.approx(124.6667 + (8.0 - 31 / 6), abs=1e-4)
    assert sched.activities["A"].variance == 0.0


@pytest.mark.parametrize("nodes, message", [
    ([cpm.Node("A", ("B",), 1, 2, 3), cpm.Node("B", ("A",), 1, 2, 3)], "cycle"),
    ([cpm.Node("A", ("Q",), 1, 2, 3)], "unknown"),
    ([cpm.Node("A", ("A",), 1, 2, 3)], "itself"),
    ([cpm.Node("A", (), 3, 2, 1)], "o <= m <= p"),
])
def test_invalid_networks_are_rejected(nodes, message):
    with pytest.raises(cpm.NetworkError, match=message):
        cpm.compute_schedule(nodes)


def test_monte_carlo_is_reproducible_and_ordered():
    a = cpm.monte_carlo(NODES, iterations=500, seed=1)
    b = cpm.monte_carlo(NODES, iterations=500, seed=1)
    assert a == b
    assert a["p50_days"] <= a["p80_days"] <= a["p90_days"]
    assert a["criticality_index"]["A"] == 1.0 and a["criticality_index"]["Z"] == 1.0  # single start and end
    assert a["criticality_index"]["L"] < 0.05  # 30 days of float
