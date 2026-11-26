"""Adversarial data-quality suite: 54 negative mutation cases DQ-001..DQ-054.

Each case starts from something valid (the real registry, a correctly staged artifact, a correct
API response) and applies one mutation an attacker, a careless script or a bad upstream could
introduce. The case passes only if the pipeline refuses it with the expected hard control
(HC-01..HC-12, see ``data_pipeline.policy_gates.HARD_CONTROLS``).

The case catalogue is a reconstruction (the reference workbook was not available); the mapping
from case to control is asserted below so the catalogue cannot silently drift.
"""
from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from data_pipeline.fetchers import ApiFetcher, DropZoneIntake, HttpArchiveFetcher, LiveLookupUnavailable, VaultStubFetcher
from data_pipeline.fetchers import fetcher_for
from data_pipeline.policy_gates import (HARD_CONTROLS, PolicyViolation, check_temporal_split, evaluate_artifact,
                                        evaluate_training_export, safe_component)
from data_pipeline.provenance import load_record_index, record_hash, write_manifest_once
from data_pipeline.registry import Registry, RegistryError
from data_pipeline.storage import DataLake

from _dp_support import (NO_WAIT, NOW, REGISTRY, MockWeb, add_formatted, approve, drop, edit_manifest, license_page,
                         manifest_sha, mutated_registry, stage)

SCHEMA_DOC = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://example/x", "type": "object"}


# ----------------------------------------------------------------------------------------------
# fixtures and helpers
# ----------------------------------------------------------------------------------------------
@pytest.fixture
def lake(tmp_path):
    return DataLake(tmp_path / "data_lake")


def schema_web(body: bytes = json.dumps(SCHEMA_DOC).encode(), ctype: str = "application/schema+json") -> MockWeb:
    return MockWeb({"https://json-schema.org/draft/": httpx.Response(200, content=body, headers={"content-type": ctype}),
                    "https://json-schema.org/": license_page("Academic Free License v3.0")})


def fetch(lake, sid, web=None, **kw):
    f = fetcher_for(REGISTRY.get(sid), lake, REGISTRY, client=(web or MockWeb()).client() if REGISTRY.get(sid).access
                    not in ("vault", "drop_zone") else None, sleep=lambda s: None, retries=0, rate_limiter=NO_WAIT, **kw)
    return f, f.fetch()


def staged_src001(lake) -> Path:
    _, out = fetch(lake, "SRC-001", schema_web())
    assert out.status == "STAGED", out.reasons
    return out.artifact_dir


def codes(report) -> set[str]:
    return report.codes


def export_codes(lake, artifact_dir, eval_hashes=None) -> set[str]:
    return evaluate_training_export(lake, artifact_dir, REGISTRY, lake.ledger, eval_hashes or set()).codes


def raises_code(code, fn, *a, **kw):
    with pytest.raises((PolicyViolation, RegistryError)) as exc:
        fn(*a, **kw)
    got = exc.value.codes if isinstance(exc.value, RegistryError) else {exc.value.code}
    assert code in got, (code, got, str(exc.value))
    return exc.value


CASES: dict[str, tuple[str, str, callable]] = {}


def case(case_id: str, control: str, title: str):
    def register(fn):
        CASES[case_id] = (control, title, fn)
        return fn
    return register


# ======================================= HC-01 provenance =======================================
@case("DQ-001", "HC-01", "manifest without source_id")
def dq001(lake, tmp_path):
    d = staged_src001(lake)
    edit_manifest(d, source_id=None)
    assert "HC-01" in codes(evaluate_artifact(lake, d, REGISTRY))


@case("DQ-002", "HC-01", "source_id not in the registry (SRC-076)")
def dq002(lake, tmp_path):
    d = staged_src001(lake)
    edit_manifest(d, source_id="SRC-076")
    rep = evaluate_artifact(lake, d, REGISTRY)
    assert "HC-01" in codes(rep) and "not in the source registry" in str(rep.violations[0])


@case("DQ-003", "HC-01", "node_id disagrees with the registry")
def dq003(lake, tmp_path):
    d = staged_src001(lake)
    edit_manifest(d, node_id="N5")
    assert "HC-01" in codes(evaluate_artifact(lake, d, REGISTRY))


