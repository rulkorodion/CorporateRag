# База знаний КТЗ — frontend

Production chat UI for the corporate RAG FastAPI backend.

## Local development

```powershell
Set-Location frontend
Copy-Item .env.example .env.local
npm install
npm run dev
```

`RAG_API_URL` is used only by Next.js server route handlers. The browser talks to
same-origin `/api/*` routes and never connects directly to Qdrant or Ollama.

## Verification

```powershell
npm test
npm run typecheck
npm run lint
npm run build
```

## Production Docker

The root `deployment/compose.production.yml` adds `rag-frontend` on loopback port
`3001`. It proxies to the existing FastAPI service on `127.0.0.1:8010` and has no
GPU or model volumes. The editor area is available at `/admin`; credentials are
read only at runtime from `/opt/corporate-rag/config/admin.env` and are never
included in the browser bundle.

Uploaded documents first enter `/opt/corporate-rag/admin-upload`. The frontend
can read the source tree but cannot modify it. A host systemd path unit validates
and atomically promotes queued files, then runs the existing incremental updater.
