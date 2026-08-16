FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-full.txt ./
RUN pip install --no-cache-dir -r requirements-full.txt
COPY app ./app
COPY workers ./workers
RUN useradd --create-home --uid 10001 athena && chown -R athena:athena /app
USER athena
EXPOSE 8007
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8007/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8007", "--workers", "1"]
