# Графики прокатов (Stage 15)

## Рабочая схема

Графики прокатов являются отдельным логическим набором данных внутри текущего
Corporate RAG. Они не добавляются в Qdrant и не смешиваются с документами базы
знаний:

- штатный `corporate-rag-update.timer` по-прежнему запускается каждый час;
- перед обычным инкрементальным обновлением его скрипт вызывает
  `rolling-sync`;
- `rolling-sync` реально скачивает Google XLSX не чаще одного раза в 24 часа,
  поэтому тот же hourly timer служит автоматическим повтором после сетевой
  ошибки;
- XLSX проверяется как ZIP, открывается `openpyxl(data_only=True)`, сверяются
  все 29 ожидаемых листов и минимальное число распознанных позиций;
- при неизменном SHA-256 и версии нормализатора обработка пропускается;
- новый SQLite-снимок сначала полностью создаётся во временном каталоге и
  только затем атомарно заменяет `current.sqlite3`;
- при любой ошибке рабочий снимок остаётся без изменений.

Рабочие данные находятся только в:

```text
/opt/corporate-rag/storage/rolling-schedules/
  current.sqlite3
  status.json
  snapshots/<sha256>.xlsx
```

Хранятся два последних исходных XLSX. Исходная Google-таблица не изменяется.

## Поиск и ответы

Runtime сначала проверяет, относится ли вопрос к плану прокатов. Обычный вопрос
сразу продолжает прежний путь Qdrant → reranker → LLM. Для вопроса о графике:

1. из текста извлекаются стан/лист, точный типоразмер, вид продукции, марка
   стали и период;
2. выполняется точный SQL-поиск по нормализованному снимку;
3. только найденные структурированные факты передаются Qwen для естественной
   формулировки ответа; модель не выполняет поиск и не имеет права менять
   даты, размеры, станы или объёмы;
4. проверенные ссылки вида `#gid=<gid>&range=B15:P15`, время синхронизации и
   предупреждения добавляются системой; при недоступности LLM остаётся
   детерминированный резервный ответ;
5. без явно указанной даты показываются только текущие и будущие позиции;
6. `57×3` и `57×3,5` имеют разные канонические ключи и не смешиваются.

Маршрутизация понимает не только фиксированные шаблоны, но и естественные
формулировки про график прокатов, план производства, выпуск, загрузку стана,
ремонты, простои и переналадки. Процедурные вопросы вроде «как оформить
производственный заказ» остаются в обычном документном RAG. Запрос ремонта или
простоя фильтруется по служебным событиям и не возвращает производственные
позиции.

Ошибки Excel (`#VALUE!`, `#REF!`, `#NAME?` и другие) никогда не превращаются в
производственные значения. Характеристики берутся из исходного наименования,
только если их можно извлечь однозначно. Неизвестная единица, дата, объём или
характеристика остаётся неизвестной. Лист с повреждённым импортом получает
статус `unavailable`, а отсутствие результата на нём не трактуется как
отсутствие плана.

## Ручные команды

Запустить загрузку независимо от 24-часового интервала:

```bash
cd /opt/corporate-rag/app
sudo docker compose -f deployment/compose.production.yml \
  --profile update run --rm rag-update \
  python -m corporate_rag_ingestion rolling-sync --force --json
```

Показать состояние активного снимка без записи:

```bash
sudo docker exec corporate-rag-api \
  python -m corporate_rag_ingestion rolling-status --json
```

Журнал ежедневной загрузки находится в журнале уже существующего systemd job:

```bash
sudo journalctl -u corporate-rag-update.service --since today --no-pager
sudo systemctl list-timers corporate-rag-update.timer --no-pager
```

## Проверка после развёртывания

```bash
curl -sS http://127.0.0.1:8010/ready | python3 -m json.tool

curl -sS -N --max-time 60 \
  -X POST http://127.0.0.1:3001/api/answer/stream \
  -H 'Content-Type: application/json' \
  -d '{"question":"Когда планируется прокат трубы 57×3,5?"}'
```

В `/ready` блок `rolling_schedules` диагностический: отсутствие первого
снимка не делает обычную базу знаний неготовой. Для ответа по графикам при этом
будет честно сообщено, что проверенный снимок ещё не загружен.

## Развёртывание инкрементального архива

Архив Stage 15 содержит только изменённые файлы с верхним каталогом `app/`.
Он накладывается на существующий `/opt/corporate-rag/app`; Docker-образы в
архив не входят и собираются на сервере по обычной для проекта схеме.

```bash
cd /opt/corporate-rag
deploy_stamp="$(date +%Y%m%d-%H%M%S)"
sudo cp -a app "backups/app-before-stage15-rolling-$deploy_stamp"

unpack_dir="$(mktemp -d /tmp/corporate-rag-stage15.XXXXXX)"
tar -xzf /tmp/corporate-rag-stage15-rolling-schedules.tar.gz -C "$unpack_dir"
sudo cp -a "$unpack_dir/app/." /opt/corporate-rag/app/

sudo install -d -m 0770 -o 10001 -g 10001 \
  /opt/corporate-rag/storage/rolling-schedules
sudo install -m 0755 \
  /opt/corporate-rag/app/deployment/scripts/corporate-rag-update \
  /usr/local/sbin/corporate-rag-update

cd /opt/corporate-rag/app
sudo docker compose -f deployment/compose.production.yml \
  build rag-api rag-frontend
sudo docker compose -f deployment/compose.production.yml \
  --profile update build rag-update

sudo docker compose -f deployment/compose.production.yml \
  --profile update run --rm rag-update \
  python -m corporate_rag_ingestion rolling-sync --force --json

sudo docker compose -f deployment/compose.production.yml \
  up -d --force-recreate rag-api rag-frontend
```

После прогрева:

```bash
sudo docker compose -f deployment/compose.production.yml ps
curl -sS http://127.0.0.1:8010/ready | python3 -m json.tool
curl -sS -o /dev/null -w 'Сайт: HTTP %{http_code}\n' \
  http://127.0.0.1:3001/
sudo systemctl is-enabled corporate-rag-update.timer
sudo systemctl is-active corporate-rag-update.timer
sudo docker exec corporate-rag-api \
  python -m corporate_rag_ingestion rolling-status --json
```

Временный каталог распаковки можно удалить после проверки. Снимок графиков
обычно занимает около 2,3 МБ для одной версии; при штатном хранении двух XLSX
ожидаемый устойчивый объём — примерно 3,5–4 МБ.

## Откат

Код откатывается восстановлением предыдущего каталога `app` и пересозданием
API/frontend из прежних тегов Compose. Набор
`storage/rolling-schedules` не влияет на старую версию и может оставаться на
диске. Удалять его для отката не требуется.

Без удаления неудачной версии:

```bash
cd /opt/corporate-rag
rollback_stamp="$(date +%Y%m%d-%H%M%S)"
sudo mv app "backups/app-stage15-failed-$rollback_stamp"
sudo cp -a backups/app-before-stage15-rolling-<TIMESTAMP> app
sudo install -m 0755 app/deployment/scripts/corporate-rag-update \
  /usr/local/sbin/corporate-rag-update
cd app
sudo docker compose -f deployment/compose.production.yml \
  up -d --force-recreate rag-api rag-frontend
```
