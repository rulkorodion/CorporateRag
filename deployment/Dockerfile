ARG BASE_IMAGE=specification-checker-worker:0.1.4-stage6.20260806
FROM ${BASE_IMAGE}

USER root

RUN if ! command -v libreoffice >/dev/null 2>&1 && ! command -v soffice >/dev/null 2>&1; then \
      apt-get update \
      && apt-get install -y --no-install-recommends libreoffice-core libreoffice-writer \
      && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /build
COPY requirements-production.txt pyproject.toml README.md ./
RUN python -m pip install --no-cache-dir -r requirements-production.txt

COPY src ./src
RUN python -m pip install --no-cache-dir --no-deps . \
    && python -c "import fastapi, fastembed, qdrant_client, sentence_transformers, corporate_rag_ingestion.api.app" \
    && rm -rf /build

WORKDIR /app
COPY tools/index_production.py /app/tools/index_production.py
COPY tools/load_test_api.py /app/tools/load_test_api.py
COPY tools/summarize_performance_reports.py /app/tools/summarize_performance_reports.py
COPY tools/run_api_production.py /app/tools/run_api_production.py
COPY tools/smoke_test_production.py /app/tools/smoke_test_production.py
COPY config /app/config

RUN mkdir -p /app/storage/chunks /app/storage/normalized /app/storage/assets /app/knowledge-base \
    && chown -R app:app /app

USER app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false

EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=10s --start-period=300s --retries=3 \
  CMD python -c "import json,urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8010/ready', timeout=5)); raise SystemExit(0 if data.get('ready') else 1)"

CMD ["python", "/app/tools/run_api_production.py"]
