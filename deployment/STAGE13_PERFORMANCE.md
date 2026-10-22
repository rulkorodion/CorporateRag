# Нагрузочное тестирование и A/B-профили

> Этот файл сохраняет воспроизводимую методику подбора production-параметров. Указанные теги образов относятся к тому этапу измерений. Для текущего развёртывания нужно использовать теги из `compose.production.yml`.

Все замеры выполняются на production-like сервере. Локальные результаты не заменяют серверные.

## Сборка и передача архива

Из каталога проекта в Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass `
  -File .\deployment\prepare_bundle.ps1 `
  -SkipKnowledgeBase
Get-FileHash .\tmp\corporate-rag-stage13-performance.tar.gz -Algorithm SHA256
scp .\tmp\corporate-rag-stage13-performance.tar.gz `
  user@server.example.com:/tmp/
```

## Развёртывание с возможностью отката

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

Команда профиля выводит каталог резервной копии настроек. Этот путь нужно сохранить для отката.

## Проверка готовности и потока

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

В потоке должны быть `status`, один или несколько `delta` и один `final`. Только `final.data` считается проверенным ответом.

## A/B-тест параллелизма

Каталог отчётов создаётся один раз:

```bash
sudo install -d -o 10001 -g 10001 -m 0750 \
  /opt/corporate-rag/storage/reports/performance-stage13
```

Для каждого профиля нужно применить его, прогреть Ollama вне измерения и запустить одинаковую нагрузку:

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
```

Профили проверяются в одинаковом порядке:

```bash
mode=1-1-1
mode=1-1-2
mode=2-1-1
mode=2-2-1
mode=2-2-2
```

`2-2-2` одновременно устанавливает API LLM concurrency и `OLLAMA_NUM_PARALLEL=2`. При OOM, CUDA error или неготовном `/ready` нужно сразу вернуть baseline:

```bash
sudo corporate-rag-performance-profile apply 1-1-1
```

## A/B-тест бюджета контекста

После выбора стабильного профиля параллелизма проверяются per-request бюджеты. Production default не меняется:

```bash
for budget in 6000 5000 4000; do
  sudo docker run --rm --network host --gpus all \
    -v /opt/corporate-rag/storage/reports/performance-stage13:/reports \
    corporate-rag-api:0.2.1-stage13-performance \
    python /app/tools/load_test_api.py \
    --base-url http://127.0.0.1:8010 \
    --mode "context-$budget" \
    --users 1 --requests 10 \
    --max-context-tokens "$budget" \
    --include-answer-text \
    --output "/reports/context-$budget.json"
done
```

Нужно сравнивать `prompt_estimate_ratio`, `prompt_eval_count`, ссылки на источники и полноту ответа. До завершения этой проверки default остаётся 6000.

## A/B-тест лимита ответа

```bash
for limit in 512 1024 2048; do
  sudo docker run --rm --network host --gpus all \
    -v /opt/corporate-rag/storage/reports/performance-stage13:/reports \
    corporate-rag-api:0.2.1-stage13-performance \
    python /app/tools/load_test_api.py \
    --base-url http://127.0.0.1:8010 \
    --mode "output-$limit" \
    --users 1 --requests 10 \
    --num-predict "$limit" \
    --include-answer-text \
    --output "/reports/output-$limit.json"
done
```

`done_reason=length`, оборванное предложение, ухудшение валидации ссылок или пропущенные шаги означают, что лимит слишком мал. Без реальных измерений output cap не вводится.

## Сводка и выгрузка результатов

```bash
sudo docker run --rm \
  -v /opt/corporate-rag/storage/reports/performance-stage13:/reports \
  corporate-rag-api:0.2.1-stage13-performance \
  python /app/tools/summarize_performance_reports.py /reports/*.json \
  | sudo tee /opt/corporate-rag/storage/reports/performance-stage13/summary.md
sudo tar -czf /tmp/corporate-rag-stage13-results.tar.gz \
  -C /opt/corporate-rag/storage/reports performance-stage13
```

Итоговые профили и бюджеты выбираются по этим отчётам, а не по предположениям.

## Откат

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
