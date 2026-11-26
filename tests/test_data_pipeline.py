"""Positive-path tests for the data pipeline: registry, fetchers, provenance sidecars, formatters, CLI.

All network access is mocked; the negative mutation cases live in test_dq_adversarial.py.
"""
from __future__ import annotations

import io
import json
import tarfile
from collections import Counter
from datetime import timedelta
from pathlib import Path

import httpx
import numpy as np
import pyarrow.parquet as pq
import pytest
import yaml
from jsonschema import Draft202012Validator
from PIL import Image

from data_pipeline.fetchers import (VAULT_MODELS, ApiFetcher, DropZoneIntake, GitFetcher, HttpArchiveFetcher,
                                    VaultStubFetcher)
from data_pipeline.formatters import FormatError, build_for
from data_pipeline.policy_gates import (API_KEY_LIVE_LOOKUPS, DOMAIN_BOUNDARY_SOURCES, DOMAIN_TRANSFER_WARNING,
                                        DROP_ZONE_ONLY, PolicyViolation, evaluate_artifact)
from data_pipeline.provenance import load_manifest
from data_pipeline.storage import DataLake

from _dp_support import NO_WAIT, NOW, REGISTRY, MockWeb, add_formatted, approve, drop, license_page, manifest_sha, stage


@pytest.fixture
def lake(tmp_path):
    return DataLake(tmp_path / "data_lake")


def kw(web=None, **extra):
    return {"client": (web or MockWeb()).client(), "sleep": lambda s: None, "retries": 0, "rate_limiter": NO_WAIT, **extra}


# ----------------------------------------------------------------------------------------------
# registry
# ----------------------------------------------------------------------------------------------
def test_registry_shape_and_hard_coded_guardrails():
    assert len(REGISTRY) == 75
    parts = Counter(s.partition for s in REGISTRY)
    assert parts["private_ground_truth"] == 15
    for sid in ("SRC-038", "SRC-039"):
        s = REGISTRY.get(sid)
        assert s.partition == "eval_noncommercial" and s.node_id == "N4" and s.commercial_training_allowed is False
    for sid in API_KEY_LIVE_LOOKUPS:
        s = REGISTRY.get(sid)
        assert s.intended_use == "LIVE-LOOKUP" and s.training_allowed is False and s.api.ttl_seconds
        assert s.partition == "rag_reference"
    for sid in DOMAIN_BOUNDARY_SOURCES:
        assert REGISTRY.get(sid).domain_transfer_warning == DOMAIN_TRANSFER_WARNING
    for sid in DROP_ZONE_ONLY:
        assert REGISTRY.get(sid).access == "drop_zone"
    assert all(s.official_url.startswith("internal://athena/") for s in REGISTRY if s.access == "vault")
    sel = REGISTRY.select(nodes=["N4"], priorities=["P0"])
    assert {s.source_id for s in sel} == {"SRC-036", "SRC-042", "SRC-043"}


# ----------------------------------------------------------------------------------------------
# fetchers + provenance
# ----------------------------------------------------------------------------------------------
def schema_web():
    doc = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://example.org/s", "type": "object",
           "properties": {"a": {"type": "integer"}}}
    return MockWeb({"https://json-schema.org/draft/": httpx.Response(200, json=doc),
                    "https://json-schema.org/": license_page("Academic Free License v3.0 — text")})


def test_http_archive_stages_with_sidecars_and_is_idempotent(lake):
    out = HttpArchiveFetcher(REGISTRY.get("SRC-001"), lake, REGISTRY, **kw(schema_web())).fetch()
    assert out.status == "STAGED", out.reasons
    d = out.artifact_dir
    assert d.parent == lake.root / "train_candidates" / "N0" / "SRC-001"
    m = load_manifest(d)
    assert m.training_rights_approval == "NOT_REVIEWED" and m.origin_type == "PUBLIC_OPEN"
    assert "Academic Free License" in m.license_snapshot_text and m.transport["tls_verified"] is True
    assert len(m.artifact_files) == 3
    prov = json.loads((d / "prov.jsonld").read_text())
    assert any(n["@type"] == "prov:Entity" and n.get("athena:sha256") == m.sha256 for n in prov["@graph"])
    spdx = json.loads((d / "spdx.json").read_text())
    assert spdx["spdxVersion"] == "SPDX-2.3" and spdx["packages"][0]["checksums"][0]["checksumValue"] == m.sha256
    bom = json.loads((d / "cyclonedx.json").read_text())
    assert bom["specVersion"] == "1.6" and bom["components"][0]["type"] == "data"
    again = HttpArchiveFetcher(REGISTRY.get("SRC-001"), lake, REGISTRY, **kw(schema_web())).fetch()
    assert again.status == "UNCHANGED" and again.artifact_dir == d


