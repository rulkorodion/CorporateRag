# Развёртывание Corporate RAG

Схема рассчитана на сервер с уже работающими Ollama и Qdrant Server. Compose-проект не создаёт и не изменяет эти сервисы. API и updater используют заранее подготовленные CUDA/PyTorch-слои и модели.

## Схема сервисов

- `corporate-rag-api` — один FastAPI-процесс на `127.0.0.1:8010`;
- `corporate-rag-frontend` — Next.js на `127.0.0.1:3002`;
- `corporate-rag-gateway` — единая точка входа Nginx на порту `3001`;
- `corporate-rag-onlyoffice` — Document Server в режиме просмотра, внутренний порт `127.0.0.1:8082`;
- Qdrant Server — внешний для Compose сервис на `127.0.0.1:6333`;
- Ollama — внешний для Compose сервис на `127.0.0.1:11434`;
- embedding и reranker — CUDA device 0;
- модели монтируются только для чтения из `/opt/models`;
- чанки, нормализованные данные, assets и исходные документы берутся из `/opt/corporate-rag`.

PDF, DOC/DOCX/RTF и XLS/XLSX открываются во встроенном ONLYOFFICE. API подписывает конфигурацию просмотра JWT-токеном и выдаёт файлы только по проверенным ID документа и версии. Изображения и MP4 показываются средствами браузера; для MP4 поддержаны byte-range запросы.

## Обычный жизненный цикл

Команды выполняются из `/opt/corporate-rag/app`:

```bash
sudo docker compose -f deployment/compose.production.yml build rag-api rag-frontend
sudo docker compose -f deployment/compose.production.yml up -d \
  rag-api onlyoffice rag-frontend rag-gateway
sudo docker compose -f deployment/compose.production.yml ps
sudo docker compose -f deployment/compose.production.yml logs -f --tail=200 rag-api
sudo docker compose -f deployment/compose.production.yml restart rag-api
sudo docker compose -f deployment/compose.production.yml stop rag-api
```

`down -v` использовать нельзя: даже при bind mounts деструктивные флаги не должны входить в штатную процедуру.

## Индексация

`rag-index` — отдельный Compose-профиль. Он синхронизирует чанки с коллекцией `corporate_knowledge_chunks_v1` и завершается. Обычные `up` и `restart` его не запускают.

```bash
sudo docker compose -f deployment/compose.production.yml \
  --profile index run --rm rag-index
```

## Инкрементальные обновления

`rag-update` — отдельный профиль с read-only доступом к базе знаний и read-write доступом к `storage`. Он последовательно обрабатывает новые, изменённые и удалённые файлы, после чего завершается.

Сначала можно посмотреть план без изменений:

```bash
sudo docker compose -f deployment/compose.production.yml \
  --profile update run --rm rag-update \
  python -m corporate_rag_ingestion update --dry-run --json
```

Запуск реального обновления:

```bash
sudo docker compose -f deployment/compose.production.yml \
  --profile update run --rm rag-update
```

Принудительная обработка одного пути, glob-шаблона или UUID:

```bash
sudo docker compose -f deployment/compose.production.yml \
  --profile update run --rm rag-update \
  python -m corporate_rag_ingestion update \
  --force 'Отдел ИТ/Инструкция.pdf' --json
```

Запуски сериализуются файлом `/app/storage/incremental-update.lock`. Отчёты JSON атомарно записываются в `storage/reports/incremental`. Пустой реестр по умолчанию считается ошибкой, чтобы не запустить случайную полную пересборку.

Тот же почасовой systemd timer проверяет графики прокатов. Сама синхронизация имеет 24-часовой due-check, SHA-256 no-op и атомарную замену SQLite-снимка. Подробности — в [`ROLLING_SCHEDULES.md`](ROLLING_SCHEDULES.md).

## Защищённая production-конфигурация

Установка host-level скриптов, systemd units, почасового updater, ежедневного backup и boot recovery:

```bash
cd /opt/corporate-rag/app
sudo bash deployment/install_stage12.sh
```

Логи Docker ограничены, корневые файловые системы контейнеров read-only, Linux capabilities сокращены, CORS ограничен, а выдача файлов проходит проверку ID и версии. Обновление, backup и restore используют общую maintenance-блокировку.

## Загрузка документов через админку

```bash
cd /opt/corporate-rag/app
sudo bash deployment/install_stage13.sh
```

Учётные данны редактора хранятся как PBKDF2-хеш в `/opt/corporate-rag/config/admin.env`. Загрузки попадают в изолированную очередь, повторно валидируются на host, атомарно перемещаются в source tree и запускают incremental updater. Почасовой timer остаётся резервным механизмом.

## Потоковые ответы и нагрузочные проверки

API выдаёт NDJSON-поток через `/api/answer/stream`. Сначала идут статус и промежуточные фрагменты; последнее событие `final` содержит проверенный ответ, источники, предупреждения и телеметрию.

```bash
sudo docker exec corporate-rag-api python /app/tools/smoke_test_production.py
sudo docker exec corporate-rag-api python /app/tools/load_test_api.py \
  --base-url http://127.0.0.1:8010 \
  --ollama-base-url http://127.0.0.1:11434 \
  --concurrency 2 5 --requests 10 --output /tmp/api-load-test.json
```

Профили параллелизма, методика A/B-тестов и откат описаны в [`STAGE13_PERFORMANCE.md`](STAGE13_PERFORMANCE.md).

## Дополнительная документация

- [`RUNBOOK.md`](RUNBOOK.md) — штатные проверки, восстановление и безопасность;
- [`ROLLING_SCHEDULES.md`](ROLLING_SCHEDULES.md) — обновление и поиск по графикам прокатов;
- [`STAGE13_PERFORMANCE.md`](STAGE13_PERFORMANCE.md) — нагрузочные и A/B-проверки.
