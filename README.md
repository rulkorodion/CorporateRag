# Corporate RAG ingestion foundation

## Production frontend (Stage 10)

The Next.js corporate chat lives in `frontend/`. It calls the existing FastAPI
only through same-origin Next.js route handlers and preserves a stateless backend:
every question is sent independently to `POST /api/answer`. See
`frontend/README.md` for dev/build commands. In production the compose service is
`rag-frontend`, listening on `127.0.0.1:3001`.

This repository implements the ingestion boundary:

`Scanner -> Document Registry -> Format Router -> NormalizedDocument`

and the OCR enrichment boundary:

`Visual Decision Layer -> ImportedOCR / MonkeyOCR -> NormalizedDocument`

and the retrieval preparation boundary:

`NormalizedDocument -> StructuralChunker / TableChunker -> RetrievalChunk`

and the search boundary:

`RetrievalChunk -> Qwen3 dense + BM25 sparse -> Qdrant -> RRF hybrid retrieval`

The source knowledge base is read-only. Runtime data is written under `storage/`.

PDF pages are independently inspected with PyMuPDF before Docling output is
considered. The normalized page model records native text-layer counts, raster
image metrics, `page_visual_type`, `needs_ocr`, and a decision reason. OCR is not
executed by ingestion itself. Existing normalized files are upgraded by
`normalizer_version` without rerunning Docling when source content is unchanged.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pip install -e ".[retrieval]"
Copy-Item .env.example .env
```

`requirements-retrieval.txt` records the exact retrieval package versions used
for the validated baseline. PyTorch is intentionally device-specific: the main
environment retains its CPU build, while the documented GPU launcher reuses the
existing CUDA build without modifying either environment.

## Commands

```powershell
# Register and normalize new/changed files
.venv\Scripts\python -m corporate_rag_ingestion ingest

# Registry scan only (no format parsing)
.venv\Scripts\python -m corporate_rag_ingestion scan

# Force normalization of current versions again
.venv\Scripts\python -m corporate_rag_ingestion ingest --reprocess

# Build deterministic, traceable text/table chunks without modifying normalized JSON
.venv\Scripts\python -m corporate_rag_ingestion chunk

# Idempotently build/update the local persistent Qdrant hybrid index
.venv\Scripts\python -m corporate_rag_ingestion index

# Search in dense, BM25 sparse, or RRF hybrid mode
.venv\Scripts\python -m corporate_rag_ingestion search `
  "какая максимальная загрузка автомобиля?" --mode hybrid --top-k 5

# Hybrid top-40 -> Qwen3 reranker -> final top-10
.venv\Scripts\python -m corporate_rag_ingestion search `
  "какая максимальная загрузка автомобиля?" `
  --mode rerank --candidates 40 --top-k 10

# Build/update the future OCR manifest; this never loads or runs MonkeyOCR
.venv\Scripts\python -m corporate_rag_ingestion ocr-manifest

# Validate, cache, and merge externally produced OCR JSON into NormalizedDocument
.venv\Scripts\python -m corporate_rag_ingestion ocr-import --input storage\ocr\imported

# Read-only compatibility check before importing
.venv\Scripts\python -m corporate_rag_ingestion ocr-import `
  --input storage\ocr\imported --validate-only

# If a one-off server batch used temporary doc-001-style identifiers, create a
# canonical copy by exact relative_path + full SHA-256 (never by filename alone)
.venv\Scripts\python -m corporate_rag_ingestion ocr-prepare-import `
  --input rag-ocr-results-20260810\output-20260810 `
  --output storage\ocr\imported\all_pages.jsonl
