"""Extraction, and the refusal that defines this service.

``test_a_missing_parameter_is_never_defaulted`` is the assertion this whole
repository exists to protect.
"""
from __future__ import annotations

import io

import pytest

from app.extractors import documents as docs
from app.extractors.specs import (BY_KEY, PATTERNS, SAFETY_KEYS, Confidence,
                                  extract_all, extract_one, extract_pages,
                                  missing_report)

DATASHEET = """
AOD4184A  N-Channel Enhancement Mode Field Effect Transistor
Absolute Maximum Ratings
  Drain-Source Voltage        V(BR)DSS = 40 V
  Continuous Drain Current    ID  50 A
  Junction Temperature        TJ (max)  175 °C
Thermal Characteristics
  Thermal Resistance, Junction-to-Case     RthJC  1.8 °C/W
  Thermal Resistance, Junction-to-Ambient  RthJA  50 °C/W
Isolation
  Creepage distance: 3.2 mm
  Clearance distance: 2.6 mm
  Working voltage 250 V
Supply
  Lead time 12 weeks
"""

NO_THERMAL = """
ACME-CONN-8P Connector
8 position, 1.0 mm pitch, gold flash, RoHS compliant.
Rated current 2 A.
"""


class TestExtraction:
    def test_reads_labelled_values(self):
        found = extract_all(DATASHEET)
        assert found["rth_jc_k_per_w"].value == 1.8
        assert found["v_ds_max_v"].value == 40.0
        assert found["operating_temp_max_c"].value == 175.0
        assert found["creepage_mm"].value == 3.2
        assert found["lead_time_weeks"].value == 12.0

    def test_junction_to_case_is_not_confused_with_junction_to_ambient(self):
        """Both are 'thermal resistance' and they differ by a factor of 28."""
        found = extract_all(DATASHEET)
        assert found["rth_jc_k_per_w"].value == 1.8
        assert found["rth_ja_k_per_w"].value == 50.0

    def test_every_value_carries_its_evidence(self):
        for spec in extract_all(DATASHEET).values():
            assert spec.evidence, f"{spec.key} has no evidence string"
            assert str(spec.value).rstrip("0").rstrip(".") in spec.evidence.replace(
                " ", "") or str(spec.value) in spec.evidence

    def test_confidence_reflects_how_the_match_was_made(self):
        found = extract_all(DATASHEET)
        assert found["rth_jc_k_per_w"].confidence == Confidence.LABELLED_TEXT

    def test_the_theta_symbol_form_is_read(self):
        assert extract_one("θJC = 2.1 °C/W", BY_KEY["rth_jc_k_per_w"]).value == 2.1

    def test_non_breaking_spaces_do_not_defeat_the_match(self):
        text = "RthJC = 1.4 °C/W"
        assert extract_one(text, BY_KEY["rth_jc_k_per_w"]).value == 1.4


class TestTheRefusal:
    def test_a_missing_parameter_is_never_defaulted(self):
        """The bug this service was built to remove.

        The reference implementation did:
            rth_jc = float(m.group(1)) if m else 2.5   # Default fallback

        which turns a datasheet whose thermal resistance could not be parsed
        into a part with θJC = 2.5 K/W. That number then flows into Node 4's
        thermal twin and decides whether the adapter passes qualification, and
        nobody ever sees the regex that did not match.
        """
        found = extract_all(NO_THERMAL)
        assert "rth_jc_k_per_w" not in found
        assert not any(s.value == 2.5 for s in found.values())

    def test_the_absence_is_reported_with_its_consequence(self):
        report = missing_report(extract_all(NO_THERMAL),
                                requested=["rth_jc_k_per_w", "creepage_mm"])
        assert "rth_jc_k_per_w" in report["safety_relevant_missing"]
        assert report["complete"] is False
        assert any("Node 4" in c for c in report["consequences"])

    def test_an_implausible_value_is_refused_not_clamped(self):
        """A 900 K/W match has found the wrong number on the page."""
        assert extract_one("RthJC = 900 C/W", BY_KEY["rth_jc_k_per_w"]) is None
        assert extract_one("RthJC = 1.8 C/W", BY_KEY["rth_jc_k_per_w"]).value == 1.8

    def test_a_page_number_does_not_become_a_creepage_distance(self):
        assert extract_one("Page 12 of 40", BY_KEY["creepage_mm"]) is None

    def test_every_safety_parameter_has_a_plausible_range(self):
        for pattern in PATTERNS:
            if pattern.safety_relevant:
                lo, hi = pattern.plausible
                assert lo > -1e8 and hi < 1e8, (
                    f"{pattern.key} is safety-relevant with an unbounded range, so "
                    "any number on the page can become its value")

    def test_the_safety_set_is_not_empty(self):
        assert SAFETY_KEYS, "if nothing is safety-relevant, nothing is ever held"