@case("DQ-004", "HC-01", "intended_use relabelled")
def dq004(lake, tmp_path):
    d = staged_src001(lake)
    edit_manifest(d, intended_use="EVAL/BENCHMARK")
    assert "HC-01" in codes(evaluate_artifact(lake, d, REGISTRY))


@case("DQ-005", "HC-01", "sha256 is not a 64-hex digest")
def dq005(lake, tmp_path):
    d = staged_src001(lake)
    edit_manifest(d, sha256="abc123")
    assert "HC-01" in codes(evaluate_artifact(lake, d, REGISTRY))


@case("DQ-006", "HC-10", "payload bytes changed after staging")
def dq006(lake, tmp_path):
    d = staged_src001(lake)
    target = next((d / "artifact").glob("*.json"))
    os.chmod(target, 0o644)
    target.write_text('{"type": "string", "tampered": true}')
    rep = evaluate_artifact(lake, d, REGISTRY)
    assert "HC-10" in codes(rep) and "tampered" in " ".join(map(str, rep.violations))


@case("DQ-007", "HC-01", "blank license snapshot")
def dq007(lake, tmp_path):
    d = staged_src001(lake)
    edit_manifest(d, license_snapshot_text="   ", license_snapshot_sha256=None)
    assert "HC-01" in codes(evaluate_artifact(lake, d, REGISTRY))


@case("DQ-008", "HC-01", "license_spdx_id swapped to a different license")
def dq008(lake, tmp_path):
    d = staged_src001(lake)
    edit_manifest(d, license_spdx_id="MIT")
    assert "HC-01" in codes(evaluate_artifact(lake, d, REGISTRY))


@case("DQ-009", "HC-01", "naive (non-UTC) fetched_at_utc")
def dq009(lake, tmp_path):
    d = staged_src001(lake)
    edit_manifest(d, fetched_at_utc="2026-01-01T10:00:00")
    assert "HC-01" in codes(evaluate_artifact(lake, d, REGISTRY))


@case("DQ-010", "HC-01", "fetched_at_utc in the future")
def dq010(lake, tmp_path):
    d = staged_src001(lake)
    edit_manifest(d, fetched_at_utc=(NOW + timedelta(days=3)).isoformat())
    assert "HC-01" in codes(evaluate_artifact(lake, d, REGISTRY))


@case("DQ-011", "HC-01", "origin_type outside the enum")
def dq011(lake, tmp_path):
    d = staged_src001(lake)
    edit_manifest(d, origin_type="SCRAPED")
    assert "HC-01" in codes(evaluate_artifact(lake, d, REGISTRY))


@case("DQ-012", "HC-01", "training_rights_approval outside the enum")
def dq012(lake, tmp_path):
    d = staged_src001(lake)
    edit_manifest(d, training_rights_approval="YES")
    assert "HC-01" in codes(evaluate_artifact(lake, d, REGISTRY))


@case("DQ-013", "HC-01", "data_steward missing")
def dq013(lake, tmp_path):
    d = staged_src001(lake)
    edit_manifest(d, data_steward=None)
    assert "HC-01" in codes(evaluate_artifact(lake, d, REGISTRY))


@case("DQ-014", "HC-01", "official_url points somewhere else")
def dq014(lake, tmp_path):
    d = staged_src001(lake)
    edit_manifest(d, official_url="https://mirror.example.com/schema")
    assert "HC-01" in codes(evaluate_artifact(lake, d, REGISTRY))


@case("DQ-015", "HC-10", "re-writing an existing manifest with different content")
def dq015(lake, tmp_path):
    from data_pipeline.provenance import load_manifest
    d = staged_src001(lake)
    m = load_manifest(d)
    forged = m.model_copy(update={"training_rights_approval": "APPROVED"})
    raises_code("HC-10", write_manifest_once, d, forged)
    assert load_manifest(d).training_rights_approval == "NOT_REVIEWED"


@case("DQ-016", "HC-01", "unknown extra field smuggled into the manifest")
def dq016(lake, tmp_path):
    d = staged_src001(lake)
    edit_manifest(d, approved_for_training_by="intern")
    assert "HC-01" in codes(evaluate_artifact(lake, d, REGISTRY))