```

## OCR import and future GPU processing

Put a server export directory (or a single `.json` / `.jsonl` file) under
`storage/ocr/imported/`. When the directory contains `all_pages.jsonl`, that
aggregate is used and per-page files are not read a second time. Every record is
matched by `document_id`, `version_id`, normalized `relative_path`, one-based
`page_number`, and `source_sha256`. The pinned model revision and canonical
settings hash must also match. Stale or misaddressed OCR is rejected and never
merged.

`ocr-prepare-import` is an explicit compatibility step for autonomous batches
that used temporary IDs. It leaves the export untouched, resolves a unique
current document only by the complete normalized relative path plus SHA-256,
writes an import-ready copy, and records the original batch IDs in provenance.
It writes no output at all if any record is ambiguous or invalid.

Pages classified as `ambiguous` remain excluded by default. After explicit human
visual review, a selected batch may be prepared and imported with
`--allow-ambiguous`. The original classification is preserved and provenance is
marked with `selection_basis=explicit_user_visual_review`; this flag never makes
future ambiguous pages eligible for the automatic manifest.

Completed OCR enriches the existing normalized version in place. It preserves
the original `scan_candidate` classification, sets `ocr_status=completed` and
`needs_ocr=false`, stores provenance and blocks, and extracts MonkeyOCR `Picture`
regions as assets without adding them to searchable text. `PARTIAL` and `FAILED`
results remain retryable.

Future GPU execution is deliberately protected by two independent switches. On
a prepared worker, install `requirements-monkeyocr.txt`, make the pinned model
available locally, then explicitly enable and run one deterministic shard:

```powershell
$env:OCR_MONKEYOCR_ENABLED = "true"
$env:HF_HUB_OFFLINE = "1"
$env:TRANSFORMERS_OFFLINE = "1"
.venv\Scripts\python -m corporate_rag_ingestion ocr-run `
  --manifest storage\ocr\manifest.jsonl `
  --shard-index 0 --shard-count 2 --allow-monkeyocr
```

Use shard index `1` in the second worker. The model is prepared once per worker
process. A completed exact fingerprint is reused; changed source SHA, model
revision, or effective settings produces a new task. The normal test suite never
imports GPU libraries or loads the model.

Paths and the database are configurable with `KNOWLEDGE_BASE_PATH`,
`STORAGE_PATH`, and `DATABASE_URL`. A PostgreSQL SQLAlchemy URL can replace the
SQLite default without changing application code.
Install the `postgres` extra when switching the URL to PostgreSQL.

`DOCLING_DOCUMENT_TIMEOUT_SECONDS` bounds pathological PDF processing; a timed-out
version is recorded as an error and does not stop other files.

## Structural chunking

The `chunk` command writes one versioned envelope to
`storage/chunks/<document_id>/<version_id>.json`. Text chunks respect headings,
paragraph/list boundaries, page-spanning sentences, OCR provenance, and existing
image relations. Tables use a separate row-aware representation with repeated
headers and retained HTML. Chunk IDs and JSON output are deterministic; an exact
second run performs no writes.

If an older normalized PDF contains native text in `NormalizedPage.native_text`
but Docling emitted no structural element for that page, chunking creates a
deterministic, explicitly registered page-derived source in memory. This prevents
content loss in long PDFs while leaving the normalized JSON untouched and keeps
the page number and source identity traceable in the chunk envelope.

The effective settings hash includes token limits, chunker version, and
`config/chunking_overrides.json`. That override contains only pages explicitly
rejected after human visual review. Changing either settings or overrides changes
the chunk identity and triggers a clean rebuild without modifying any
`NormalizedDocument` or source file.

Legacy `.doc` and `.rtf` files are routed through `LegacyOfficeAdapter`, converted
to cache-only DOCX files, and then processed by Docling. Install LibreOffice and
ensure `soffice` is on `PATH`, or set `LIBREOFFICE_PATH`. On Ubuntu, install the
`libreoffice` package; on Windows, install LibreOffice normally or set the full
path to `soffice.exe`.

Media transcript exceptions live in `config/media_overrides.json`. A video marked
`not_required` remains a non-indexable media asset and does not emit a missing
transcript warning.

## Hybrid retrieval

Retrieval settings are configured with the `RETRIEVAL_*` variables documented
in `.env.example`. The default uses persistent Qdrant Local Mode under
`storage/qdrant`; set `RETRIEVAL_QDRANT_URL` (and, when required, an API key) to
use a Qdrant server without changing indexing or search code. Qwen and BM25
revisions are pinned, stored in project-local caches, and loaded once per
process. The default runtime is offline after the initial model download.

When a separate CUDA-enabled Python environment already exists, it can execute
the same CLI without installing anything into that environment. On this
workstation the command is:

```powershell
& 'C:\Users\Rodion\Documents\Согласование спецификаций\.venv-gpu\Scripts\python.exe' `
  tools\run_retrieval_gpu.py index
```

