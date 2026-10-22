# Corporate RAG production deployment

This deployment reuses the server's existing NVIDIA/PyTorch image layers,
Ollama, and Qdrant Server. It does not start or modify those services.

Runtime topology:

- `corporate-rag-api`, one process, host network, listening on `127.0.0.1:8010`;
- `corporate-rag-gateway`, the single externally visible endpoint on port `3001`;
- `corporate-rag-onlyoffice`, view-only Document Server available through
  `/onlyoffice/` (its internal port `8082` is loopback-only);
- existing Qdrant Server at `127.0.0.1:6333`;
- existing Ollama at `127.0.0.1:11434`;
- dense embedding and reranker on container-visible CUDA device 0;
- models mounted read-only from `/opt/models`;
- chunks, normalized JSON, assets, and source files mounted read-only from
  `/opt/corporate-rag`.

PDF, DOC/DOCX/RTF and XLS/XLSX sources open in the embedded ONLYOFFICE viewer.
The API signs every view-only configuration with JWT and exposes source files
only by validated document/version IDs. Images and MP4 remain native browser
viewers; MP4 responses support byte ranges for seeking.

`rag-index` is an opt-in Compose profile. It synchronizes the deterministic
chunk store into `corporate_knowledge_chunks_v1` and exits. It is never run by
ordinary `up` or `restart`, so API restarts do not trigger reindexing.

`rag-update` is the separate opt-in Stage 11 process. It mounts the source
knowledge base read-only, mounts `/opt/corporate-rag/storage` read-write, uses
the same existing Qdrant Server, and exits after one serialized incremental
run. It is not started by normal API/frontend lifecycle commands.

The API is deliberately loopback-only while authentication is absent. Use an
SSH tunnel for operator access:

```powershell
ssh -L 8010:127.0.0.1:8010 rulko@ktzai.ktzholding.ru
```

Then open `http://127.0.0.1:8010/docs` locally.

Server lifecycle commands (run from `/opt/corporate-rag/app`):

```bash
sudo docker compose -f deployment/compose.production.yml build rag-api
sudo docker compose -f deployment/compose.production.yml --profile index run --rm rag-index
sudo docker compose -f deployment/compose.production.yml up -d rag-api
sudo docker compose -f deployment/compose.production.yml ps
sudo docker compose -f deployment/compose.production.yml logs -f --tail=200 rag-api
sudo docker compose -f deployment/compose.production.yml restart rag-api
sudo docker compose -f deployment/compose.production.yml stop rag-api
sudo docker compose -f deployment/compose.production.yml down
```

Do not use `down -v`; runtime data and models are host bind mounts, but avoiding
destructive flags is intentional.

## Incremental knowledge-base updates (Stage 11)

Before the first Stage 11 server run, copy the existing registry and optional
per-page OCR cache once. The export uses SQLite's online backup API rather than
copying a possibly open database file:

```powershell
.venv\Scripts\python.exe .\tools\export_incremental_state.py
scp .\tmp\corporate-rag-incremental-state.tar.gz `
  rulko@ktzai.ktzholding.ru:/tmp/
```

Back up any server copy, verify the archive, and extract it into
`/opt/corporate-rag`. Never repeat this bootstrap with a stale laptop registry
after production updates have started.

Build order matters because the lightweight updater image derives from the API
image and adds ingestion-only dependencies:

```bash
cd /opt/corporate-rag/app
sudo docker compose -f deployment/compose.production.yml build rag-api
sudo docker compose -f deployment/compose.production.yml --profile update build rag-update
```

After restoring the historical OCR page cache, run this migration once while
the active source files still match the registry. It stores rendered-page
SHA-256 values for already completed OCR pages, does not invoke MonkeyOCR, and
allows a later changed PDF to reuse every visually unchanged page:

```bash
sudo docker compose -f deployment/compose.production.yml --profile update run --rm \
  rag-update python -m corporate_rag_ingestion update \
  --bootstrap-ocr-fingerprints --json