# ======================================= HC-02 isolation =======================================
@case("DQ-017", "HC-02", "TRAIN-CANDIDATE artifact stored in rag_reference")
def dq017(lake, tmp_path):
    d = stage(lake, "SRC-001", {"schema.json": b"{}"}, partition="rag_reference")
    rep = evaluate_artifact(lake, d, REGISTRY)
    assert "HC-02" in codes(rep) and "train_candidates" in " ".join(map(str, rep.violations))


@case("DQ-018", "HC-02", "RAG/REFERENCE artifact stored in train_candidates")
def dq018(lake, tmp_path):
    d = stage(lake, "SRC-003", {"licenses.json": b"[]"}, partition="train_candidates")
    assert "HC-02" in codes(evaluate_artifact(lake, d, REGISTRY))


@case("DQ-019", "HC-02", "path traversal through node / version components")
def dq019(lake, tmp_path):
    raises_code("HC-02", safe_component, "../N0", "node_id")
    raises_code("HC-02", safe_component, "SRC-001/../../x", "source_id")
    spec = REGISTRY.get("SRC-001")
    v = lake.version_dir(spec, "../../../../etc/passwd", "a" * 64)
    assert v.resolve().is_relative_to(lake.source_dir(spec).resolve())


@case("DQ-020", "HC-02", "data lake rooted inside the production vector store")
def dq020(lake, tmp_path):
    raises_code("HC-02", DataLake, tmp_path / "rag" / "vectorstore" / "lake", protected_paths=[tmp_path / "rag" / "vectorstore"])
    from data_pipeline.storage import REPO_ROOT
    raises_code("HC-02", DataLake, REPO_ROOT / "rag" / "vectorstore" / "data_lake")


@case("DQ-021", "HC-02", "partition directory symlinked outside the lake")
def dq021(lake, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = lake.root / "train_candidates" / "N0" / "SRC-001"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)
    _, out = fetch(lake, "SRC-001", schema_web())
    assert out.status == "REJECTED" and "HC-02" in out.reasons[0]
    assert list(outside.iterdir()) == []


@case("DQ-022", "HC-02", "invalid node id N9")
def dq022(lake, tmp_path):
    raises_code("HC-02", safe_component, "N9", "node_id")
    with pytest.raises(RegistryError, match="schema"):
        Registry.from_dict(mutated_registry("SRC-001", node_id="N9"))


# ======================================= HC-03 training rights =======================================
@case("DQ-023", "HC-03", "export while training_rights_approval is NOT_REVIEWED")
def dq023(lake, tmp_path):
    d = stage(lake, "SRC-001", {"schema.json": b"{}"})
    add_formatted(d, [{"messages": []}])
    rep = evaluate_training_export(lake, d, REGISTRY, lake.ledger, set())
    assert "HC-03" in rep.codes and "NOT_REVIEWED" in " ".join(map(str, rep.violations))


@case("DQ-024", "HC-03", "export after a REJECTED or PENDING steward decision")
def dq024(lake, tmp_path):
    d = stage(lake, "SRC-001", {"schema.json": b"{}"})
    add_formatted(d, [{"messages": []}])
    approve(lake, "SRC-001", manifest_sha(d), "PENDING")
    assert "HC-03" in export_codes(lake, d)
    approve(lake, "SRC-001", manifest_sha(d), "APPROVED")
    approve(lake, "SRC-001", manifest_sha(d), "REJECTED")            # latest decision wins
    assert "HC-03" in export_codes(lake, d)


@case("DQ-025", "HC-06", "approved LIVE-LOOKUP data pushed into a training export")
def dq025(lake, tmp_path):
    d = stage(lake, "SRC-060", {"records.jsonl": b'{"date": "2024-01-02", "value": "3.95"}\n'}, subdir="live_cache/k1",
              origin_type="LIVE_API", training_allowed=False)
    approve(lake, "SRC-060", manifest_sha(d))
    got = export_codes(lake, d)
    assert {"HC-06", "HC-03"} <= got


@case("DQ-026", "HC-03", "EVAL/BENCHMARK artifact exported to training")
def dq026(lake, tmp_path):
    d = stage(lake, "SRC-018", {"nfr.arff": b"@relation nfr\n"})
    add_formatted(d, [{"messages": []}])
    approve(lake, "SRC-018", manifest_sha(d))
    assert "HC-03" in export_codes(lake, d)