def test_retry_honours_retry_after(lake):
    hits = {"n": 0}

    def flaky(request):
        hits["n"] += 1
        return httpx.Response(503, headers={"Retry-After": "2"}) if hits["n"] == 1 else httpx.Response(200, json={"$schema": "x"})

    slept = []
    web = MockWeb({"https://json-schema.org/draft/": flaky, "https://json-schema.org/": license_page()})
    f = HttpArchiveFetcher(REGISTRY.get("SRC-001"), lake, REGISTRY, **kw(web, retries=2, sleep=slept.append))
    assert f.fetch().status == "STAGED" and slept[0] == 2.0


def test_dry_run_plans_without_network_or_writes(lake):
    web = MockWeb()
    out = HttpArchiveFetcher(REGISTRY.get("SRC-001"), lake, REGISTRY, dry_run=True, **kw(web)).fetch()
    assert out.status == "PLANNED" and out.plan["partition"] == "train_candidates" and web.calls == []
    assert not (lake.root / "train_candidates").exists()


def test_git_clone_is_shallow_https_only_and_strips_git(lake):
    calls = []

    def runner(cmd, **k):
        calls.append(cmd)
        if "clone" in cmd:
            dest = Path(cmd[-1])
            (dest / ".git").mkdir(parents=True)
            (dest / "LICENSE.md").write_text("CC-BY-SA-4.0 with the KiCad libraries exception")
            (dest / "Device.kicad_sym").write_text("(kicad_symbol_lib (version 1))")
        out = "0123456789abcdef0123456789abcdef01234567\n" if "rev-parse" in cmd else ""
        return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()

    f = GitFetcher(REGISTRY.get("SRC-023"), lake, REGISTRY, runner=runner, **kw())
    out = f.fetch()
    assert out.status == "STAGED", out.reasons
    clone = calls[0]
    for flag in ("--depth", "--single-branch", "protocol.allow=never", "protocol.https.allow=always",
                 "http.sslVerify=true", "core.symlinks=false"):
        assert flag in clone
    assert clone[clone.index("--branch") + 1] == "8.0.6"
    m = load_manifest(out.artifact_dir)
    assert m.version_tag == "8.0.6+g0123456789ab" and "KiCad libraries exception" in m.license_snapshot_text
    assert not (out.artifact_dir / "artifact" / ".git").exists()
    assert m.commercial_training_allowed is False                     # share-alike: needs legal review


