# Фронтенд базы знаний

Веб-интерфейс корпоративного RAG-помощника на Next.js. Поддерживает обычные и потоковые ответы, карточки источников, просмотр документов, админку и обезличенную статистику.

## Локальная разработка

```powershell
Set-Location frontend
Copy-Item .env.example .env.local
npm ci
npm.cmd run dev
```

Интерфейс будет доступен на `http://127.0.0.1:3000`.

`RAG_API_URL` используется только в server-side маршрутах Next.js. Браузер обращается к same-origin маршрутам `/api/*` и не подключается напрямую к Qdrant или Ollama.

## Проверка

```powershell
npm.cmd test
npm.cmd run typecheck
npm.cmd run lint
npm.cmd run build
```

## Docker-развёртывание

Корневой `deployment/compose.production.yml` запускает сервис `rag-frontend` на loopback-порту `3002`. Nginx публикует его на порту `3001`, а frontend передаёт API-запросы в FastAPI на `127.0.0.1:8010`. GPU и каталоги моделей frontend не требуются.

Админка доступна по пути `/admin`. Учётные данны читаются при запуске из `/opt/corporate-rag/config/admin.env` и не попадают в браузерную сборку.

Загруженные документы сначала попадают в изолированную очередь `/opt/corporate-rag/admin-upload`. Frontend может читать исходную базу знаний, но не может её изменять. Host-level systemd service повторно проверяет файлы, атомарно перемещает их в базу знаний и запускает инкрементальное обновление.