@case("DQ-027", "HC-03", "APPROVED written into the manifest instead of the steward ledger")
def dq027(lake, tmp_path):
    d = stage(lake, "SRC-001", {"schema.json": b"{}"}, training_rights_approval="APPROVED")
    add_formatted(d, [{"messages": []}])
    assert "HC-03" in export_codes(lake, d)
    raises_code("HC-03", lake.ledger.record, source_id="SRC-001", sha256=manifest_sha(d), decision="APPROVED",
                steward_id="steward.n3-design", registry=REGISTRY)


# ======================================= HC-04 non-commercial =======================================
@case("DQ-028", "HC-04", "MVTec AD flipped to commercial_training_allowed: true")
def dq028(lake, tmp_path):
    raises_code("HC-04", Registry.from_dict, mutated_registry("SRC-038", commercial_training_allowed=True))
    raises_code("HC-04", Registry.from_dict, mutated_registry("SRC-039", intended_use="TRAIN-CANDIDATE", training_allowed=True))


@case("DQ-029", "HC-04", "MVTec AD 2 artifact routed to eval_benchmarks")
def dq029(lake, tmp_path):
    d = stage(lake, "SRC-039", {"mvtec_ad_2.tar": b"x" * 600}, partition="eval_benchmarks")
    got = codes(evaluate_artifact(lake, d, REGISTRY))
    assert {"HC-04", "HC-02"} <= got


@case("DQ-030", "HC-04", "steward-approved MVTec AD exported to training")
def dq030(lake, tmp_path):
    d = stage(lake, "SRC-038", {"mvtec_ad.tar": b"x" * 600})
    add_formatted(d, [{"image": "a.png"}], name="data.yaml")
    approve(lake, "SRC-038", manifest_sha(d))
    assert "HC-04" in export_codes(lake, d)


@case("DQ-031", "HC-04", "NC license on a TRAIN-CANDIDATE source")
def dq031(lake, tmp_path):
    raises_code("HC-04", Registry.from_dict, mutated_registry("SRC-036", license_spdx_id="CC-BY-NC-4.0"))


# ======================================= HC-05 no scraping =======================================
@case("DQ-032", "HC-05", "portal / paid-standard source wired to an HTTP fetcher")
def dq032(lake, tmp_path):
    web = MockWeb()
    raises_code("HC-05", HttpArchiveFetcher, REGISTRY.get("SRC-027"), lake, REGISTRY, client=web.client())
    data = mutated_registry("SRC-013", access="http_archive", drop_zone=None,
                            http={"files": [{"url": "https://eprocure.gov.in/cppp/latestactivetendersnew", "name": "t.html"}]})
    raises_code("HC-05", Registry.from_dict, data)
    assert web.calls == []


@case("DQ-033", "HC-05", "drop-zone file without entitlement metadata")
def dq033(lake, tmp_path):
    drop(lake, "SRC-027", "IEC_62368-1.pdf", b"%PDF-1.7 standard", write_entitlement=False)
    _, out = fetch(lake, "SRC-027")
    assert out.status == "REJECTED" and "HC-05" in out.reasons[0]
    zone = DropZoneIntake(REGISTRY.get("SRC-027"), lake, REGISTRY).zone()
    assert (zone / "rejected" / "IEC_62368-1.pdf").exists() and not list((zone / "inbox").iterdir())


@case("DQ-034", "HC-05", "entitlement SHA-256 does not match the file")
def dq034(lake, tmp_path):
    drop(lake, "SRC-050", "tps62840.pdf", b"%PDF-1.7 datasheet", sha256="0" * 64)
    _, out = fetch(lake, "SRC-050")
    assert out.status == "REJECTED" and "does not match the entitlement" in out.reasons[0]


@case("DQ-035", "HC-05", "expired entitlement")
def dq035(lake, tmp_path):
    drop(lake, "SRC-055", "tender.pdf", b"%PDF-1.7 tender", valid_until=(NOW - timedelta(days=1)).isoformat())
    _, out = fetch(lake, "SRC-055")
    assert out.status == "REJECTED" and "not valid" in out.reasons[0]


@case("DQ-036", "HC-05", "entitlement issued for a different source")
def dq036(lake, tmp_path):
    drop(lake, "SRC-059", "gem-bid.pdf", b"%PDF-1.7 gem", source_id="SRC-055")
    _, out = fetch(lake, "SRC-059")
    assert out.status == "REJECTED" and "is for SRC-055" in out.reasons[0]