def test_git_tarball_mode_extracts_safely(lake):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in (("specification-1.6/LICENSE", b"Apache License 2.0"), ("specification-1.6/schema/bom-1.6.schema.json", b"{}")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    spec = REGISTRY.get("SRC-004")
    spec = spec.model_copy(update={"git": spec.git.model_copy(update={"mode": "tarball"})})
    web = MockWeb({"https://github.com/CycloneDX/specification/archive/refs/tags/1.6.tar.gz":
                   httpx.Response(200, content=buf.getvalue())})
    out = GitFetcher(spec, lake, REGISTRY, **kw(web)).fetch()
    assert out.status == "STAGED", out.reasons
    assert (out.artifact_dir / "artifact" / "schema" / "bom-1.6.schema.json").exists()


def test_api_pagination_post_body(lake):
    pages = []

    def usa(request):
        body = json.loads(request.content)
        pages.append(body["page"])
        return httpx.Response(200, json={"results": [{"Award ID": f"A-{body['page']}", "Award Amount": 1000.5}]})

    web = MockWeb({"https://api.usaspending.gov/": usa, "https://www.usaspending.gov/about": license_page("public domain")})
    out = ApiFetcher(REGISTRY.get("SRC-012"), lake, REGISTRY, **kw(web)).fetch()
    assert out.status == "STAGED", out.reasons and pages == [1, 2]
    lines = (out.artifact_dir / "artifact" / "records.jsonl").read_text().splitlines()
    assert len(lines) == 2 and load_manifest(out.artifact_dir).origin_type == "LIVE_API"


def test_oauth_graphql_live_lookup_and_ttl_cache(lake):
    tokens = []

    def token(request):
        tokens.append(request.content)
        return httpx.Response(200, json={"access_token": "tok-abcdef123456", "expires_in": 3600})

    def gql(request):
        assert request.headers["Authorization"] == "Bearer tok-abcdef123456"
        assert json.loads(request.content)["variables"] == {"q": "TPS62840DLCR"}
        return httpx.Response(200, json={"data": {"supSearchMpn": {"results": [{"part": {"mpn": "TPS62840DLCR"}}]}}})

    web = MockWeb({"https://identity.nexar.com/connect/token": token, "https://api.nexar.com/graphql": gql,
                   "https://nexar.com/terms-of-service": license_page("Nexar API terms")})
    env = {"NEXAR_CLIENT_ID": "client-id-0001", "NEXAR_CLIENT_SECRET": "client-secret-0001"}
    f = ApiFetcher(REGISTRY.get("SRC-045"), lake, REGISTRY, env=env, **kw(web))
    first = f.lookup({"q": "TPS62840DLCR"})
    assert not first.from_cache and first.records[0]["part"]["mpn"] == "TPS62840DLCR"
    assert first.artifact_dir.is_relative_to(lake.root / "rag_reference" / "N5" / "SRC-045" / "live_cache")
    m = load_manifest(first.artifact_dir)
    assert m.training_allowed is False and m.cache_expires_at_utc > NOW and m.fetch_method == "api_live_lookup"
    n = len(web.calls)
    assert f.lookup({"q": "TPS62840DLCR"}).from_cache and len(web.calls) == n
    on_disk = b"".join(p.read_bytes() for p in lake.root.rglob("*") if p.is_file())
    assert b"tok-abcdef123456" not in on_disk and b"client-secret-0001" not in on_disk
    assert evaluate_artifact(lake, first.artifact_dir, REGISTRY, list(env.values())).ok


def test_drop_zone_intake_accepts_hash_and_entitlement(lake):
    drop(lake, "SRC-027", "IEC_62368-1_2023.pdf", b"%PDF-1.7 licensed standard copy")
    out = DropZoneIntake(REGISTRY.get("SRC-027"), lake, REGISTRY).fetch()
    assert out.status == "STAGED", out.reasons
    m = load_manifest(out.artifact_dir)
    assert m.fetch_method == "manual_drop_zone" and m.entitlement["entitlement_id"] == "PO-2026-0042"
    assert "no AI training" in m.license_snapshot_text and m.training_allowed is False
    zone = DropZoneIntake(REGISTRY.get("SRC-027"), lake, REGISTRY).zone()
    assert (zone / "accepted" / "IEC_62368-1_2023.pdf").exists() and not list((zone / "inbox").iterdir())
    assert evaluate_artifact(lake, out.artifact_dir, REGISTRY).ok


def test_vault_stubs_intake_and_fixtures_for_all_15(lake):
    assert len(VAULT_MODELS) == 15
    for spec in (s for s in REGISTRY if s.access == "vault"):
        f = VaultStubFetcher(spec, lake, REGISTRY)
        assert f.fetch().status == "AWAITING_DROP"
        schema = json.loads((f.vault_dir() / "schema.json").read_text())
        assert schema["$id"] == spec.official_url
        fx = f.generate_synthetic_fixtures(4)
        assert fx.status == "STAGED", (spec.source_id, fx.reasons)
        assert load_manifest(fx.artifact_dir).origin_type == "SYNTHETIC_TEST_CASE"
    f = VaultStubFetcher(REGISTRY.get("SRC-015"), lake, REGISTRY)
    rec = {"rfq_id": "RFQ-1001", "customer_segment": "automotive", "country": "IN", "product_category": "BMS",
           "quoted_value": "125000.00", "currency": "INR", "outcome": "WON", "decided_at": "2026-02-01T00:00:00Z"}
    (f.vault_dir() / "inbox" / "2026-q1.jsonl").write_text(json.dumps(rec) + "\n")
    out = f.fetch()
    assert out.status == "STAGED" and load_manifest(out.artifact_dir).origin_type == "PRIVATE_GROUND_TRUTH"
    assert "accepted" in out.artifact_dir.parts and evaluate_artifact(lake, out.artifact_dir, REGISTRY).ok


# ----------------------------------------------------------------------------------------------
# formatters
# ----------------------------------------------------------------------------------------------
def test_slm_builder_mapped_pairs_validate_against_schema(lake):
    recs = [{"publication-number": "814138-2026", "notice-title": {"eng": "Germany – Network components"},
             "buyer-name": {"deu": ["GWDG"]}, "buyer-country": ["DEU"], "classification-cpv": ["32422000"],
             "notice-type": "cn-standard", "publication-date": "2026-11-23+01:00"},
            {"publication-number": "1-2026"}]
    d = stage(lake, "SRC-010", {"records.jsonl": "\n".join(json.dumps(r) for r in recs).encode()})
    doc = build_for(lake, REGISTRY.get("SRC-010"), d)
    assert doc["counts"]["written"] == 2 and doc["counts"]["dropped_schema_invalid"] == 0
    for line in (d / "formatted" / "slm.jsonl").read_text().splitlines():
        pair = json.loads(line)
        assert [m["role"] for m in pair["messages"]] == ["system", "user", "assistant"]
        answer = json.loads(pair["messages"][2]["content"])
        assert not list(Draft202012Validator(pair["response_schema"]).iter_errors(answer))
        assert pair["metadata"]["source_id"] == "SRC-010" and pair["metadata"]["record_sha256"]
    first = json.loads((d / "formatted" / "slm.jsonl").read_text().splitlines()[0])
    ans = json.loads(first["messages"][2]["content"])
    assert ans["title"] == "Germany – Network components" and ans["buyer"] == "GWDG" and ans["winner"] is None


def test_slm_builder_json_schema_verdicts_come_from_jsonschema(lake):
    meta = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://example.org/meta",
            "type": "object", "properties": {"type": {"type": "string"}, "minLength": {"type": "integer", "minimum": 0}}}
    d = stage(lake, "SRC-001", {"schema.json": json.dumps(meta).encode()})
    build_for(lake, REGISTRY.get("SRC-001"), d)
    for line in (d / "formatted" / "slm.jsonl").read_text().splitlines():
        pair = json.loads(line)
        instance = json.loads(pair["messages"][1]["content"].split("Input (SRC-001):\n", 1)[1])["instance"]
        assert json.loads(pair["messages"][2]["content"])["valid"] == Draft202012Validator(meta).is_valid(instance)


