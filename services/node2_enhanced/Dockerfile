# Athena Node 2 — Micro-PLM & Stage-Gate Governance
#
#   docker build -t athena/node2:1.0.0 .
#   docker compose up
#
# Multi-stage so the runtime image carries no compiler. asyncpg ships manylinux
# wheels, so the build stage is quick and the runtime needs only libpq-free
# Python — asyncpg speaks the wire protocol directly.

FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /wheels
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt

# ---------------------------------------------------------------- runtime
FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ATHENA_ENVIRONMENT=prod \
    ATHENA_LOG_LEVEL=INFO

WORKDIR /srv/athena-node2

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt && rm -rf /wheels

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts
COPY tests ./tests
COPY pyproject.toml ./

RUN useradd --create-home --uid 10001 athena && chown -R athena:athena /srv/athena-node2
USER athena

EXPOSE 8002

# /health exercises the database connection and reports whether the schema is
# seeded. A TCP check would go green against an empty database, and an empty PLM
# answers "no gate found" to every question — which reads exactly like a clean
# programme.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8002/health || exit 1

# Multiple workers are safe here: all state is in PostgreSQL, not in the process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002", "--workers", "2"]