# ======================================= HC-06 live lookups =======================================
@case("DQ-037", "HC-06", "API-key source called without its key")
def dq037(lake, tmp_path):
    web = MockWeb(default=httpx.Response(200, json={"data": []}))
    _, out = fetch(lake, "SRC-011", web, env={})
    assert out.status == "REJECTED" and "COMTRADE_API_KEY" in out.reasons[0] and "HC-06" in out.reasons[0]
    assert web.calls == []                                           # fail closed: not a single request


@case("DQ-038", "HC-06", "API key literal written into the registry")
def dq038(lake, tmp_path):
    row = next(r for r in Registry.raw()["sources"] if r["source_id"] == "SRC-060")
    api = {**row["api"], "params": {**row["api"]["params"], "api_key": "abcd1234deadbeef"}}
    raises_code("HC-06", Registry.from_dict, mutated_registry("SRC-060", api=api))


@case("DQ-039", "HC-06", "live lookup tagged training_allowed: true")
def dq039(lake, tmp_path):
    raises_code("HC-06", Registry.from_dict, mutated_registry("SRC-046", training_allowed=True))


@case("DQ-040", "HC-06", "expired TTL cache served when the API is down")
def dq040(lake, tmp_path):
    state = {"up": True}

    def api(request):
        if not state["up"]:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"observations": [{"date": "2024-01-02", "value": "3.95"}]})

    web = MockWeb({"https://api.stlouisfed.org/": api, "https://fred.stlouisfed.org/legal/": license_page()})
    f = ApiFetcher(REGISTRY.get("SRC-060"), lake, REGISTRY, client=web.client(), env={"FRED_API_KEY": "fred-key-0001"},
                   sleep=lambda s: None, retries=0, rate_limiter=NO_WAIT)
    clock = {"t": datetime.now(timezone.utc)}
    f.now = lambda: clock["t"]
    first = f.lookup()
    assert not first.from_cache and first.records
    n = len(web.calls)
    clock["t"] += timedelta(hours=1)
    assert f.lookup().from_cache and len(web.calls) == n             # inside TTL: no request
    clock["t"] += timedelta(days=2)
    state["up"] = False
    raises_code("HC-06", f.lookup)                                   # stale entry is never served
    assert isinstance(pytest.raises(LiveLookupUnavailable, f.lookup).value, PolicyViolation)


@case("DQ-041", "HC-06", "API response echoes the credential")
def dq041(lake, tmp_path):
    key = "fred-secret-777777"
    web = MockWeb({"https://api.stlouisfed.org/": lambda r: httpx.Response(200, json={
        "observations": [{"date": "2024-01-02", "value": "3.95", "self": str(r.url)}]}),
        "https://fred.stlouisfed.org/legal/": license_page()})
    f = ApiFetcher(REGISTRY.get("SRC-060"), lake, REGISTRY, client=web.client(), env={"FRED_API_KEY": key},
                   sleep=lambda s: None, retries=0, rate_limiter=NO_WAIT)
    res = f.lookup()
    blobs = [p.read_bytes() for p in lake.root.rglob("*") if p.is_file()]
    assert blobs and not any(key.encode() in b for b in blobs)
    from data_pipeline.provenance import load_manifest
    m = load_manifest(res.artifact_dir)
    assert m.redactions >= 1
    raises_code("HC-06", write_manifest_once, tmp_path / "x", m.model_copy(update={"source_name": key}), [key])


# ======================================= HC-07 domain boundary =======================================
@case("DQ-042", "HC-07", "Paderborn bearing data without its domain-transfer warning")
def dq042(lake, tmp_path):
    raises_code("HC-07", Registry.from_dict, mutated_registry("SRC-069", domain_transfer_warning=None))
    raises_code("HC-07", Registry.from_dict, mutated_registry("SRC-070", domain_transfer_warning="OK_FOR_ELECTRONICS"))


