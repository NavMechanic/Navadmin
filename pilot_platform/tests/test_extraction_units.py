import pytest

from app.ai.evals import evaluate_extraction
from app.ai.extraction import extract_rfq
from app.contracts.units import UnitError, parse_quantity, parse_temperature


@pytest.mark.parametrize("text, value, unit", [
    ("150 mA", 0.15, "A"), ("3V3", 3.3, "V"), ("1.2 kW", 1200.0, "W"), ("85 mm", 0.085, "m"),
    ("2.4 GHz", 2.4e9, "Hz"), ("4k7", 4700.0, "ohm"), ("250 g", 0.25, "kg"), ("1,500 W", 1500.0, "W"),
])
def test_parse_quantity(text, value, unit):
    q = parse_quantity(text)
    assert q.value == pytest.approx(value) and q.unit == unit


def test_parse_quantity_errors_and_temperature():
    with pytest.raises(UnitError):
        parse_quantity("fast")
    with pytest.raises(UnitError):
        parse_quantity("5 V", expect="A")
    assert parse_temperature("185 F").value == pytest.approx(85.0)
    assert parse_temperature("−40 °C").value == -40


def test_gold_corpus_meets_gate_threshold():
    report = evaluate_extraction(threshold=0.9)
    assert report["passed"], report["per_case"]
    assert report["fields_total"] >= 60


def test_conflicting_labels_are_flagged_not_guessed():
    out = extract_rfq("Supply voltage: 5 V\nSupply voltage: 12 V\nQuantity: 100\nOperating temperature: 0 to 40 C")
    assert [c["field"] for c in out["conflicts"]] == ["supply_voltage"]
    assert "supply_voltage" in out["low_confidence"] and out["needs_review"] is True


def test_missing_required_and_schema_errors():
    out = extract_rfq("Quantity: 100\nOperating temperature: 60 to 10 C")
    assert "supply_voltage" in out["missing_required"]
    assert out["schema_errors"] == ["operating_temp_min must be below operating_temp_max"]


def test_provenance_spans_point_into_the_source_text():
    text = "Customer: Acme\nSupply voltage: 24 V\nQuantity: 50 units\nOperating temperature: -10 to 50 °C"
    out = extract_rfq(text)
    for f in out["fields"].values():
        src = f["source"]
        assert text[src["start"]:src["end"]] == src["text"] and src["text"]
    assert out["fields"]["supply_voltage"]["confidence"] == 0.95


def test_bare_unanchored_values_are_low_confidence():
    out = extract_rfq("Board runs at 5 V. Quantity: 10. Operating temperature: 0 to 40 C")
    assert out["fields"]["supply_voltage"]["confidence"] == 0.5
    assert "supply_voltage" in out["low_confidence"]
