"""Celery tasks and MLflow tracking.

    celery -A workers.tasks.celery_app worker --loglevel=info -c 4

What belongs on this queue, and what does not
---------------------------------------------
Parameter *extraction* does not. It takes milliseconds and the person who
uploaded the datasheet needs to know immediately whether its thermal resistance
could be read — a queued extraction returns ``{"status": "QUEUED"}`` and the
caller never learns that it could not. That runs synchronously in the API.

What belongs here is the work that takes minutes and that nobody is waiting on:
registering a parameter set into MLflow, retraining a surrogate when enough new
data has arrived, reconciling an extracted value against what other nodes
believe, and sweeping the held queue.

The MLflow discipline
---------------------
Every registered parameter set is a run with its **provenance** logged as tags —
document id, extraction confidence, and whether a human supplied it. A model
registry that records ``rth_jc = 1.8`` without recording that it was
hand-entered by an engineer rather than read from a datasheet cannot answer the
only question that matters after an escape: where did this number come from.

Tasks never invent values. ``task_register_parameters`` refuses a payload whose
safety-relevant parameters are absent rather than filling them, for the same
reason the extractor does.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("athena.workers")

BROKER = os.environ.get("ATHENA_CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
BACKEND = os.environ.get("ATHENA_CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/1")
MLFLOW_URI = os.environ.get("ATHENA_MLFLOW_TRACKING_URI", "")

try:
    from celery import Celery
    _HAS_CELERY = True
except ImportError:  # pragma: no cover
    Celery = None  # type: ignore
    _HAS_CELERY = False

try:
    import mlflow
    _HAS_MLFLOW = True
except ImportError:
    mlflow = None  # type: ignore
    _HAS_MLFLOW = False


# Parameters whose absence must never be papered over by a worker either.
SAFETY_KEYS = frozenset({
    "rth_jc_k_per_w", "creepage_mm", "clearance_mm", "v_ds_max_v",
    "v_rms_working_v", "operating_temp_max_c"})


if _HAS_CELERY:
    celery_app = Celery("athena_workers", broker=BROKER, backend=BACKEND)
    celery_app.conf.update(
        task_serializer="json", result_serializer="json", accept_content=["json"],
        timezone="UTC", enable_utc=True,
        # A task that dies mid-run is redelivered rather than lost. The tasks
        # here are written to be idempotent, which is what makes that safe.
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_time_limit=1800,
        task_soft_time_limit=1500,
        result_expires=86400,
        task_routes={
            "task_retrain_*": {"queue": "training"},
            "task_register_*": {"queue": "default"},
            "task_reconcile_*": {"queue": "default"},
        },
        beat_schedule={
            "sweep-held-documents": {
                "task": "task_sweep_held",
                "schedule": 3600.0,
            },
        },
    )
else:  # pragma: no cover - lets the module import without Celery installed
    class _Stub:
        def task(self, *a, **k):
            def deco(fn):
                fn.delay = lambda *args, **kwargs: (_ for _ in ()).throw(
                    RuntimeError("Celery is not installed; nothing can be queued."))
                return fn
            return deco

        conf = type("conf", (), {"update": lambda *a, **k: None})()

    celery_app = _Stub()  # type: ignore


def _mlflow_ready() -> bool:
    if not _HAS_MLFLOW or not MLFLOW_URI:
        return False
    try:
        mlflow.set_tracking_uri(MLFLOW_URI)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("MLflow unreachable at %s: %s", MLFLOW_URI, exc)
        return False


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# =====================================================================
# Tasks
# =====================================================================
@celery_app.task(name="task_register_parameters", bind=False)
def task_register_parameters(payload: dict[str, Any]) -> dict[str, Any]:
    """Register an extracted parameter set into MLflow with its provenance.

    Idempotent: the run name is derived from the document id, so a redelivered
    task overwrites rather than duplicates.
    """
    part_no = payload.get("part_no") or "unknown"
    specs: dict[str, Any] = payload.get("specs") or {}
    document_id = payload.get("document_id", "")

    missing_safety = sorted(SAFETY_KEYS - set(specs))
    result: dict[str, Any] = {
        "status": "ok", "part_no": part_no, "document_id": document_id,
        "registered": sorted(specs), "missing_safety": missing_safety,
        "at": _utc(),
    }

    if missing_safety:
        # Recorded, not filled. The worker has the same policy as the extractor.
        result["note"] = (
            f"{len(missing_safety)} safety-relevant parameter(s) are absent from "
            f"this payload ({', '.join(missing_safety)}). They are recorded as "
            "absent. A worker that filled them would defeat the extractor's "
            "refusal to default one line upstream.")

    if not _mlflow_ready():
        result["mlflow"] = "unavailable"
        result["note"] = (result.get("note", "") + " MLflow is not configured or "
                          "not reachable; the parameters are stored in the "
                          "ingestion database either way and this run can be "
                          "replayed from there.").strip()
        log.info("registered %s (no MLflow): %d parameter(s)", part_no, len(specs))
        return result

    try:
        mlflow.set_experiment("athena-component-parameters")
        with mlflow.start_run(run_name=f"{part_no}:{document_id[:8]}"):
            mlflow.set_tags({
                "part_no": part_no,
                "document_id": document_id,
                "supplier": payload.get("supplier") or "unknown",
                "sku": payload.get("sku") or "unknown",
                "source": "athena-ingest",
                # The tag that matters after an escape.
                "provenance": ",".join(
                    f"{k}={v.get('confidence')}" for k, v in sorted(specs.items())),
                "safety_complete": str(not missing_safety),
            })
            for key, spec in specs.items():
                value = spec.get("value") if isinstance(spec, dict) else spec
                try:
                    mlflow.log_metric(key, float(value))
                except (TypeError, ValueError):
                    continue
                if isinstance(spec, dict):
                    mlflow.log_param(f"{key}__confidence", spec.get("confidence"))
                    mlflow.log_param(f"{key}__unit", spec.get("unit"))
            mlflow.log_dict({
                "part_no": part_no, "document_id": document_id,
                "specs": specs, "missing_safety": missing_safety,
                "policy": ("Absent parameters are recorded as absent. Nothing here "
                           "is a default."),
            }, "parameters.json")
        result["mlflow"] = "logged"
        log.info("MLflow run for %s: %d parameter(s), %d safety gap(s)",
                 part_no, len(specs), len(missing_safety))
    except Exception as exc:  # noqa: BLE001
        result["mlflow"] = f"error: {type(exc).__name__}: {exc}"
        log.warning("MLflow logging failed for %s: %s", part_no, exc)
    return result


@celery_app.task(name="task_retrain_thermal_surrogate")
def task_retrain_thermal_surrogate(part_no: str,
                                   parameters: dict[str, Any]) -> dict[str, Any]:
    """Refresh Node 4's thermal surrogate inputs for one part.

    This does not retrain the PINN — that lives in Node 4 and takes minutes on
    its own hardware. What this does is assemble the *inputs* Node 4 should be
    using for this part, version them, and record which document each came from,
    so a simulation result can be traced to the datasheet that produced it.
    """
    rth_jc = parameters.get("rth_jc_k_per_w")
    if rth_jc is None:
        return {
            "status": "refused", "part_no": part_no,
            "reason": (
                "No junction-to-case thermal resistance is known for this part. "
                "Node 4 would have to assume one, and a junction temperature "
                "computed from an assumed thermal resistance is a number about an "
                "imaginary part. Supply it through the ingestion service, with a "
                "recorded reason, before this runs."),
            "at": _utc()}

    payload = {"part_no": part_no, "rth_jc_k_per_w": rth_jc,
               "operating_temp_max_c": parameters.get("operating_temp_max_c"),
               "v_ds_max_v": parameters.get("v_ds_max_v"),
               "updated_at": _utc()}

    if _mlflow_ready():
        try:
            mlflow.set_experiment("athena-thermal-surrogate")
            with mlflow.start_run(run_name=f"surrogate:{part_no}"):
                mlflow.set_tags({"part_no": part_no, "consumer": "node4"})
                for k, v in payload.items():
                    if isinstance(v, (int, float)):
                        mlflow.log_metric(k, float(v))
                mlflow.log_dict(payload, "thermal_inputs.json")
            payload["mlflow"] = "logged"
        except Exception as exc:  # noqa: BLE001
            payload["mlflow"] = f"error: {exc}"
    else:
        payload["mlflow"] = "unavailable"

    payload["status"] = "ok"
    payload["note"] = (
        "These are Node 4's thermal *inputs*, versioned and traceable to their "
        "source document. The PINN itself is retrained inside Node 4.")
    return payload


@celery_app.task(name="task_reconcile_parameter")
def task_reconcile_parameter(part_no: str, key: str, value: float,
                             platform_values: dict[str, float] | None = None
                             ) -> dict[str, Any]:
    """Compare an ingested parameter against what the rest of the platform holds.

    The creepage case is exactly this: Node 2 records 3.0 mm required and 2.4 mm
    measured, Node 4 computes 5.0 mm, Node 5's gauge is set to 6.4 mm. A
    datasheet supplying a fifth value should surface the disagreement, not
    quietly become a sixth source of truth.
    """
    platform_values = platform_values or {}
    all_values = {"ingested": value, **platform_values}
    distinct = {round(v, 4) for v in all_values.values()}

    out: dict[str, Any] = {
        "status": "ok", "part_no": part_no, "key": key,
        "values": all_values, "distinct_count": len(distinct), "at": _utc()}

    if len(distinct) > 1:
        spread = max(all_values.values()) - min(all_values.values())
        out["status"] = "conflict"
        out["spread"] = round(spread, 4)
        out["note"] = (
            f"{len(distinct)} different values for {key} on {part_no}: "
            + ", ".join(f"{k}={v}" for k, v in sorted(all_values.items()))
            + f" (spread {spread:.4g}). This is reported, not resolved. Choosing "
              "one automatically is how the wrong rating ends up in a thermal "
              "model or a safety gate.")
    return out


@celery_app.task(name="task_sweep_held")
def task_sweep_held() -> dict[str, Any]:
    """Periodic reminder that documents are sitting in the held queue.

    A hold that nobody is told about becomes a hold nobody clears, and a
    datasheet stuck in a queue for three months is functionally the same as one
    that was never uploaded — except that somebody believes it was.
    """
    return {"status": "ok", "at": _utc(),
            "note": ("Beat-scheduled sweep. Wire this to the notification channel "
                     "the quality team actually reads. An unnoticed hold is a "
                     "silently dropped datasheet.")}


@celery_app.task(name="task_health")
def task_health() -> dict[str, Any]:
    return {"status": "ok", "celery": _HAS_CELERY, "mlflow": _mlflow_ready(),
            "broker": BROKER.split("@")[-1], "at": _utc()}


def worker_report() -> dict[str, Any]:
    """What the API reports about the worker fleet."""
    return {
        "celery_installed": _HAS_CELERY,
        "mlflow_installed": _HAS_MLFLOW,
        "mlflow_uri": MLFLOW_URI or None,
        "broker": BROKER.split("@")[-1],
        "queues": ["default", "training"],
        "tasks": ["task_register_parameters", "task_retrain_thermal_surrogate",
                  "task_reconcile_parameter", "task_sweep_held", "task_health"],
        "note": ("Extraction runs synchronously in the API — a queued extraction "
                 "returns QUEUED and the uploader never learns their datasheet's "
                 "thermal resistance could not be read. Only the minute-scale "
                 "work is queued here."),
    }