@case("DQ-043", "HC-07", "C-MAPSS promoted to training as electronics evidence")
def dq043(lake, tmp_path):
    d = stage(lake, "SRC-068", {"train_FD001.txt": b"1 1 0.1 0.2 100 518.67\n"})
    add_formatted(d, [{"unit": 1}], name="train.parquet")
    approve(lake, "SRC-068", manifest_sha(d))
    assert "HC-07" in export_codes(lake, d)
    edit_manifest(d, domain_transfer_warning=None)
    assert "HC-07" in codes(evaluate_artifact(lake, d, REGISTRY))


# ======================================= HC-08 private ground truth =======================================
@case("DQ-044", "HC-08", "internal://athena vault fetched over HTTP")
def dq044(lake, tmp_path):
    web = MockWeb()
    raises_code("HC-08", HttpArchiveFetcher, REGISTRY.get("SRC-015"), lake, REGISTRY, client=web.client())
    f = HttpArchiveFetcher(REGISTRY.get("SRC-001"), lake, REGISTRY, client=web.client())
    raises_code("HC-08", f.check_url, "internal://athena/n1/rfq-win-loss-history")
    raises_code("HC-08", Registry.from_dict, mutated_registry("SRC-015", official_url="https://intranet.example/rfq"))
    assert web.calls == []


@case("DQ-045", "HC-08", "vault record that fails its intake schema")
def dq045(lake, tmp_path):
    f = VaultStubFetcher(REGISTRY.get("SRC-015"), lake, REGISTRY)
    v = f.ensure_stub()
    bad = {"rfq_id": "RFQ-1001", "customer_segment": "auto", "country": "India", "product_category": "BMS",
           "quoted_value": "125000", "currency": "INR", "outcome": "MAYBE", "decided_at": "2026-02-01T00:00:00Z"}
    (v / "inbox" / "batch1.jsonl").write_text(json.dumps(bad) + "\n")
    out = f.fetch()
    assert out.status == "REJECTED" and "HC-08" in out.reasons[0]
    assert (v / "rejected" / "batch1.jsonl").exists()


@case("DQ-046", "HC-08", "vault artifact labelled PUBLIC_OPEN")
def dq046(lake, tmp_path):
    d = stage(lake, "SRC-015", {"batch.jsonl": b'{"rfq_id": "RFQ-1"}\n'}, subdir="rfq-win-loss-history/accepted",
              origin_type="PUBLIC_OPEN")
    assert "HC-08" in codes(evaluate_artifact(lake, d, REGISTRY))


# ======================================= HC-09 synthetic =======================================
@case("DQ-047", "HC-09", "synthetic demo fixtures exported to training")
def dq047(lake, tmp_path):
    out = VaultStubFetcher(REGISTRY.get("SRC-073"), lake, REGISTRY).generate_synthetic_fixtures(5)
    assert out.status == "STAGED"
    add_formatted(out.artifact_dir, [{"x": 1}])
    approve(lake, "SRC-073", manifest_sha(out.artifact_dir))
    assert "HC-09" in export_codes(lake, out.artifact_dir)


@case("DQ-048", "HC-09", "synthetic fixtures relabelled as private ground truth")
def dq048(lake, tmp_path):
    f = VaultStubFetcher(REGISTRY.get("SRC-015"), lake, REGISTRY)
    out = f.generate_synthetic_fixtures(3)
    edit_manifest(out.artifact_dir, origin_type="PRIVATE_GROUND_TRUTH", fetch_method="vault_intake")
    assert "HC-09" in codes(evaluate_artifact(lake, out.artifact_dir, REGISTRY))
    rec = json.loads((out.artifact_dir / "artifact" / "fixtures.jsonl").read_text().splitlines()[0])
    (f.vault_dir() / "inbox" / "laundered.jsonl").write_text(json.dumps(rec) + "\n")
    res = f.fetch()
    assert res.status == "REJECTED" and "HC-09" in res.reasons[0]


# ======================================= HC-11 transport =======================================
@case("DQ-049", "HC-11", "plain-HTTP source URL")
def dq049(lake, tmp_path):
    raises_code("HC-11", Registry.from_dict, mutated_registry("SRC-001", official_url="http://json-schema.org/draft/2020-12/schema"))
    f = HttpArchiveFetcher(REGISTRY.get("SRC-001"), lake, REGISTRY, client=MockWeb().client())
    raises_code("HC-11", f.check_url, "http://json-schema.org/draft/2020-12/schema")