def test_rag_chunks_are_grounding_only(lake):
    d = stage(lake, "SRC-003", {"licenses.json": json.dumps([{"licenseId": "MIT", "name": "MIT License"}] * 3).encode()})
    build_for(lake, REGISTRY.get("SRC-003"), d)
    chunk = json.loads((d / "formatted" / "rag_chunks.jsonl").read_text().splitlines()[0])
    assert chunk["metadata"]["training_allowed"] is False and chunk["citation"]["artifact_sha256"] == manifest_sha(d)
    assert not (d / "formatted" / "slm.jsonl").exists()


def test_requirements_classification_from_arff(lake):
    arff = b"@relation nfr\n@attribute RequirementText string\n@attribute class {F,PE,US}\n@data\n" \
           b"'The product shall respond within 2 seconds',PE\n'The system shall allow login',F\n"
    d = stage(lake, "SRC-018", {"nfr.arff": arff})
    build_for(lake, REGISTRY.get("SRC-018"), d)
    pairs = [json.loads(l) for l in (d / "formatted" / "slm.jsonl").read_text().splitlines()]
    assert [json.loads(p["messages"][2]["content"])["requirement_class"] for p in pairs] == ["PE", "F"]
    assert all(p["metadata"]["split"] == "eval" for p in pairs)


KICAD_SYM = """(kicad_symbol_lib (version 20231120) (generator "kicad_symbol_editor")
  (symbol "TPS62840" (in_bom yes) (on_board yes)
    (property "Reference" "U" (at 0 0 0))
    (property "Value" "TPS62840" (at 0 0 0))
    (property "Footprint" "Package_SON:WSON-8" (at 0 0 0))
    (property "Datasheet" "https://www.ti.com/lit/ds/symlink/tps62840.pdf" (at 0 0 0))
    (symbol "TPS62840_0_1" (rectangle (start -5 5) (end 5 -5)))
    (symbol "TPS62840_1_1"
      (pin power_in line (at -7.62 2.54 0) (length 2.54) (name "VIN") (number "1"))
      (pin output line (at 7.62 2.54 180) (length 2.54) (name "SW") (number "2"))
      (pin power_in line (at 0 -7.62 90) (length 2.54) (name "GND") (number "3")))))"""