class TestPages:
    def test_the_strongest_evidence_across_pages_wins(self):
        pages = ["thermal resistance is about 3 °C/W in typical use",
                 "RthJC = 1.8 °C/W"]
        found = extract_pages(pages)
        assert found["rth_jc_k_per_w"].value == 1.8
        assert found["rth_jc_k_per_w"].page == 2

    def test_the_page_number_is_recorded(self):
        found = extract_pages(["nothing here", "", "Creepage distance: 4.0 mm"])
        assert found["creepage_mm"].page == 3


class TestDocumentParsing:
    def test_csv_is_parsed_with_its_encoding_and_delimiter(self):
        blob = b"part,value\nAOD4184A,1.8\n"
        doc = docs.parse(blob, "specs.csv")
        assert doc.kind == "csv"
        assert doc.encoding == "utf-8"
        assert doc.tables[0]["row_count"] == 1

    def test_a_semicolon_csv_is_detected(self):
        blob = b"part;value;unit\nAOD4184A;1.8;C/W\nX;2;Y\n"
        doc = docs.parse(blob, "euro.csv")
        assert doc.delimiter == ";"

    def test_a_single_column_parse_is_flagged(self):
        blob = "a;b;c\n1;2;3\n".encode()
        doc = docs.parse(blob, "x.csv")
        assert doc.delimiter == ";" or any("delimiter" in w for w in doc.warnings)

    def test_an_empty_file_is_refused(self):
        with pytest.raises(docs.DocumentError, match="empty"):
            docs.parse(b"", "x.csv")

    def test_an_unsupported_type_is_refused_not_guessed(self):
        with pytest.raises(docs.DocumentError, match="Unsupported"):
            docs.parse(b"\x00\x01", "firmware.bin")

    def test_legacy_xls_is_refused_with_a_reason(self):
        with pytest.raises(docs.DocumentError, match="Legacy"):
            docs.parse(b"\xd0\xcf\x11\xe0", "old.xls")

    def test_text_is_parsed(self):
        doc = docs.parse(DATASHEET.encode(), "sheet.txt")
        assert doc.page_count == 1
        assert doc.char_count > 100

    def test_an_oversized_file_is_refused_before_parsing(self):
        with pytest.raises(docs.DocumentError, match="above the"):
            docs.parse(b"x" * (docs.MAX_BYTES + 1), "big.csv")


class TestVectorIndex:
    def test_chunks_carry_their_page(self):
        from app.services.vectors import chunk_pages
        chunks = chunk_pages("doc1", ["page one text", "page two text"])
        assert [c.page for c in chunks] == [1, 2]

    def test_long_pages_are_split_with_overlap(self):
        from app.services.vectors import CHUNK_CHARS, chunk_pages
        page = "\n".join(f"line {i} of a long datasheet page" for i in range(200))
        chunks = chunk_pages("doc1", [page])
        assert len(chunks) > 1
        assert all(len(c.text) <= CHUNK_CHARS + 50 for c in chunks)

    def test_retrieval_finds_the_right_passage(self):
        from app.services.vectors import VectorIndex, chunk_pages
        index = VectorIndex()
        index.add(chunk_pages("d1", ["Thermal Resistance Junction-to-Case RthJC 1.8"]))
        index.add(chunk_pages("d2", ["Packaging: reel of 3000, MSL 1, tape width 12mm"]))
        hits = index.search("RthJC thermal resistance", top_k=1)
        assert hits and hits[0]["document_id"] == "d1"

    def test_the_index_says_it_is_lexical_not_semantic(self):
        from app.services.vectors import get_index
        assert "lexical" in get_index().stats()["note"].lower()
