"""Background worker: advances RUNNING workflow runs (deferred retries, crash recovery).

    python -m app.orchestration.worker            # loop forever
    python -m app.orchestration.worker --once     # one pass (e.g. from cron)

Approval-waiting runs are left alone; they resume when a human decides.
Run one worker per database unless you add row locking (SELECT ... FOR UPDATE SKIP LOCKED on Postgres).
"""
from __future__ import annotations

import argparse
import logging
import time

from sqlalchemy import select

from ..config import load_settings
from ..db import Base, make_engine, make_sessionmaker
from ..models import WorkflowRun
from .engine import RUN_COMPENSATING, RUN_RUNNING, Engine

log = logging.getLogger("pilot_platform.worker")


def run_once(sessionmaker, settings) -> int:
    advanced = 0
    with sessionmaker() as db:
        runs = db.execute(select(WorkflowRun.id, WorkflowRun.tenant_id)
                          .where(WorkflowRun.status.in_((RUN_RUNNING, RUN_COMPENSATING)))).all()
        engine = Engine(db, settings)
        for run_id, tenant_id in runs:
            try:
                engine.advance(tenant_id, run_id)
                advanced += 1
            except Exception:  # keep the worker alive; the run keeps its state for the next pass
                db.rollback()
                log.exception("advance failed for run %s", run_id)
        try:
            from ..atlas.jobs import dispatch_due
            advanced += dispatch_due(db, settings)  # vendor jobs waiting on rate-limit backoff
        except Exception:
            db.rollback()
            log.exception("vendor job dispatch failed")
    return advanced


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    from ..integration import rfq_pipeline  # noqa: F401  (register handlers)

    settings = load_settings()
    engine = make_engine(settings.database_url)
    if settings.auto_create_schema:
        Base.metadata.create_all(engine)
    sm = make_sessionmaker(engine)
    while True:
        n = run_once(sm, settings)
        log.info("advanced %d run(s)", n)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