KICAD_MOD = """(footprint "SOT-23" (layer "F.Cu") (attr smd)
  (fp_line (start -1.7 -1.6) (end 1.7 -1.6) (layer "F.CrtYd") (width 0.05))
  (fp_line (start 1.7 -1.6) (end 1.7 1.6) (layer "F.CrtYd"))
  (fp_line (start 1.7 1.6) (end -1.7 1.6) (layer "F.CrtYd"))
  (pad "1" smd roundrect (at -0.9375 -0.95) (size 1.475 0.6) (layers "F.Cu" "F.Paste" "F.Mask"))
  (pad "2" smd roundrect (at -0.9375 0.95) (size 1.475 0.6) (layers "F.Cu" "F.Paste" "F.Mask"))
  (pad "3" smd roundrect (at 0.9375 0) (size 1.475 0.6) (layers "F.Cu" "F.Paste" "F.Mask"))
  (model "${KICAD8_3DMODEL_DIR}/Package_TO_SOT_SMD.3dshapes/SOT-23.wrl"))"""
STEP = ("ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('sot23'),'2;1');\nFILE_NAME('sot23.step','2026-01-01',(''),(''),'','','');\n"
        "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));\nENDSEC;\nDATA;\n#1=CARTESIAN_POINT('',(0.,0.,0.));\n"
        "#2=CARTESIAN_POINT('',(2.9,1.3,1.0));\n#3=MANIFOLD_SOLID_BREP('',#4);\n"
        "#5=( LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.) );\nENDSEC;\nEND-ISO-10303-21;\n")


def test_eda_feature_pack(lake, tmp_path):
    buf = io.BytesIO()
    np.savez(buf, congestion=np.arange(16, dtype=float).reshape(4, 4))
    d = stage(lake, "SRC-023", {"Regulator.kicad_sym": KICAD_SYM.encode(), "Package.pretty/SOT-23.kicad_mod": KICAD_MOD.encode(),
                                "sot23.step": STEP.encode(), "maps/congestion.npz": buf.getvalue()})
    doc = build_for(lake, REGISTRY.get("SRC-023"), d)
    assert doc["counts"]["kicad_symbol"] == 1 and doc["counts"]["kicad_footprint"] == 1 and doc["counts"]["step_model"] == 1
    rows = {r["kind"]: r for r in map(json.loads, (d / "formatted" / "feature_pack.jsonl").read_text().splitlines())}
    sym, fp, step = rows["kicad_symbol"], rows["kicad_footprint"], rows["step_model"]
    assert sym["pin_count"] == 3 and sym["power_pin_count"] == 2 and sym["footprint"] == "Package_SON:WSON-8"
    assert fp["pad_count"] == 3 and fp["smd_pads"] == 3 and fp["min_pad_pitch_mm"] == pytest.approx(1.9)
    assert fp["courtyard_w_mm"] == pytest.approx(3.4) and fp["courtyard_area_m2"] == pytest.approx(3.4 * 3.2e-6)
    assert step["bbox_x_m"] == pytest.approx(0.0029) and step["solid_count"] == 1
    assert rows["numeric_array"]["max"] == 15.0
    assert pq.read_table(d / "formatted" / "feature_pack.parquet").num_rows == 4


