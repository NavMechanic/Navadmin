"""N0, N1, N2, N5, N6 → ChatML instruction pairs with strict JSON-schema answers, plus
pre-training text and RAG grounding chunks.

* ``slm.jsonl`` — ``{"messages": [system, user, assistant], "response_schema": {...}, "metadata": {...}}``.
  Every assistant answer is a JSON object that **validates against its response schema**
  (Draft 2020-12, ``additionalProperties: false``). Answers are derived deterministically from the
  source record — mapped fields, a jsonschema verdict, a regex modality — never generated, and
  anything the record does not state is ``null``.
* ``pretrain.jsonl`` — ``{"text", "metadata"}`` chunks for long documents (PDF handbooks).
* ``rag_chunks.jsonl`` — grounding chunks with a citation for RAG/REFERENCE and LIVE-LOOKUP
  sources; tagged ``training_allowed: false`` and never written as instruction pairs.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

from jsonschema import Draft202012Validator

from ..provenance import record_hash
from .common import (FormatError, chunk_text, document_texts, finish_output, get_path, iter_records, payload_files,
                     prepare_output, provenance_meta)

NODE_ROLE = {"N0": "data-governance and schema assistant", "N1": "market-intelligence analyst",
             "N2": "product-requirements engineer", "N5": "sourcing and supply-risk analyst",
             "N6": "commercial bid analyst", "N3": "design engineer", "N4": "quality engineer",
             "N7": "finance analyst", "N8": "lifecycle and reliability engineer"}

# record_kind → (task, {output_field: (json type, [candidate paths])})
MAPPERS: dict[str, tuple[str, dict[str, tuple[str, list[str]]]]] = {
    "openalex_work": ("Extract the publication's key facts.", {
        "title": ("string", ["title", "display_name"]), "publication_year": ("integer", ["publication_year"]),
        "doi": ("string", ["doi"]), "work_type": ("string", ["type"]), "cited_by_count": ("integer", ["cited_by_count"]),
        "primary_topic": ("string", ["primary_topic.display_name"]),
        "institution_countries": ("array", ["authorships.*.countries"])}),
    "ted_notice": ("Extract the tender / award notice as structured data.", {
        "publication_number": ("string", ["publication-number"]), "title": ("string", ["notice-title"]),
        "buyer": ("string", ["buyer-name"]), "buyer_country": ("string", ["buyer-country"]),
        "notice_type": ("string", ["notice-type"]), "publication_date": ("string", ["publication-date"]),
        "cpv_codes": ("array", ["classification-cpv"]), "tender_deadline": ("string", ["deadline-receipt-tender-date-lot"]),
        "winner": ("string", ["winner-name"]), "total_value": ("number", ["total-value"])}),
    "usaspending_award": ("Summarise the federal award.", {
        "award_id": ("string", ["Award ID"]), "recipient": ("string", ["Recipient Name"]),
        "amount_usd": ("number", ["Award Amount"]), "description": ("string", ["Description"]),
        "agency": ("string", ["Awarding Agency"]), "start_date": ("string", ["Start Date"])}),
    "ocds_release": ("Extract the procurement release (OCDS).", {
        "ocid": ("string", ["ocid"]), "tender_title": ("string", ["tender.title"]), "buyer": ("string", ["buyer.name"]),
        "value_amount": ("number", ["tender.value.amount"]), "currency": ("string", ["tender.value.currency"]),
        "procurement_method": ("string", ["tender.procurementMethod"]),
        "award_suppliers": ("array", ["awards.*.suppliers.*.name"])}),
    "worldbank_procnotice": ("Extract the procurement notice.", {
        "notice_id": ("string", ["id"]), "project_name": ("string", ["project_name"]),
        "country": ("string", ["project_ctry_name"]), "notice_type": ("string", ["notice_type"]),
        "description": ("string", ["bid_description"]), "deadline": ("string", ["submission_deadline_date"]),
        "procurement_method": ("string", ["procurement_method_name"])}),
    "osv_vuln": ("Extract the vulnerability record for a firmware supply-chain review.", {
        "vuln_id": ("string", ["id"]), "summary": ("string", ["summary"]), "aliases": ("array", ["aliases"]),
        "severity_vectors": ("array", ["severity.*.score"]), "affected_packages": ("array", ["affected.*.package.name"]),
        "published": ("string", ["published"]), "modified": ("string", ["modified"])}),
    "nhtsa_complaint": ("Extract the field-failure complaint.", {
        "odi_number": ("integer", ["odiNumber"]), "components": ("string", ["components"]),
        "summary": ("string", ["summary"]), "date_of_incident": ("string", ["dateOfIncident"]),
        "crash": ("boolean", ["crash"]), "fire": ("boolean", ["fire"])}),
    "open_supply_facility": ("Extract the facility record.", {
        "os_id": ("string", ["os_id", "id"]), "name": ("string", ["name"]), "country": ("string", ["country_name", "country"]),
        "address": ("string", ["address"]), "sector": ("string", ["sector"])}),
}
_MODALITY = [("shall", r"\bshall\b"), ("must", r"\bmust\b"), ("should", r"\bshould\b"), ("will", r"\bwill\b"),
             ("may", r"\bmay\b")]
_SCHEMA_PROBES = [{"type": "string"}, {"type": "string", "minLength": 3}, {"type": 12}, {"minLength": -1},
                  {"properties": {"a": {"type": "integer"}}, "required": ["a"]}, {"required": "a"},
                  {"items": {"type": "number"}}, {"$ref": 5}, {"multipleOf": 0}, {"pattern": "^[a-z]+$"},
                  {"additionalProperties": False}, {"enum": ["a", "a"]}, {"maxItems": 2.5}, {"type": ["string", "null"]}]


def schema_for(fields: dict[str, tuple[str, list[str]]]) -> dict:
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "additionalProperties": False,
            "required": sorted(fields), "properties": {
                f: ({"type": ["array", "null"], "items": {"type": ["string", "number"]}} if t == "array"
                    else {"type": [t, "null"]}) for f, (t, _) in fields.items()}}


def _coerce(value: Any, t: str) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, dict):                      # language-keyed values, e.g. TED {"eng": "..."}
        if t == "array":
            value = list(value.values())
        else:
            value = value.get("eng") or value.get("ENG") or next(iter(value.values()), None)
            if isinstance(value, list):
                value = value[0] if value else None
    try:
        if t == "integer":
            return int(float(value))
        if t == "number":
            return float(str(value).replace(",", "")) if not isinstance(value, (int, float)) else float(value)
        if t == "boolean":
            return value if isinstance(value, bool) else str(value).strip().lower() in ("true", "yes", "1", "y")
        if t == "array":
            items = value if isinstance(value, list) else [value]
            flat = []
            for x in items:
                flat += x if isinstance(x, list) else [x]
            return [x if isinstance(x, (int, float)) and not isinstance(x, bool) else str(x) for x in flat if x is not None]
        if isinstance(value, list):
            value = "; ".join(str(x) for x in value)
        return str(value)
    except (TypeError, ValueError):
        return None


def map_record(record: dict, fields: dict[str, tuple[str, list[str]]]) -> dict:
    out = {}
    for name, (t, paths) in fields.items():
        val = None
        for p in paths:
            val = get_path(record, p)
            if val not in (None, "", []):
                break
        out[name] = _coerce(val, t)
    return out


def chatml(node: str, task: str, schema: dict, source_id: str, record: Any, answer: dict, meta: dict) -> dict:
    system = (f"You are ATHENA-{node}, a {NODE_ROLE.get(node, 'assistant')}. Reply with exactly one JSON object that "
              "validates against the JSON Schema in the request. Use null for anything the input does not state; "
              "never guess values.")
    user = (f"Task: {task}\nJSON Schema:\n{json.dumps(schema, sort_keys=True)}\n"
            f"Input ({source_id}):\n{json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)[:6000]}")
    return {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user},
                         {"role": "assistant", "content": json.dumps(answer, sort_keys=True, ensure_ascii=False)}],
            "response_schema": schema, "metadata": meta}


class _Writer:
    def __init__(self, path: Path) -> None:
        self.fh = open(path, "w", encoding="utf-8")
        self.n = 0

    def write(self, obj: dict) -> None:
        self.fh.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
        self.n += 1

    def close(self) -> None:
        self.fh.close()


# ----------------------------------------------------------------------------------------------
# builders
# ----------------------------------------------------------------------------------------------
def build(lake, spec, artifact_dir: Path, max_records: int = 200_000) -> dict:
    """ChatML / pre-training output for TRAIN-CANDIDATE and EVAL/BENCHMARK sources at N0/N1/N2/N5/N6 (and N8 text)."""
    if spec.intended_use in ("RAG/REFERENCE", "LIVE-LOOKUP"):
        return build_rag_chunks(lake, spec, artifact_dir)
    manifest, out = prepare_output(lake, artifact_dir, spec)
    kind = spec.record_kind or "key_value"
    split = "eval" if spec.intended_use == "EVAL/BENCHMARK" else "train_candidate"
    hashes: set[str] = set()
    counts = {"written": 0, "dropped_schema_invalid": 0, "pretrain_chunks": 0}
    if kind == "pdf_text":
        w = _Writer(out / "pretrain.jsonl")
        for rel, text, loc in document_texts(artifact_dir):
            for i, chunk in enumerate(chunk_text(text, 2000, 200)):
                h = record_hash(chunk)
                hashes.add(h)
                w.write({"text": chunk, "metadata": provenance_meta(manifest, file=rel, chunk=i, split=split,
                                                                    record_sha256=h, **loc)})
        counts["pretrain_chunks"] = w.n
        w.close()
        return finish_output(out, manifest, "slm_jsonl_builder", counts, hashes)

    w = _Writer(out / "slm.jsonl")
    gen = _json_schema_pairs if kind == "json_schema" else _requirement_pairs if kind == "requirements_labeled" \
        else _mapped_pairs
    for pair, h in gen(spec, artifact_dir):
        if counts["written"] >= max_records:
            break
        answer = json.loads(pair["messages"][2]["content"])
        if list(Draft202012Validator(pair["response_schema"]).iter_errors(answer)):
            counts["dropped_schema_invalid"] += 1
            continue
        pair["metadata"] = provenance_meta(manifest, record_sha256=h, split=split, **pair["metadata"])
        hashes.add(h)
        w.write(pair)
        counts["written"] += 1
    w.close()
    if counts["written"] == 0:
        raise FormatError(f"{spec.source_id}: no instruction pairs could be built from the artifact ({kind})")
    return finish_output(out, manifest, "slm_jsonl_builder", counts, hashes, {"record_kind": kind, "split": split})


def _mapped_pairs(spec, artifact_dir: Path):
    kind = spec.record_kind or "key_value"
    if kind in MAPPERS:
        task, fields = MAPPERS[kind]
    else:
        task, fields = "Return the record's scalar fields.", None
    for rel, rec in iter_records(artifact_dir):
        if fields is None:
            flds = {k: ("string", [k]) for k, v in rec.items() if isinstance(v, (str, int, float, bool)) and k}
            if not flds:
                continue
            schema, answer = schema_for(flds), map_record(rec, flds)
        else:
            schema, answer = schema_for(fields), map_record(rec, fields)
            if all(v in (None, []) for v in answer.values()):
                continue
        yield chatml(spec.node_id, task, schema, spec.source_id, rec, answer, {"file": rel, "task": kind}), record_hash(rec)


def _requirement_pairs(spec, artifact_dir: Path):
    for rel, rec in iter_records(artifact_dir):
        text = next((rec[k] for k in ("text", "RequirementText", "requirement", "Requirement", "req_text") if rec.get(k)), None)
        if not text:
            continue
        label = next((rec[k] for k in ("class", "_class_", "label", "Class", "type") if rec.get(k)), None)
        if label is not None:
            schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
                      "additionalProperties": False, "required": ["requirement_class"],
                      "properties": {"requirement_class": {"type": "string"}}}
            answer, task = {"requirement_class": str(label)}, "Classify the requirement (functional or non-functional category)."
        else:
            modality = next((m for m, rx in _MODALITY if re.search(rx, text, re.I)), None)
            schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
                      "additionalProperties": False, "required": ["requirement_id", "statement", "modality"],
                      "properties": {"requirement_id": {"type": ["string", "null"]}, "statement": {"type": "string"},
                                     "modality": {"type": ["string", "null"],
                                                  "enum": ["shall", "must", "should", "will", "may", None]}}}
            answer = {"requirement_id": rec.get("requirement_id"), "statement": text.strip(), "modality": modality}
            task = "Extract the requirement statement and its modal verb."
        yield chatml(spec.node_id, task, schema, spec.source_id, {"text": text}, answer,
                     {"file": rel, "task": "requirements"}), record_hash(text)


def _json_schema_pairs(spec, artifact_dir: Path):
    """Schema-validation pairs: the verdict comes from jsonschema, using the fetched meta-schemas."""
    from referencing import Registry, Resource
    try:
        from jsonschema_specifications import REGISTRY as BUILTIN
    except ImportError:  # pragma: no cover
        BUILTIN = Registry()
    docs = {}
    for p in payload_files(artifact_dir, (".json",)):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(doc, dict) and ("$schema" in doc or "$id" in doc):
            docs[p.relative_to(Path(artifact_dir) / "artifact").as_posix()] = doc
    registry = BUILTIN
    for doc in docs.values():
        if doc.get("$id"):
            registry = registry.with_resource(doc["$id"], Resource.from_contents(doc, default_specification=
                                                                                  _spec_2020()))
    out_schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
                  "additionalProperties": False, "required": ["valid", "violations"],
                  "properties": {"valid": {"type": "boolean"}, "violations": {"type": "array", "maxItems": 5, "items": {
                      "type": "object", "additionalProperties": False, "required": ["path", "keyword"],
                      "properties": {"path": {"type": "string"}, "keyword": {"type": "string"}}}}}}
    for rel, doc in docs.items():
        try:
            validator = Draft202012Validator(doc, registry=registry)
            for probe in _SCHEMA_PROBES:
                errors = sorted(validator.iter_errors(probe), key=lambda e: (list(e.absolute_path), e.validator))
                answer = {"valid": not errors, "violations": [
                    {"path": "/" + "/".join(str(x) for x in e.absolute_path), "keyword": str(e.validator)} for e in errors[:5]]}
                record = {"schema": doc.get("$id", rel), "instance": probe}
                yield chatml(spec.node_id, "Validate the instance against the referenced JSON Schema.", out_schema,
                             spec.source_id, record, answer, {"file": rel, "task": "json_schema_validation"}), record_hash(record)
        except Exception:  # unresolvable refs in a partial meta-schema set: skip that document
            continue


def _spec_2020():
    from referencing.jsonschema import DRAFT202012
    return DRAFT202012


def build_rag_chunks(lake, spec, artifact_dir: Path) -> dict:
    """Grounding chunks with citations. Never instruction pairs; tagged training_allowed=false."""
    manifest, out = prepare_output(lake, artifact_dir, spec)
    w = _Writer(out / "rag_chunks.jsonl")
    hashes: set[str] = set()
    citation = {"source_id": manifest.source_id, "official_url": manifest.official_url,
                "version_tag": manifest.version_tag, "artifact_sha256": manifest.sha256,
                "license_spdx_id": manifest.license_spdx_id, "fetched_at_utc": manifest.fetched_at_utc.isoformat()}
    records = list(iter_records(artifact_dir)) if not payload_files(artifact_dir, (".pdf", ".html", ".htm", ".ttl", ".txt", ".md")) else []
    sources = ((rel, json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str), {}) for rel, rec in records) \
        if records else document_texts(artifact_dir)
    for rel, text, loc in sources:
        for i, chunk in enumerate(chunk_text(text)):
            h = record_hash(chunk)
            hashes.add(h)
            w.write({"chunk_id": f"{manifest.source_id}:{manifest.sha256[:12]}:{len(hashes)}", "text": chunk,
                     "citation": {**citation, "file": rel, **loc},
                     "metadata": provenance_meta(manifest, training_allowed=False, grounding_only=True,
                                                 record_sha256=h, chunk=i)})
    w.close()
    return finish_output(out, manifest, "rag_chunks", {"chunks": w.n}, hashes, {"grounding_only": True})