The launcher imports CUDA PyTorch from the selected interpreter and then uses
the RAG project's regular environment for retrieval-only dependencies.

On the 4 GB workstation GPU, rerank mode releases the dense embedding model
after hybrid candidate retrieval and before loading the CrossEncoder. The
reranker is loaded once per process. A persistent server may place embedding
and reranking in separate workers or disable this release policy when hardware
has sufficient memory.

The reranker is pinned to `Qwen/Qwen3-Reranker-0.6B` revision
`e61197ed45024b0ed8a2d74b80b4d909f1255473`. It follows the official
SentenceTransformers `CrossEncoder` pair format, uses a configurable instruction,
and reports sigmoid relevance scores. Its candidate count, final top-k, batch
size, max length, device, dtype, and lifecycle policy are configurable with the
`RETRIEVAL_RERANKER_*` settings in `.env.example`.

The reproducible before/after evaluation is:

```powershell
& 'C:\Users\Rodion\Documents\Согласование спецификаций\.venv-gpu\Scripts\python.exe' `
  tools\evaluate_reranker.py --candidates 40 --final-top-k 10
```

Only chunks whose metadata does not explicitly set
`retrieval_candidate=false` are indexed. Each point carries both named vectors
and a traceable payload. The index fingerprint includes the complete chunk,
chunker settings, and retrieval settings; unchanged points are reused, changed
points are upserted, and stale point IDs are deleted. Hybrid search retrieves
dense and sparse candidates independently and performs transparent local RRF so
both component ranks and scores are returned to callers.

## Context builder

`context-v1.2-table-fields-citations` turns the final reranker results into a deterministic,
fully traceable `ContextPackage` without changing Qdrant, `RetrievalChunk`, or
`NormalizedDocument`. It removes exact/near duplicates, limits repetitive use
of one document while retaining complementary sections, and adds an adjacent
chunk only when a structural continuation signal is present. A useful neighbor
may replace only a lower-ranked tail result.

Tables retain their original headers, rows, and HTML metadata. Their LLM-only
rendering is hardened into explicit `field: value` records per row. Empty cells
are marked as empty instead of allowing a neighboring column to supply a value,
and Excel row numbers are explicitly distinguished from source citations. OCR and
`native_page_text` fallback provenance flow into each numbered source. Related
images are included only when the asset is declared in the matching normalized
document version and its local file exists.

```powershell
.venv\Scripts\python -m corporate_rag_ingestion search `
  "как настроить подпись в Outlook" --mode context `
  --top-k 10 --context-max-tokens 6000 --context-max-chunks 10
```

The defaults are 6000 estimated tokens, 10 total sources, at most 3 primary
sources per document, and at most 2 selective neighbors. The counter is behind
a small interface so the final LLM tokenizer can replace the current stable
offline estimator without changing the builder.

The reproducible Stage 6 evaluation reuses the real Stage 5 reranker baseline
and does not load either model:

```powershell
.venv\Scripts\python tools\evaluate_context.py
```

## Grounded answer generation

The `answer` command runs the complete read-only RAG path: hybrid retrieval,
Qwen reranking, ContextBuilder, remote Ollama chat generation, and strict
citation validation. The LLM receives only the user question and
`ContextPackage.rendered_context`; retrieval scores and internal payloads are
not included in its prompt.

```powershell
& "C:\Users\Rodion\Documents\Согласование спецификаций\.venv-gpu\Scripts\python.exe" `
  .\tools\run_retrieval_gpu.py answer `
  "к кому обращаться, чтобы заказать визитку?"