```

The dry run is side-effect free: it does not create a lock/report, connect to
Qdrant, or instantiate embedding/OCR backends.

```bash
sudo docker compose -f deployment/compose.production.yml --profile update run --rm \
  rag-update python -m corporate_rag_ingestion update --dry-run --json
```

Run the actual update only after reviewing that plan:

```bash
sudo docker compose -f deployment/compose.production.yml --profile update run --rm rag-update
```

Force one exact path, glob, or registry document UUID without rebuilding other
documents:

```bash
sudo docker compose -f deployment/compose.production.yml --profile update run --rm \
  rag-update python -m corporate_rag_ingestion update \
  --force 'Отдел ИТ/Инструкция.pdf' --json
```

Runs are serialized by `/app/storage/incremental-update.lock`; JSON reports are
written atomically under `storage/reports/incremental`. An empty registry is
rejected by default so an operator cannot accidentally turn an incremental run
into a full rebuild. `--allow-empty-registry` exists only for an intentional
first import into a new environment.

The same hourly maintenance job also checks the Google Sheets rolling schedule.
The schedule command has its own 24-hour due check, SHA-256 no-op detection,
atomic SQLite snapshot, and last-good rollback behavior. It does not write to
Qdrant or reindex the document corpus. See
[`ROLLING_SCHEDULES.md`](ROLLING_SCHEDULES.md) for manual synchronization,
diagnostics, verification, and rollback.

The Windows bundle contains only application code, `chunks`, `normalized`,
`assets`, and the source knowledge base. It excludes local model caches,
Qdrant Local Mode files, OCR imports, tests, and reports:

```powershell
powershell -ExecutionPolicy Bypass -File .\deployment\prepare_bundle.ps1
scp .\tmp\corporate-rag-stage13-performance.tar.gz rulko@ktzai.ktzholding.ru:/tmp/
scp .\tmp\corporate-rag-knowledge-base.tar.gz rulko@ktzai.ktzholding.ru:/tmp/
```

## Production hardening (Stage 12)

Stage 12 adds bounded Docker logs, read-only container root filesystems,
dropped Linux capabilities, strict CORS, guarded source-file access, hourly
incremental updates, daily backups, retention, and boot recovery. Install the
host-level scripts and units after deploying the Stage 12 application files:

```bash
cd /opt/corporate-rag/app
sudo bash deployment/install_stage12.sh
```

The incremental timer runs hourly with up to five minutes of randomized delay.
The backup timer runs daily at 03:30 with up to thirty minutes of randomized
delay and retains seven complete backups. Both jobs share a maintenance lock,
so update, backup, and restore cannot overlap. See
[`RUNBOOK.md`](RUNBOOK.md) for normal operation, recovery, and rollback.

## Editor uploads (Stage 13)

Stage 13 adds `/admin` to the existing frontend. The shared editor credential
is stored as a PBKDF2 hash in `/opt/corporate-rag/config/admin.env`. Configure it
and install the upload path unit during deployment:

```bash
cd /opt/corporate-rag/app
sudo bash deployment/install_stage13.sh
```

The installer prompts for a password only when `admin.env` does not exist.
Uploads are written to an isolated queue, debounced for 30 seconds, validated
again on the host, atomically moved into the source tree, and processed by the
Stage 11 incremental updater. The hourly updater remains enabled as a fallback.

## Performance and streaming (Stage 13 Performance)

The API also exposes an NDJSON stream at `/api/answer/stream`. Queue status and
provisional deltas arrive first; the last `final` event contains the validated
answer, citations, sources, warnings, and telemetry. Deployment, rollback,
concurrency profiles, and one-variable-at-a-time benchmark commands are in
[`STAGE13_PERFORMANCE.md`](STAGE13_PERFORMANCE.md).

After startup, run the reproducible smoke and load checks inside the image:

```bash
sudo docker exec corporate-rag-api python /app/tools/smoke_test_production.py
sudo docker exec corporate-rag-api python /app/tools/load_test_api.py \
  --base-url http://127.0.0.1:8010 \
  --ollama-base-url http://127.0.0.1:11434 \
  --concurrency 2 5 --requests 10 --output /tmp/api-load-test.json
```
