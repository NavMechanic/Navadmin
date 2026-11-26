"""Builders that turn staged artifacts into model-ready formats inside the artifact's own partition."""
from . import eda_physics_builder, slm_jsonl_builder, timeseries_builder, vision_yolo_builder
from .common import FormatError

BUILDERS = {
    "slm_jsonl": slm_jsonl_builder.build,
    "rag_chunks": slm_jsonl_builder.build_rag_chunks,
    "eda_physics": eda_physics_builder.build,
    "vision_yolo": vision_yolo_builder.build,
    "timeseries": timeseries_builder.build,
}


def build_for(lake, spec, artifact_dir) -> dict:
    if spec.formatter == "none":
        return {"builder": None, "skipped": "formatter none (vault / schema-only source)"}
    return BUILDERS[spec.formatter](lake, spec, artifact_dir)


__all__ = ["BUILDERS", "FormatError", "build_for"]