```

Use `--json` for the stable `RAGAnswer` model and `--show-context` when the full
ContextPackage is needed for diagnostics. The normal text output includes the
answer, validated source locations/chunk IDs, warnings, and component latency.
An answer that cites a source number absent from the current ContextPackage is
marked `invalid_citations`; the CLI exits with code 3 instead of silently
accepting it.

Ollama defaults to the existing SSH tunnel at `http://127.0.0.1:11435` and the
production model `qwen3:14b`. Model selection is configuration-only: set
`LLM_MODEL=qwen3:8b` for the lighter fallback when GPU capacity is more important. The default API context window
is `LLM_NUM_CTX=12288`, which covers the current ContextBuilder budget without
reserving an unnecessary 32K KV cache. Set `LLM_BASE_URL=http://127.0.0.1:11434`
when the application runs directly on the server. Proxy environment variables are ignored by default
(`LLM_TRUST_ENV=false`) so a localhost SSH tunnel cannot accidentally be routed
through `HTTP_PROXY`/`HTTPS_PROXY`. Temperature, context size, keep-alive, timeout,
thinking, streaming, prompt version, and generation version are documented in
`.env.example`. Streaming is intentionally disabled because answers are validated
as one complete artifact.

The reproducible A/B model comparison has separate context preparation and
generation phases. Every model therefore receives byte-for-byte identical
`ContextPackage` JSON, system prompt, and generation settings:

```powershell
& "C:\Users\Rodion\Documents\Согласование спецификаций\.venv-gpu\Scripts\python.exe" `
  .\tools\benchmark_llm_models.py prepare
.venv\Scripts\python.exe .\tools\benchmark_llm_models.py run `
  --models qwen3.6:latest qwen3:8b --num-ctx 12288
```

Prepared packages and full answers are written to
`storage/reports/llm-ab-contexts.json` and
`storage/reports/llm-ab-benchmark.json`. Controlled empty, conflicting, and
empty-table-field cases are explicitly marked and are not represented as facts
from the corporate knowledge base.

The table-hardening regression benchmark compares the old positional JSON with
the explicit field/value rendering on 39 unambiguous real table questions plus
the existing 12 general RAG scenarios:

```powershell
.venv\Scripts\python.exe .\tools\benchmark_table_context.py prepare
.venv\Scripts\python.exe .\tools\benchmark_table_context.py run `
  --models qwen3:14b qwen3:8b
```

Its prepared contexts and complete model outputs are stored under
`storage/reports/table-hardening-*.json`.

The backend accepts only citations matching a real `source_number`. Used source
records retain chunk/document/version/page/section and OCR/fallback provenance.
Related images are resolved exclusively from cited sources and from the assets
already validated by ContextBuilder. Instructions found inside retrieved files
are explicitly treated as untrusted document data in the system prompt.

The reproducible real-data smoke evaluation keeps the dense model and reranker
loaded once, calls the remote Ollama model for seven query types, and writes a
traceable report to `storage/reports/generation-smoke.json`:

```powershell
& "C:\Users\Rodion\Documents\Согласование спецификаций\.venv-gpu\Scripts\python.exe" `
  .\tools\evaluate_generation.py --candidates 40 --top-k 10
```

## Production FastAPI backend

The stateless API starts one persistent RAG runtime during application lifespan.
Dense embedding and reranker models are loaded once, Qdrant and Ollama clients
are reused, and shutdown closes all clients and releases the reranker. Run one
API worker: multiple workers would duplicate model memory and create independent
queues.

```powershell
cd "C:\Users\Rodion\Documents\ChatGPT\RAG база"
& "C:\Users\Rodion\Documents\Согласование спецификаций\.venv-gpu\Scripts\python.exe" `
  .\tools\run_api_gpu.py
```

Endpoints:

- `POST /api/answer` with `{"question":"..."}` returns the complete traced answer;
- `POST /api/answer/stream` returns status, live deltas and one validated final event;
- `GET /health` exposes counters, p50/p95 process latency, RAM and queue state;
- `GET /ready` checks Qdrant, loaded local models and the configured Ollama model.

Heavy stages have independent bounded FIFO gates. The production profile selected
after the Stage 13 server A/B is embedding/reranker/LLM `1/1/2` together with
`OLLAMA_NUM_PARALLEL=2`, 32 waiting requests per stage and a 300-second queue
timeout. Queue position updates are streamed as FIFO advances. Queue overflow
returns HTTP 429 and queue timeout returns HTTP 503. Configuration is documented
in `deployment/production.env.example`.

The reproducible live load test uses real questions at concurrency 1/2/5/10 and
writes all request-level and aggregate measurements to
`storage/reports/api-load-test.json`:

```powershell
.venv\Scripts\python.exe .\tools\load_test_api.py `
  --concurrency 1 2 5 10 --requests 10
```