def _png(path: Path, size=(64, 64), square=None, mode="RGB"):
    img = Image.new(mode, size, 0)
    if square:
        x1, y1, x2, y2 = square
        for x in range(x1, x2):
            for y in range(y1, y2):
                img.putpixel((x, y), 255 if mode == "L" else (255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG" if path.suffix == ".png" else "JPEG")
    return buf.getvalue()


def test_yolo_deeppcb_and_mvtec_eval_only(lake, tmp_path):
    files = {"PCBData/trainval.txt": b"group1/1/1000.jpg group1/1_not/1000.txt\n",
             "PCBData/test.txt": b"group1/1/1001.jpg group1/1_not/1001.txt\n",
             "PCBData/group1/1/1000_test.jpg": _png(Path("a.jpg")), "PCBData/group1/1/1000_temp.jpg": _png(Path("a.jpg")),
             "PCBData/group1/1/1001_test.jpg": _png(Path("a.jpg")),
             "PCBData/group1/1_not/1000.txt": b"16,16,32,48,2\n", "PCBData/group1/1_not/1001.txt": b"0 0 8 8 6\n"}
    d = stage(lake, "SRC-036", files)
    doc = build_for(lake, REGISTRY.get("SRC-036"), d)
    assert doc["counts"]["boxes"] == 2 and doc["counts"]["test"] == 1
    data = yaml.safe_load((d / "formatted" / "data.yaml").read_text())
    assert data["train"] == "images/train" and data["names"][1] == "short" and data["nc"] == 6
    label = next((d / "formatted" / "labels").rglob("*1000_test.txt")).read_text().split()
    assert label == ["1", "0.375000", "0.500000", "0.250000", "0.500000"]

    mv = {"bottle/test/broken_large/000.png": _png(Path("x.png"), (32, 32)),
          "bottle/ground_truth/broken_large/000_mask.png": _png(Path("x.png"), (32, 32), (4, 4, 12, 10), "L"),
          "bottle/test/good/001.png": _png(Path("x.png"), (32, 32))}
    d2 = stage(lake, "SRC-038", mv)
    assert d2.is_relative_to(lake.root / "eval_noncommercial" / "N4")
    build_for(lake, REGISTRY.get("SRC-038"), d2)
    text = (d2 / "formatted" / "data.yaml").read_text()
    data2 = yaml.safe_load(text)
    assert data2["train"] is None and data2["athena"]["eval_only"] and "non-commercial" in text
    assert data2["athena"]["commercial_training_allowed"] is False
    boxes = next((d2 / "formatted" / "labels" / "test").glob("*000.txt")).read_text().split()
    assert boxes[0] == "0" and float(boxes[3]) == pytest.approx(8 / 32)

    wrong = stage(lake, "SRC-038", mv, partition="eval_benchmarks")
    with pytest.raises(PolicyViolation) as exc:
        build_for(lake, REGISTRY.get("SRC-038"), wrong)
    assert exc.value.code == "HC-04"


def test_timeseries_units_utc_and_leak_free_splits(lake):
    rows = ["utc_timestamp,DE_load_actual_entsoe_transparency,DE_solar_generation_actual"]
    rows += [f"2024-01-{d:02d}T{h:02d}:00:00Z,{40000 + h},{h * 10}" for d in range(1, 11) for h in range(24)]
    d = stage(lake, "SRC-063", {"series.csv": "\n".join(rows).encode()})
    doc = build_for(lake, REGISTRY.get("SRC-063"), d)
    assert doc["units_si"]["DE_load_actual_entsoe_transparency"]["si_unit"] == "W"
    train = pq.read_table(d / "formatted" / "train.parquet").to_pandas()
    val = pq.read_table(d / "formatted" / "val.parquet").to_pandas()
    test = pq.read_table(d / "formatted" / "test.parquet").to_pandas()
    assert train["DE_load_actual_entsoe_transparency"].iloc[0] == 40000e6
    assert train["timestamp_utc"].max() < val["timestamp_utc"].min() <= val["timestamp_utc"].max() < test["timestamp_utc"].min()
    assert str(train["timestamp_utc"].dt.tz) == "UTC"
    meta = json.loads(pq.read_schema(d / "formatted" / "train.parquet").metadata[b"athena"])
    assert meta["time_standard"] == "UTC" and meta["split"] == "train"


def test_timeseries_naive_local_time_and_sequence_groups(lake):
    fred = "\n".join(json.dumps({"date": f"2024-01-{i:02d}", "value": "4.0" if i % 2 else "."}) for i in range(1, 8))
    d = stage(lake, "SRC-060", {"records.jsonl": fred.encode()}, subdir="live_cache/q1", origin_type="LIVE_API")
    build_for(lake, REGISTRY.get("SRC-060"), d)
    series = pq.read_table(d / "formatted" / "series.parquet").to_pandas()
    assert len(series) == 4 and series["value"].iloc[0] == pytest.approx(0.04)
    assert series["timestamp_utc"].iloc[0].isoformat() == "2024-01-01T05:00:00+00:00"   # America/New_York midnight
    assert not (d / "formatted" / "train.parquet").exists()                              # live data: no training split

    lines = [" ".join(str(x) for x in [u, c, 0, 0, 100] + [518.67] * 21) for u in range(1, 6) for c in range(1, 4)]
    d2 = stage(lake, "SRC-068", {"CMAPSSData/train_FD001.txt": "\n".join(lines).encode()})
    build_for(lake, REGISTRY.get("SRC-068"), d2)
    splits = json.loads((d2 / "formatted" / "splits.json").read_text())
    groups = [set(splits[k]["groups"]) for k in splits]
    assert sum(len(g) for g in groups) == 5 and not set.intersection(*groups)
    t = pq.read_table(d2 / "formatted" / "train.parquet").to_pandas()
    assert t["T2"].iloc[0] == pytest.approx(518.67 * 5 / 9 - 273.15)                     # °R → °C


def test_timeseries_refuses_naive_without_zone_and_unknown_units(lake):
    spec = REGISTRY.get("SRC-063")
    d = stage(lake, "SRC-063", {"series.csv": b"utc_timestamp,DE_load_actual_entsoe_transparency,DE_solar_generation_actual\n"
                                               b"2024-01-01 00:00,1,1\n2024-01-01 01:00,2,2\n2024-01-01 02:00,3,3\n"})
    with pytest.raises(FormatError, match="naive timestamps"):
        build_for(lake, spec, d)
    bad = spec.model_copy(update={"timeseries": spec.timeseries.model_copy(update={"value_columns": {"DE_load_actual_entsoe_transparency": "furlong"}})})
    with pytest.raises(FormatError, match="no SI conversion"):
        build_for(lake, bad, d)


# ----------------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------------
def test_cli_plan_approve_export_report(lake, capsys):
    from data_pipeline import cli

    assert cli.main(["--nodes", "N0..N1", "--priority", "P0", "--action", "plan", "--lake", str(lake.root)]) == 0
    assert "SRC-011" in capsys.readouterr().out and not lake.root.exists()
    assert cli.parse_nodes("N0..N2,N5") == ["N0", "N1", "N2", "N5"] and cli.parse_modes("train,nc") == ["train_candidates", "eval_noncommercial"]

    d = stage(lake, "SRC-001", {"schema.json": b'{"type": "object"}'})
    add_formatted(d, [{"messages": [{"role": "user", "content": "q"}]}])
    assert cli.main(["--sources", "SRC-001", "--action", "export", "--lake", str(lake.root), "--bundle", "b0"]) == 2
    assert cli.main(["--sources", "SRC-001", "--action", "approve", "--steward", "steward.n0-governance", "--sha256",
                     manifest_sha(d), "--decision", "APPROVED", "--lake", str(lake.root)]) == 0
    assert cli.main(["--sources", "SRC-001", "--action", "export", "--lake", str(lake.root), "--bundle", "b1"]) == 0
    bundle = lake.root / "_exports" / "training" / "b1"
    doc = json.loads((bundle / "bundle_manifest.json").read_text())
    assert doc["included"][0]["training_rights_approval"] == "APPROVED"
    assert (bundle / "SRC-001" / d.name / "formatted" / "slm.jsonl").exists()

    ev = stage(lake, "SRC-018", {"nfr.arff": b"@relation x\n"})
    add_formatted(ev, [{"x": 1}])
    approve(lake, "SRC-018", manifest_sha(ev))
    assert cli.main(["--sources", "SRC-018", "--action", "export", "--lake", str(lake.root), "--bundle", "b2"]) == 2
    capsys.readouterr()
    assert cli.main(["--action", "report", "--lake", str(lake.root), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["artifacts"] == 2 and report["summary"]["failing"] == 0


def test_mask_boxes_connected_components():
    from data_pipeline.formatters.vision_yolo_builder import mask_boxes
    m = np.zeros((20, 20), dtype=np.uint8)
    m[2:5, 2:6] = 255                      # blob 1: 4x3
    m[10:12, 10:12] = 255                  # blob 2 …
    m[12, 12] = 255                        # … joined diagonally (8-connectivity)
    m[18, 0] = 255                         # speck below min_area
    assert mask_boxes(m) == [(2, 2, 6, 5), (10, 10, 13, 13)]
