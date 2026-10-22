# Corporate RAG

Корпоративный ИИ-помощник для поиска по базе знаний и точных ответов по производственным графикам. Проект объединяет обработку корпоративных документов, гибридный поиск, переранжирование, локальную LLM через Ollama, веб-интерфейс и production-развёртывание в Docker.

> Сами документы базы знаний, рабочие базы данных, снимки графиков, модели и секреты в репозиторий не входят.

## Возможности

- инкрементальная загрузка PDF, DOCX, DOC, RTF, XLSX, изображений и медиафайлов;
- нормализация текста, таблиц, структуры документа и метаданных;
- OCR-конвейер для сканов с кэшем, манифестами и проверкой происхождения данных;
- структурный чанкинг текста и таблиц;
- гибридный поиск: dense embeddings + BM25 + RRF в Qdrant;
- reranker на базе `Qwen3-Reranker-0.6B`;
- формирование ответов через Ollama с проверкой ссылок на реальные источники;
- точный структурированный поиск по графикам прокатов с ежедневной синхронизацией;
- Next.js-интерфейс с потоковыми ответами, источниками и просмотром документов;
- админка для загрузки файлов, запуска обновления и просмотра обезличенной статистики;
- ONLYOFFICE Document Server для просмотра офисных файлов;
- Docker Compose, healthcheck, systemd-автозапуск, резервное копирование и безопасные обновления.

## Архитектура

```text
Документы
    ↓
Сканер → реестр версий → нормализация/OCR → чанки
    ↓
Dense + BM25 → Qdrant → RRF → reranker → контекст
    ↓
Ollama LLM → FastAPI → Next.js → Nginx

Графики прокатов → валидация XLSX → SQLite-снимок → точный поиск → общий чат
```

Вопросы по документам проходят через обычный RAG-конвейер. Вопросы о планах производства направляются в структурированный поиск: фильтрацию и подсчёты выполняет backend, а LLM формулирует понятный ответ без изменения фактов.

## Структура репозитория

| Путь | Назначение |
|---|---|
| `src/corporate_rag_ingestion/` | ingestion, OCR, chunking, retrieval, API и графики прокатов |
| `frontend/` | веб-интерфейс Next.js |
| `deployment/` | Dockerfile, Compose, Nginx, systemd и серверные скрипты |
| `tools/` | запуск API, индексация, smoke/load-тесты и benchmark |
| `tests/` | backend-тесты |
| `config/` | несекретные настройки обработки |

## Требования

- Python 3.11+;
- Node.js 22+;
- Docker Engine с Compose Plugin для production;
- Qdrant;
- Ollama с настроенной LLM;
- NVIDIA Container Toolkit и CUDA-совместимый GPU для production-профиля;
- LibreOffice для обработки старых `.doc` и `.rtf`.

## Быстрый запуск для разработки

### Бэкенд

```powershell
git clone https://github.com/rulkorodion/CorporateRag.git
cd CorporateRag

py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev,retrieval,api]"
Copy-Item .env.example .env
```

Перед запуском заполните `.env`: укажите адреса Qdrant и Ollama, имя LLM, локальные пути к моделям и данным. Затем:

```powershell
.\.venv\Scripts\python.exe .\tools\run_api_production.py
```

API по умолчанию доступен на `http://127.0.0.1:8010`.

### Фронтенд

```powershell
cd frontend
Copy-Item .env.example .env.local
npm ci
npm.cmd run dev
```

Откройте `http://127.0.0.1:3000`. Frontend обращается к backend только через свои server-side route handlers.

## Основные CLI-команды

```powershell
# Сканирование и нормализация новых/изменённых файлов
.\.venv\Scripts\python.exe -m corporate_rag_ingestion ingest

# Построение чанков и индекса
.\.venv\Scripts\python.exe -m corporate_rag_ingestion chunk
.\.venv\Scripts\python.exe -m corporate_rag_ingestion index

# Инкрементальное обновление
.\.venv\Scripts\python.exe -m corporate_rag_ingestion update --json

# Поиск и полный RAG-ответ
.\.venv\Scripts\python.exe -m corporate_rag_ingestion search "Вопрос" --mode rerank --top-k 10
.\.venv\Scripts\python.exe -m corporate_rag_ingestion answer "Вопрос" --json

# Ручная синхронизация графиков прокатов
.\.venv\Scripts\python.exe -m corporate_rag_ingestion rolling-sync --force --json
.\.venv\Scripts\python.exe -m corporate_rag_ingestion rolling-status --json
```

## API

| Метод | Маршрут | Назначение |
|---|---|---|
| `POST` | `/api/answer` | цельный ответ |
| `POST` | `/api/answer/stream` | потоковый ответ с событиями очереди |
| `GET` | `/health` | состояние процесса, очередей и метрик |
| `GET` | `/ready` | готовность Qdrant, embedding, reranker, Ollama и графиков |
| `GET` | `/api/analytics/summary` | агрегированная статистика для админки |

## Тесты

```powershell
# Бэкенд
.\.venv\Scripts\python.exe -m pytest

# Фронтенд
cd frontend
npm.cmd test
npm.cmd run typecheck
npm.cmd run build
```

## Развёртывание на сервере

Серверная схема описана в [`deployment/compose.production.yml`](deployment/compose.production.yml). Она запускает:

- `rag-api` — FastAPI, embedding и reranker;
- `rag-frontend` — Next.js;
- `rag-gateway` — Nginx на порту `3001`;
- `onlyoffice` — Document Server на loopback-порту `8082`;
- `rag-update` — профиль инкрементального обновления;
- `rag-index` — профиль отдельной индексации.

Пример запуска:

```bash
cp deployment/production.env.example /opt/corporate-rag/config/production.env
cp deployment/admin.env.example /opt/corporate-rag/config/admin.env

docker compose -f deployment/compose.production.yml build rag-api rag-frontend
docker compose -f deployment/compose.production.yml up -d rag-api onlyoffice rag-frontend rag-gateway
docker compose -f deployment/compose.production.yml ps
```

Перед запуском замените все placeholder-значения в `production.env` и `admin.env`, сгенерируйте секреты и ограничьте доступ к файлам правами файловой системы.

### Проверка

```bash
curl -sS http://127.0.0.1:8010/ready | python3 -m json.tool
curl -sS -o /dev/null -w 'Site HTTP %{http_code}\n' http://127.0.0.1:3001/
curl -sS -o /dev/null -w 'ONLYOFFICE HTTP %{http_code}\n' http://127.0.0.1:3001/onlyoffice/healthcheck
```

Автозапуск и восстановление после reboot реализованы через [`deployment/systemd/corporate-rag.service`](deployment/systemd/corporate-rag.service). Обновления и резервное копирование также имеют свои systemd services/timers.

## Безопасность данных

- `.env`, база знаний, OCR-результаты, runtime storage, снимки XLSX, архивы и админские очереди исключены из Git;
- файлы `*.env.example` содержат только шаблоны;
- исходная база знаний монтируется в production только для чтения;
- контейнеры запускаются с `read_only`, `no-new-privileges`, ограничением capabilities и ротацией логов;
- тексты вопросов, ответы и содержимое документов не сохраняются в агрегированной телеметрии.

## Документация

- [Развёртывание на сервере](deployment/README.md)
- [Регламент эксплуатации](deployment/RUNBOOK.md)
- [Графики прокатов](deployment/ROLLING_SCHEDULES.md)
- [Настройки окружения](.env.example)