## Local Streamlit interface

The local evaluation UI keeps the retrieval models alive for the whole browser
session. On the 4 GB laptop, the dense embedding model stays on CPU and the
reranker stays on CUDA; the final LLM remains on the remote Ollama server. This
avoids reloading and swapping both local models for every question.

```powershell
cd "C:\Users\Rodion\Documents\ChatGPT\RAG база"
& "C:\Users\Rodion\Documents\Согласование спецификаций\.venv-gpu\Scripts\python.exe" `
  .\tools\run_streamlit_gpu.py
```

Open `http://127.0.0.1:8501` if the browser does not open automatically. The UI
shows a grounded answer, validated source cards, and download buttons for original
files. When an answer was retrieved from a linked `Текстовый *.docx` transcript,
the user-facing source is the original MP4 (with a video player); the indexed
transcript path remains visible for traceability. Structurally linked images are
placed immediately after the cited answer paragraph instead of being collected
in a bottom gallery. Tiny decorative fragments and assets unrelated to the
chunk's source elements are excluded. PDF image regions are rendered on demand
from the original page at higher resolution; other assets are shown without
upscaling and can be downloaded at their original quality.

Standalone JPG/JPEG/PNG files are retrievable by their filename and folder even
though OCR/classification is intentionally still pending. A matching answer
returns the original image inline and explicitly treats the filename as the only
searchable text instead of inventing visual contents. XLSX merged cells are
propagated to every covered semantic row during normalization, so a shared
contact or communication method remains attached to each matching topic.
Chat history is display-only: every question is independently grounded and no
previous answer is inserted into the next prompt. Use the sidebar to vary
candidate count, reranker top-k, context sources, and token budget.

Install the interface dependencies with `pip install -r requirements-ui.txt`.

## Production Docker deployment

Stage 9 deployment files live in `deployment/`. The production container
reuses the prepared server CUDA/PyTorch image layers and connects to the
existing Ollama and Qdrant services over the host network; it never starts a
second Ollama or Qdrant instance. Dense embedding and reranking are configured
independently and both run on CUDA in production.

See `deployment/README.md` for the minimal bundle, controlled one-time Qdrant
index synchronization, API lifecycle, smoke tests, and load tests. The API is
loopback-only on port `8010` until authentication/reverse-proxy work is added.

## Safe incremental updates

Stage 11 reuses the existing recursive scanner, SHA-256 document/version
registry, normalization/OCR caches, structural chunker, and hybrid indexer. A
normal run handles only new, changed, or deleted paths; unchanged documents do
not enter normalization, OCR, chunking, or embedding.

```powershell
.venv\Scripts\python.exe -m corporate_rag_ingestion update --dry-run --json
.venv\Scripts\python.exe -m corporate_rag_ingestion update --json
.venv\Scripts\python.exe -m corporate_rag_ingestion update `
  --force "Отдел ИТ/*.pdf" --json
```

For an existing pre-Stage-11 OCR corpus, run the one-time
`update --bootstrap-ocr-fingerprints --json` migration before source files are
changed. It renders completed pages to record content hashes but never loads or
calls MonkeyOCR.

Candidate versions are staged without changing `Document.current_version_id`.
Their normalized document, any required per-page OCR, chunks, and vectors are
fully prepared first. Qdrant verifies every candidate point before removing
only the previous version's points for that document. Candidate points carry a
private staging flag and are excluded from all dense/sparse searches until the
complete set is verified and activated. Registry promotion is last; Qdrant
rollback restores the prior points if promotion fails. A broken
file is recorded as a failed candidate while its active version remains in
retrieval and processing continues for other files. Deleted sources retain
their registry/version history but their document-scoped Qdrant points are
removed. Repeating a no-change run is index-idempotent.

Reports include scan/change counts, OCR processed/reused pages, chunks,
embedding calculated/reused counts, Qdrant additions/removals, per-file timing,
and errors. See `deployment/README.md` for the production Compose profile,
one-time registry bootstrap, GPU/offline model mounts, and exact server commands.
