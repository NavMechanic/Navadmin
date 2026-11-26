"""ATHENA policy-gated data acquisition, staging and validation pipeline (SRC-001..SRC-075, nodes N0–N8).

Modules: ``registry`` (typed catalog), ``policy_gates`` (12 hard controls), ``provenance`` (manifests,
PROV-O, SPDX, CycloneDX), ``storage`` (isolated data lake), ``fetchers`` and ``formatters``.
Run ``python -m data_pipeline.cli --help``.
"""
__version__ = "1.0.0"
