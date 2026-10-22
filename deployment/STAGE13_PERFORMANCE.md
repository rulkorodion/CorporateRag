# Stage 13 Performance deployment and A/B runbook

All measurements below must be run on the production-like server. Local test
results do not substitute for server measurements.

## Build and transfer

Run in Windows PowerShell from the project directory:

```powershell
powershell -ExecutionPolicy Bypass `
  -File .\deployment\prepare_bundle.ps1 `
  -SkipKnowledgeBase
Get-FileHash .\tmp\corporate-rag-stage13-performance.tar.gz -Algorithm SHA256
scp .\tmp\corporate-rag-stage13-performance.tar.gz `
  rulko@ktzai.ktzholding.ru:/tmp/
```

## Deploy with rollback

Run on the server:

```bash
cd /opt/corporate-rag
stage13_stamp=$(date +%Y%m%d-%H%M%S)
stage13_unpack="/tmp/corporate-rag-stage13-performance-$stage13_stamp"
mkdir -p "$stage13_unpack" /opt/corporate-rag/backups
tar -xzf /tmp/corporate-rag-stage13-performance.tar.gz -C "$stage13_unpack"
test -f "$stage13_unpack/app/tools/load_test_api.py"

mv /opt/corporate-rag/app \
  "/opt/corporate-rag/backups/app-before-stage13-performance-$stage13_stamp"
mv "$stage13_unpack/app" /opt/corporate-rag/app

cd /opt/corporate-rag/app
sudo docker compose -f deployment/compose.production.yml config --quiet
sudo docker compose -f deployment/compose.production.yml build rag-api rag-frontend
sudo install -m 0755 \
  deployment/scripts/corporate-rag-performance-profile \
  /usr/local/sbin/corporate-rag-performance-profile
sudo corporate-rag-performance-profile apply 1-1-2
sudo docker compose -f deployment/compose.production.yml \
  up -d --force-recreate rag-frontend
```

The profile command prints its exact settings-backup directory. Keep that path
for rollback.

## Readiness and streaming smoke test

```bash
curl -fsS http://127.0.0.1:8010/health | python3 -m json.tool
curl -fsS http://127.0.0.1:8010/ready | python3 -m json.tool
curl -fsS -o /dev/null -w 'frontend HTTP %{http_code}\n' http://127.0.0.1:3001/
curl -fsS -N \
  -X POST http://127.0.0.1:3001/api/answer/stream \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/x-ndjson' \
  -d '{"question":"К кому обращаться, чтобы заказать визитку?"}' \
  | tee /tmp/stage13-stream-smoke.ndjson
```

The stream must contain `status`, one or more `delta` records, and one `final`
record. Only `final.data` is the validated answer.

## Concurrency A/B

Create the report directory once:

```bash
sudo install -d -o 10001 -g 10001 -m 0750 \
  /opt/corporate-rag/storage/reports/performance-stage13
```

For each profile, apply it, warm Ollama outside the measurement, then run the
same workload:

```bash
mode=1-1-1
sudo corporate-rag-performance-profile apply "$mode"
curl -fsS -N \
  -X POST http://127.0.0.1:8010/api/answer/stream \
  -H 'Content-Type: application/json' \
  -d '{"question":"К кому обращаться, чтобы заказать визитку?"}' \
  -o /dev/null

sudo docker run --rm --network host --gpus all \
  -v /opt/corporate-rag/storage/reports/performance-stage13:/reports \
  corporate-rag-api:0.2.1-stage13-performance \
  python /app/tools/load_test_api.py \
  --base-url http://127.0.0.1:8010 \
  --mode "$mode" \
  --users 1 2 5 10 \
  --requests 20 \
  --output "/reports/concurrency-$mode.json"

sudo docker logs --since 30m corporate-rag-api 2>&1 \
  | grep -Ei 'traceback|exception|cuda error|out of memory|permission denied' \
  | tail -50 \
  | sudo tee "/opt/corporate-rag/storage/reports/performance-stage13/errors-$mode.txt" \
  >/dev/null || true
sudo journalctl -u ollama --since '30 minutes ago' --no-pager -o cat \
  | grep -Ei 'error|failed|panic|out of memory|cuda' \
  | tail -50 \
  | sudo tee "/opt/corporate-rag/storage/reports/performance-stage13/ollama-errors-$mode.txt" \
  >/dev/null || true
```

Repeat with only `mode` changed, in this order:

```bash
mode=1-1-1
mode=1-1-2
mode=2-1-1
mode=2-2-1
mode=2-2-2
```

`2-2-2` sets API LLM concurrency and `OLLAMA_NUM_PARALLEL` to two together.
On OOM, CUDA error, or failed readiness, return immediately to baseline:

```bash
sudo corporate-rag-performance-profile apply 1-1-1
```

## Context budget A/B

Run after choosing one stable concurrency profile. These are per-request
overrides and do not edit the production default:

```bash
for budget in 6000 5000 4000; do
  sudo docker run --rm --network host --gpus all \
    -v /opt/corporate-rag/storage/reports/performance-stage13:/reports \
    corporate-rag-api:0.2.1-stage13-performance \
    python /app/tools/load_test_api.py \
    --base-url http://127.0.0.1:8010 \
    --mode "context-$budget" \
    --users 1 \
    --requests 10 \
    --max-context-tokens "$budget" \
    --include-answer-text \
    --output "/reports/context-$budget.json"
done
```

Review `prompt_estimate_ratio`, actual `prompt_eval_count`, citations, source
paths, and answer completeness. The production default stays 6000 until this
review is complete.

## Output budget A/B

This is a separate experiment; no context override is supplied:

```bash
for limit in 512 1024 2048; do
  sudo docker run --rm --network host --gpus all \
    -v /opt/corporate-rag/storage/reports/performance-stage13:/reports \
    corporate-rag-api:0.2.1-stage13-performance \
    python /app/tools/load_test_api.py \
    --base-url http://127.0.0.1:8010 \
    --mode "output-$limit" \
    --users 1 \
    --requests 10 \
    --num-predict "$limit" \
    --include-answer-text \
    --output "/reports/output-$limit.json"
done
```

`done_reason=length`, an incomplete sentence, worse citation validation, or
missing steps means the limit is too small. The default remains unlimited
until the real results justify a cap.

## Summarize and transfer the actual results

```bash
sudo docker run --rm \
  -v /opt/corporate-rag/storage/reports/performance-stage13:/reports \
  corporate-rag-api:0.2.1-stage13-performance \
  python /app/tools/summarize_performance_reports.py /reports/*.json \
  | sudo tee /opt/corporate-rag/storage/reports/performance-stage13/summary.md
sudo tar -czf /tmp/corporate-rag-stage13-results.tar.gz \
  -C /opt/corporate-rag/storage/reports performance-stage13
```

Copy `/tmp/corporate-rag-stage13-results.tar.gz` to the workstation. Those
reports, not assumptions, determine the final profile and budgets.

## Rollback

Restore the settings backup printed by the first profile command, then the
previous app directory:

```bash
sudo corporate-rag-performance-profile rollback \
  /opt/corporate-rag/backups/performance-profiles/<backup-directory>
cd /opt/corporate-rag
mv app "backups/app-failed-stage13-performance-$(date +%Y%m%d-%H%M%S)"
mv backups/app-before-stage13-performance-<deployment-timestamp> app
cd /opt/corporate-rag/app
sudo docker compose -f deployment/compose.production.yml \
  up -d --force-recreate rag-api rag-frontend
```
