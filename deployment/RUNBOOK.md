# Регламент эксплуатации Corporate RAG

Корневой каталог production: `/opt/corporate-rag`. Compose-файл: `/opt/corporate-rag/app/deployment/compose.production.yml`.

## Ежедневная проверка

```bash
cd /opt/corporate-rag/app
sudo docker compose -f deployment/compose.production.yml ps
curl -fsS http://127.0.0.1:8010/health | python3 -m json.tool
curl -fsS http://127.0.0.1:8010/ready | python3 -m json.tool
curl -fsS -o /dev/null -w 'frontend HTTP %{http_code}\n' http://127.0.0.1:3001/
curl -fsS -o /dev/null -w 'ONLYOFFICE HTTP %{http_code}\n' \
  http://127.0.0.1:3001/onlyoffice/healthcheck
nvidia-smi
df -h /
```

Ожидаемый результат: все четыре контейнера имеют статус `healthy`, `/ready` возвращает `"ready": true`, а frontend и ONLYOFFICE — HTTP 200.

## Логи

Compose использует logging driver `local`: до 20 МБ на файл и пять файлов на контейнер.

```bash
sudo docker compose -f deployment/compose.production.yml logs --tail=200 rag-api
sudo docker compose -f deployment/compose.production.yml logs --tail=200 rag-frontend
sudo docker compose -f deployment/compose.production.yml logs --tail=200 rag-gateway
sudo journalctl -u corporate-rag-update.service -n 100 --no-pager
sudo journalctl -u corporate-rag-backup.service -n 100 --no-pager
sudo journalctl -u corporate-rag-upload.service -n 100 --no-pager
```

## Внешние зависимости

```bash
curl -fsS http://127.0.0.1:6333/collections/corporate_knowledge_chunks_v1 |
  python3 -m json.tool
curl -fsS http://127.0.0.1:11434/api/tags | python3 -m json.tool
systemctl is-enabled ollama
sudo docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' qdrant
```

Qdrant и Ollama уже существуют на сервере. Их нельзя пересоздавать из Compose-проекта Corporate RAG.

## Инкрементальное обновление

Почасовые запуски сериализуются через `/run/lock/corporate-rag-maintenance.lock`. Сам pipeline имеет дополнительную data-lock и быстро завершается, если изменений нет.

```bash
systemctl list-timers --all 'corporate-rag-*'
sudo systemctl start corporate-rag-update.service
sudo systemctl status corporate-rag-update.service --no-pager
sudo journalctl -u corporate-rag-update.service -n 100 --no-pager
```

## Загрузка файлов редактором

Админка открывается по пути `/admin`. Загруженные байты остаются в изолированной очереди; только host service может перенести проверенный файл в source tree.

```bash
systemctl status corporate-rag-upload.path --no-pager
sudo journalctl -u corporate-rag-upload.service -n 100 --no-pager
sudo find /opt/corporate-rag/admin-upload/queue \
  -mindepth 1 -maxdepth 2 -type f
```

Смена пароля без пересборки образа:

```bash
sudo corporate-rag-admin-credentials editor
cd /opt/corporate-rag/app
sudo docker compose -f deployment/compose.production.yml \
  up -d --force-recreate rag-frontend
```

Обработка upload-очереди имеет debounce 30 секунд. Upload service и updater используют общую maintenance-блокировку, поэтому OCR, embedding, backup и restore не пересекаются.

## Резервное копирование

Ежедневный timer хранит семь последних полных копий в `/opt/corporate-rag/backups/production`. Каждая копия включает нативный Qdrant snapshot, согласованную копию SQLite, runtime-данные, исходные файлы и recovery-конфигурацию. Каталоги имеют режим `0700`.

```bash
sudo systemctl start corporate-rag-backup.service
sudo journalctl -u corporate-rag-backup.service -n 100 --no-pager
sudo ls -ld /opt/corporate-rag/backups/production/backup-*
```

Проверка последней копии без влияния на production:

```bash
latest=$(sudo find /opt/corporate-rag/backups/production \
  -mindepth 1 -maxdepth 1 -type d -name 'backup-*' \
  -printf '%p\n' | sort | tail -1)
sudo corporate-rag-restore verify "$latest"
sudo corporate-rag-restore test "$latest"
```

Test restore создаёт временную Qdrant-коллекцию, сверяет point count с манифестом и удаляет тестовую коллекцию.

## Восстановление

Восстановление production требует остановки сервиса и явного флага подтверждения:

```bash
sudo systemctl stop corporate-rag.service
cd /opt/corporate-rag/app
sudo docker compose -f deployment/compose.production.yml \
  stop rag-api rag-frontend
sudo corporate-rag-restore production "$latest" --confirm-production
sudo systemctl start corporate-rag.service
```

Заменённые файловые данные сохраняются в `/opt/corporate-rag/restore-rollback/<timestamp>`.

## Перезапуск и boot recovery

```bash
sudo systemctl restart corporate-rag.service
sudo systemctl status corporate-rag.service --no-pager -l
sudo docker compose \
  -f /opt/corporate-rag/app/deployment/compose.production.yml \
  restart rag-api rag-frontend
```

`corporate-rag.service` ждёт Qdrant и Ollama, после чего запускает API и frontend. Индексация при boot не выполняется. Persistent timers догоняют пропущенные update и backup после возврата сервера.

## Если `/ready` возвращает `false`

1. По JSON-ответу определить неготовый компонент: `qdrant`, `ollama`, `embedding` или `reranker`.
2. Проверить `nvidia-smi`, свободное место, API-логи, Qdrant и Ollama.
3. Убедиться, что каталоги embedding и reranker в `/opt/models` существуют.
4. Перезапустить только `rag-api`. Не переиндексировать данные, если коллекция не повреждена.
5. Если запуск по-прежнему невозможен, использовать последнюю проверенную backup-копию или откат кода.

## Если заканчивается место

```bash
df -h /
sudo docker system df
sudo du -sh /opt/corporate-rag/backups/production/* 2>/dev/null | sort -h
sudo journalctl --disk-usage
```

Сначала можно удалять только просроченные backup-копии, убедившись в наличии более новой проверенной копии. Очистка Docker images/build cache — отдельное решение. Volumes и Qdrant storage удалять нельзя.

## Проверка безопасности

```bash
sudo ss -lntp | grep -E ':(3001|8010|6333|11434)\b'
sudo docker inspect corporate-rag-api corporate-rag-frontend \
  --format '{{.Name}} privileged={{.HostConfig.Privileged}} readonly={{.HostConfig.ReadonlyRootfs}} log={{.HostConfig.LogConfig.Type}}'
```

API, Qdrant и Ollama должны оставаться на loopback. Браузер обращается только к Next.js/Nginx. Для временного операторского доступа можно использовать SSH-туннель:

```powershell
ssh -N -L 3001:127.0.0.1:3001 user@server.example.com
```
