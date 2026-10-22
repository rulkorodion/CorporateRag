# Corporate RAG production runbook

Production root: `/opt/corporate-rag`; Compose file:
`/opt/corporate-rag/app/deployment/compose.production.yml`.

## Daily checks

```bash
cd /opt/corporate-rag/app
sudo docker compose -f deployment/compose.production.yml ps
curl -fsS http://127.0.0.1:8010/health | python3 -m json.tool
curl -fsS http://127.0.0.1:8010/ready | python3 -m json.tool
curl -fsS -o /dev/null -w 'frontend HTTP %{http_code}\n' http://127.0.0.1:3001/
nvidia-smi
df -h /
```

API and frontend logs are bounded by the Compose `local` logging driver
(`20m` per file, five files per container):

```bash
sudo docker compose -f deployment/compose.production.yml logs --tail=200 rag-api
sudo docker compose -f deployment/compose.production.yml logs --tail=200 rag-frontend
sudo journalctl -u corporate-rag-update.service -n 100 --no-pager
sudo journalctl -u corporate-rag-backup.service -n 100 --no-pager
sudo journalctl -u corporate-rag-upload.service -n 100 --no-pager
```

Inspect the active Stage 13 concurrency profile without changing it:

```bash
sudo corporate-rag-performance-profile show
```

## Dependencies

```bash
curl -fsS http://127.0.0.1:6333/collections/corporate_knowledge_chunks_v1 |
  python3 -m json.tool
curl -fsS http://127.0.0.1:11434/api/tags | python3 -m json.tool
systemctl is-enabled ollama
sudo docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' qdrant
```

Qdrant and Ollama are existing server services. Do not recreate them from the
Corporate RAG Compose project.

## Incremental update

Hourly runs are serialized by `/run/lock/corporate-rag-maintenance.lock`.
The pipeline also keeps its own data lock. A no-change run exits quickly.

```bash
systemctl list-timers --all 'corporate-rag-*'
sudo systemctl start corporate-rag-update.service
sudo systemctl status corporate-rag-update.service --no-pager
sudo journalctl -u corporate-rag-update.service -n 100 --no-pager
```

## Editor uploads

The editor opens `/admin` on the same frontend address. Uploaded bytes are
limited to the isolated queue; only the host promotion service writes to the
knowledge-base source tree.

```bash
systemctl status corporate-rag-upload.path --no-pager
sudo journalctl -u corporate-rag-upload.service -n 100 --no-pager
sudo find /opt/corporate-rag/admin-upload/queue -mindepth 1 -maxdepth 2 -type f
```

Change the shared editor password without rebuilding the image:

```bash
sudo corporate-rag-admin-credentials editor
cd /opt/corporate-rag/app
sudo docker compose -f deployment/compose.production.yml up -d --force-recreate rag-frontend
```

Automatic upload processing is debounced for 30 seconds. The upload service
and hourly updater share `/run/lock/corporate-rag-maintenance.lock`, so OCR,
embedding, backup, and restore jobs cannot overlap.

To preview changes without writing anything:

```bash
cd /opt/corporate-rag/app
sudo docker compose -f deployment/compose.production.yml --profile update run --rm \
  rag-update python -m corporate_rag_ingestion update --dry-run --json
```

## Backup and restore

A daily timer keeps the newest seven backups under
`/opt/corporate-rag/backups/production`. Every backup contains a native Qdrant
snapshot, a consistent SQLite registry copy, runtime artifacts, source files,
and recovery configuration. Backup directories are mode `0700` because the
configuration may contain secrets.

```bash
sudo systemctl start corporate-rag-backup.service
sudo journalctl -u corporate-rag-backup.service -n 100 --no-pager
sudo ls -ld /opt/corporate-rag/backups/production/backup-*
```

Verify or test the latest backup without touching production:

```bash
latest=$(sudo find /opt/corporate-rag/backups/production -mindepth 1 -maxdepth 1 \
  -type d -name 'backup-*' -printf '%p\n' | sort | tail -1)
sudo corporate-rag-restore verify "$latest"
sudo corporate-rag-restore test "$latest"
```

The test restore creates a temporary Qdrant collection, compares its point
count with the manifest, and deletes that test collection.

Production restore is deliberately guarded and requires downtime:

```bash
sudo systemctl stop corporate-rag.service
cd /opt/corporate-rag/app
sudo docker compose -f deployment/compose.production.yml stop rag-api rag-frontend
sudo corporate-rag-restore production "$latest" --confirm-production
sudo systemctl start corporate-rag.service
```

The replaced filesystem data is retained under
`/opt/corporate-rag/restore-rollback/<timestamp>`.

## Restart and recovery

```bash
sudo systemctl restart corporate-rag.service
sudo docker compose -f /opt/corporate-rag/app/deployment/compose.production.yml \
  restart rag-api rag-frontend
sudo systemctl status corporate-rag.service --no-pager
```

`corporate-rag.service` waits for Qdrant and Ollama, starts API/frontend, and
does not perform indexing. It is enabled for boot. The update and backup timers
are persistent, so a missed scheduled run is executed after the server returns.

## If `/ready` is false

1. Read the JSON response and identify `qdrant`, `ollama`, `embedding`, or
   `reranker`.
2. Check `nvidia-smi`, free disk, API logs, Qdrant, and Ollama endpoints.
3. Verify the two model paths under `/opt/models` still exist.
4. Restart only `rag-api`; do not reindex unless the collection is actually
   missing or damaged.
5. If startup still fails, restore the latest verified backup or roll the app
   directory back to `/opt/corporate-rag/backups/app-before-<stage>-<timestamp>`.

## If disk space is low

```bash
df -h /
sudo docker system df
sudo du -sh /opt/corporate-rag/backups/production/* 2>/dev/null | sort -h
sudo journalctl --disk-usage
```

First remove only expired Corporate RAG backups after confirming a newer
verified backup exists. Docker image/build-cache cleanup is a separate operator
decision; never remove volumes and never delete Qdrant storage manually.

## Security checks

```bash
sudo ss -lntp | grep -E ':(3001|8010|6333|11434)\b'
sudo docker inspect corporate-rag-api corporate-rag-frontend \
  --format '{{.Name}} privileged={{.HostConfig.Privileged}} readonly={{.HostConfig.ReadonlyRootfs}} log={{.HostConfig.LogConfig.Type}}'
```

API stays on loopback. The browser talks only to the Next.js frontend; Next.js
proxies API/file requests. Qdrant and Ollama must not be reachable from employee
networks. Until IT publishes the frontend port, use an SSH tunnel:

```powershell
ssh -N -L 3001:127.0.0.1:3001 rulko@ktzai.ktzholding.ru
```