@case("DQ-050", "HC-11", "HTML login/error page served instead of JSON")
def dq050(lake, tmp_path):
    web = schema_web(b"<!doctype html><html><body>Please sign in</body></html>", "text/html")
    _, out = fetch(lake, "SRC-001", web)
    assert out.status == "REJECTED" and "HC-11" in out.reasons[0] and "text/html" in out.reasons[0]
    assert not (lake.root / "train_candidates").exists() or not list((lake.root / "train_candidates").rglob("*.json"))


@case("DQ-051", "HC-11", "archive member escaping the extraction directory (zip slip)")
def dq051(lake, tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("requirements/ok.xml", "<reqs/>")
        zf.writestr("../../../../evil.txt", "owned")
    web = MockWeb({"https://zenodo.org/records/1414117/files/": httpx.Response(200, content=buf.getvalue()),
                   "https://zenodo.org/records/1414117": license_page("CC BY 4.0")})
    _, out = fetch(lake, "SRC-017", web)
    assert out.status == "REJECTED" and "HC-11" in out.reasons[0]
    assert not any(p.name == "evil.txt" for p in tmp_path.rglob("evil.txt"))


@case("DQ-052", "HC-11", "download larger than the configured limit")
def dq052(lake, tmp_path):
    big = json.dumps({**SCHEMA_DOC, "pad": "x" * 5000}).encode()
    _, out = fetch(lake, "SRC-001", schema_web(big), max_bytes=1024)
    assert out.status == "REJECTED" and "max_bytes" in out.reasons[0]


# ======================================= HC-12 leakage =======================================
@case("DQ-053", "HC-12", "train and validation time ranges overlap")
def dq053(lake, tmp_path):
    bad = {"train": {"start": "2024-01-01T00:00:00+00:00", "end": "2024-06-30T00:00:00+00:00"},
           "val": {"start": "2024-06-01T00:00:00+00:00", "end": "2024-08-01T00:00:00+00:00"}}
    raises_code("HC-12", check_temporal_split, bad)
    raises_code("HC-12", check_temporal_split, {"train": {"start": 0, "end": 1, "groups": ["u1", "u2"]},
                                                "test": {"start": 2, "end": 3, "groups": ["u2"]}})
    d = stage(lake, "SRC-063", {"series.csv": b"utc_timestamp,DE_load\n"})
    add_formatted(d, [{"x": 1}], name="train.parquet", splits=bad)
    approve(lake, "SRC-063", manifest_sha(d))
    assert "HC-12" in export_codes(lake, d)


@case("DQ-054", "HC-12", "evaluation records duplicated into a training export")
def dq054(lake, tmp_path):
    shared = {"text": "The system shall log every fault code with a UTC timestamp."}
    train = stage(lake, "SRC-017", {"reqs.xml": b"<reqs/>"})
    add_formatted(train, [shared, {"text": "unique training requirement"}])
    approve(lake, "SRC-017", manifest_sha(train))
    ev = stage(lake, "SRC-018", {"nfr.arff": b"@relation nfr\n"})
    add_formatted(ev, [shared])
    eval_hashes = load_record_index(ev)
    assert export_codes(lake, train, set()) == set()                  # clean without the eval index…
    assert "HC-12" in export_codes(lake, train, eval_hashes)          # …contaminated with it


# ----------------------------------------------------------------------------------------------
# runner + catalogue integrity
# ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("case_id", sorted(CASES))
def test_dq_case(case_id, lake, tmp_path):
    control, title, fn = CASES[case_id]
    fn(lake, tmp_path)


def test_catalogue_is_complete():
    assert sorted(CASES) == [f"DQ-{i:03d}" for i in range(1, 55)]
    assert {c for c, _, _ in CASES.values()} == set(HARD_CONTROLS), "every hard control has at least one case"


def test_clean_baseline_passes_every_gate(lake):
    """The un-mutated baseline used by the cases is accepted, so each case fails only because of its mutation."""
    d = staged_src001(lake)
    rep = evaluate_artifact(lake, d, REGISTRY)
    assert rep.ok, rep.to_dict()
    add_formatted(d, [{"messages": [{"role": "user", "content": "x"}]}])
    approve(lake, "SRC-001", manifest_sha(d))
    assert evaluate_training_export(lake, d, REGISTRY, lake.ledger, set()).ok
