from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from math import ceil
from zoneinfo import ZoneInfo

from ..api.models import (
    APILatency,
    APITelemetry,
    AnswerResponse,
    SchedulePagination,
    ScheduleSelection,
)
from ..context.tokens import StableTokenCounter
from ..generation.models import (
    LLMGenerationRequest,
    LLMGenerationResult,
)
from .intent import (
    ScheduleIntentAnalysis,
    build_intent_request,
    merge_intent_analysis,
    parse_intent_response,
)
from .models import ScheduleQuery, ScheduleRecord
from .normalizer import extract_dimensions
from .search import parse_schedule_query
from .settings import RollingScheduleSettings
from .store import RollingScheduleStore, StoredSheetDiagnostic


MOSCOW = ZoneInfo("Europe/Moscow")
LLM_FACT_LIMIT = 10
LLM_LIST_LIMIT = 6
FALLBACK_FACT_LIMIT = 4
FULL_PAGE_SIZE = 25

ROLLING_SYSTEM_PROMPT = """\
Ты — корпоративный ИИ-помощник КТЗ. Составь естественный и короткий ответ по
графику производства на основании только переданных проверенных фактов.

Критические правила:
1. Сначала прямо ответь на вопрос, затем добавь только полезные подробности.
2. Используй только название стана/площадки, дату и номенклатуру из переданных фактов.
   Не сообщай объёмы, тоннажи, производительность, единицы измерения и другие
   числовые показатели, даже если пользователь их запрашивает.
3. Все подсчёты и суммы уже выполнены backend. Самостоятельно ничего не считай.
4. Для служебных событий используй только service_records, для продукции —
   только production_records. Не смешивай их.
5. position_counts_by_stand — это количество позиций, а не объём производства. Не называй станы с
   большим числом позиций «основными объёмами».
6. Для query_type=count укажи точно matching_records_total и кратко назови период. Не
   перечисляй позиции и не создавай таблицу.
7. Для общего вопроса без прикреплённой таблицы дай сводку по станам, датам и видам
   продукции. Приведи не более 6 примеров, даже если найдено больше строк.
8. Если full_result_attached=true, не переписывай таблицу: кратко подведи итог и
   скажи, что полный результат приведён ниже постранично.
9. Не создавай ссылки, не называй источник данных и не упоминай JSON, SQL, парсер
   или внутреннее устройство.
10. Не добавляй дату синхронизации и технические предупреждения — система
    добавит их после ответа.
11. Обычно достаточно 1–2 коротких абзацев и до 6 пунктов.
"""


@dataclass(frozen=True)
class RollingSchedulePlan:
    fallback: AnswerResponse
    llm_request: LLMGenerationRequest | None
    query: ScheduleQuery
    records: tuple[ScheduleRecord, ...]
    supplied_records: tuple[ScheduleRecord, ...]
    appendix: str
    started: float


def _format_date(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else "дата не подтверждена"


def _format_period(record: ScheduleRecord) -> str:
    if record.start_date and record.end_date and record.end_date != record.start_date:
        return f"{_format_date(record.start_date)}–{_format_date(record.end_date)}"
    return _format_date(record.start_date)


def _format_sync(value: str | None) -> str:
    if not value:
        return "нет успешной синхронизации"
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M МСК")
    except ValueError:
        return value


def _fact(record: ScheduleRecord) -> dict[str, object]:
    return {
        "kind": "service" if record.record_kind == "service" else "product",
        "stand": record.sheet_name,
        "date_from": record.start_date.isoformat() if record.start_date else None,
        "date_to": record.end_date.isoformat() if record.end_date else None,
        "nomenclature": record.product_name,
    }


def _top_counts(values: list[str], *, limit: int = 12) -> dict[str, int]:
    counts = Counter(values)
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit])


def _record_breakdown(records: list[ScheduleRecord], *, today: date) -> dict[str, object]:
    dated = [item for item in records if item.start_date is not None]
    undated = len(records) - len(dated)
    future = [
        item
        for item in dated
        if (item.end_date or item.start_date) >= today  # type: ignore[operator]
    ]
    past = len(dated) - len(future)
    first = min((item.start_date for item in dated), default=None)
    last = max((item.end_date or item.start_date for item in dated), default=None)
    return {
        "total": len(records),
        "dated": len(dated),
        "undated": undated,
        "past_dated": past,
        "current_or_future_dated": len(future),
        "confirmed_date_from": first.isoformat() if first else None,
        "confirmed_date_to": last.isoformat() if last else None,
    }


def _period_description(query: ScheduleQuery, records: list[ScheduleRecord]) -> str:
    dated = [item for item in records if item.start_date]
    actual_from = min((item.start_date for item in dated), default=None)
    actual_to = max((item.end_date or item.start_date for item in dated), default=None)
    if query.period_mode == "all_available":
        if actual_from and actual_to:
            return (
                f"весь актуальный снимок; подтверждённые даты "
                f"с {_format_date(actual_from)} по {_format_date(actual_to)}"
            )
        return "весь актуальный снимок"
    if query.date_from and query.date_to:
        if query.date_from == query.date_to:
            return f"на {_format_date(query.date_from)}"
        return f"с {_format_date(query.date_from)} по {_format_date(query.date_to)}"
    if query.date_from:
        end = actual_to or query.date_from
        return f"с {_format_date(query.date_from)} по {_format_date(end)}"
    if query.date_to:
        start = actual_from or query.date_to
        return f"с {_format_date(start)} по {_format_date(query.date_to)}"
    return "без ограничения по дате"


def _select_representatives(records: list[ScheduleRecord]) -> tuple[ScheduleRecord, ...]:
    selected: list[ScheduleRecord] = []
    seen_ids: set[str] = set()
    seen_stands: set[str] = set()
    for record in records:
        if record.sheet_name not in seen_stands:
            selected.append(record)
            seen_ids.add(record.record_id)
            seen_stands.add(record.sheet_name)
        if len(selected) >= LLM_FACT_LIMIT:
            return tuple(selected)
    for record in records:
        if record.record_id not in seen_ids:
            selected.append(record)
            seen_ids.add(record.record_id)
        if len(selected) >= LLM_FACT_LIMIT:
            break
    return tuple(selected)


def _diagnostic_warning(
    diagnostics: list[StoredSheetDiagnostic], *, found_records: bool, details_matter: bool
) -> str | None:
    unavailable = [item.sheet_name for item in diagnostics if item.status == "unavailable"]
    partial = [item.sheet_name for item in diagnostics if item.status == "partial"]
    if unavailable:
        shown = ", ".join(unavailable[:4])
        suffix = " и другие" if len(unavailable) > 4 else ""
        return (
            f"На листах {shown}{suffix} исходный импорт недоступен; по ним нельзя "
            "подтвердить наличие или отсутствие плана."
        )
    if partial and found_records and details_matter:
        shown = ", ".join(partial[:4])
        suffix = " и других" if len(partial) > 4 else ""
        return (
            f"На листах {shown}{suffix} часть вспомогательных значений не рассчитана; "
            "в ответ включены только однозначно извлечённые данные."
        )
    if partial and not found_records:
        return "Часть подходящих листов заполнена не полностью, поэтому отсутствие плана не подтверждено."
    return None


def _full_page(query: ScheduleQuery, records: list[ScheduleRecord]) -> str:
    total_pages = max(ceil(len(records) / FULL_PAGE_SIZE), 1)
    page = min(max(query.page, 1), total_pages)
    start = (page - 1) * FULL_PAGE_SIZE
    page_records = records[start : start + FULL_PAGE_SIZE]
    lines = [
        f"### Позиции плана — страница {page} из {total_pages}",
        "",
        f"Период выборки: **{_period_description(query, records)}**.",
        "",
        "| Стан / площадка | Дата | Номенклатура |",
        "|---|---|---|",
    ]
    for record in page_records:
        safe_name = record.product_name.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {record.sheet_name} | {_format_period(record)} | {safe_name} |"
        )
    first = start + 1 if page_records else 0
    last = start + len(page_records)
    lines.extend(["", f"Показаны строки **{first}–{last} из {len(records)}**."])
    return "\n".join(lines)


def _selection_question(question: str) -> str:
    return re.sub(
        r"\s*[,;]?\s*(?:страниц\w*|стр\.)\s*\d{1,4}[.!?]?\s*$",
        "",
        question,
        flags=re.IGNORECASE,
    ).strip()


def _pagination(
    query: ScheduleQuery,
    records: list[ScheduleRecord],
    *,
    snapshot_version: str,
    snapshot_changed: bool,
) -> SchedulePagination | None:
    if query.response_mode != "full" or not records:
        return None
    total_pages = max(ceil(len(records) / FULL_PAGE_SIZE), 1)
    page = min(max(query.page, 1), total_pages)
    return SchedulePagination(
        selection_question=_selection_question(query.raw),
        selection=ScheduleSelection(
            sheet_names=list(query.sheet_names),
            record_kinds=list(query.record_kinds),
            product_kinds=list(query.product_kinds),
            dimensions=list(query.dimensions),
            steel=query.steel,
            date_from=query.date_from,
            date_to=query.date_to,
            date_explicit=query.date_explicit,
            period_mode=query.period_mode,
            include_undated=query.include_undated,
            products_only=query.products_only,
            query_type=query.query_type,
            response_mode="full",
        ),
        snapshot_version=snapshot_version,
        page=page,
        page_size=FULL_PAGE_SIZE,
        total_items=len(records),
        total_pages=total_pages,
        has_previous=page > 1,
        has_next=page < total_pages,
        snapshot_changed=snapshot_changed,
    )


def _covered_dates(records: list[ScheduleRecord]) -> list[date]:
    values: set[date] = set()
    for record in records:
        if record.start_date is None:
            continue
        end = record.end_date or record.start_date
        current = record.start_date
        while current <= end:
            values.add(current)
            current += timedelta(days=1)
    return sorted(values)


def _fallback_answer(
    query: ScheduleQuery,
    records: list[ScheduleRecord],
    *,
    today: date,
    snapshot_records: list[ScheduleRecord] | None = None,
) -> str:
    if query.response_mode == "number_only":
        return str(len(records))
    if query.query_type == "date_bounds":
        dated = [item for item in records if item.start_date is not None]
        if not dated:
            return "В текущем снимке нет производственных позиций с подтверждёнными датами."
        first = min(item.start_date for item in dated if item.start_date is not None)
        last = max((item.end_date or item.start_date) for item in dated)
        return (
            "Подтверждённые даты производственного плана: "
            f"с **{_format_date(first)}** по **{_format_date(last)}**. "
            f"Датированных позиций — **{len(dated)}**; без подтверждённой даты — "
            f"**{len(records) - len(dated)}**."
        )
    if query.query_type == "date_list":
        dates = _covered_dates(records)
        if not dates:
            return "В текущем снимке нет производственных позиций с подтверждёнными датами."
        rendered = ", ".join(_format_date(item) for item in dates)
        return (
            f"В производственном плане присутствует **{len(dates)} уникальных дат**: "
            f"{rendered}."
        )
    if query.query_type == "count":
        breakdown = _record_breakdown(records, today=today)
        snapshot_breakdown = _record_breakdown(
            snapshot_records if snapshot_records is not None else records,
            today=today,
        )
        period = _period_description(query, records)
        if query.period_mode == "all_available":
            return (
                f"В актуальном снимке **{breakdown['total']} производственных "
                f"позиций**: с подтверждённой датой — **{breakdown['dated']}**, "
                f"без подтверждённой даты — **{breakdown['undated']}**. "
                f"Сумма сходится: {breakdown['dated']} + "
                f"{breakdown['undated']} = {breakdown['total']}. Период: {period}."
            )
        excluded_undated = snapshot_breakdown["undated"]
        undated_note = (
            f" Без подтверждённой даты в этот подсчёт не включено: "
            f"**{excluded_undated}**."
            if not query.include_undated
            else f" Из них без подтверждённой даты — **{breakdown['undated']}**."
        )
        return (
            f"Найдено **{breakdown['total']} производственных позиций** "
            f"за период {period}.{undated_note}"
        )
    label = "служебных событий" if query.record_kinds else "производственных позиций"
    if query.response_mode == "full":
        return (
            f"По заданным условиям найдено **{len(records)} {label}** "
            f"за период {_period_description(query, records)}. "
            "Позиции плана приведены в таблице ниже."
        )
    lines = [f"По заданным условиям найдено **{len(records)} {label}**."]
    if query.record_kinds and records and all(item.start_date is None for item in records):
        lines.append(
            "Даты этих событий в источнике не подтверждены, поэтому их нельзя "
            "считать планом на конкретный период."
        )
    lines.append("")
    for record in records[:FALLBACK_FACT_LIMIT]:
        lines.append(
            f"- **{record.sheet_name} — {_format_period(record)}:** "
            f"{record.product_name}."
        )
    if len(records) > FALLBACK_FACT_LIMIT:
        lines.append(f"\nПоказаны {FALLBACK_FACT_LIMIT} примера из {len(records)}.")
    return "\n".join(lines)


def _with_metadata(answer: str, *, sync_time: str, warnings: list[str]) -> str:
    result = answer.rstrip() + f"\n\nПоследняя синхронизация: **{sync_time}**."
    if warnings:
        result += "\n\nОграничения данных:\n" + "\n".join(
            f"- {item}" for item in warnings
        )
    return result


def _answer_is_grounded(answer: str, plan: RollingSchedulePlan) -> bool:
    if not answer.strip() or "http://" in answer or "https://" in answer:
        return False
    if re.search(
        r"\b(?:объ[её]м\w*|тоннаж\w*|тонн\w*|килограмм\w*|кг|"
        r"производительност\w*|единиц\w*\s+измерен\w*)\b",
        answer,
        re.IGNORECASE,
    ):
        return False
    allowed_sizes = {tuple(item.dimensions) for item in plan.records if item.dimensions}
    if plan.query.dimensions:
        allowed_sizes.add(tuple(plan.query.dimensions))
    extracted = extract_dimensions(answer)
    if extracted and tuple(extracted) not in allowed_sizes:
        return False
    allowed_dates = {
        value
        for item in plan.records
        for value in (item.start_date, item.end_date)
        if value is not None
    }
    allowed_dates.update(
        value for value in (plan.query.date_from, plan.query.date_to) if value is not None
    )
    for day, month, year in re.findall(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b", answer):
        try:
            if date(int(year), int(month), int(day)) not in allowed_dates:
                return False
        except ValueError:
            return False
    return True


class RollingScheduleAnswerService:
    def __init__(self, settings: RollingScheduleSettings) -> None:
        self.settings = settings
        self.store = RollingScheduleStore(settings.database_path, settings.status_path)

    @staticmethod
    def intent_request(question: str, *, today: date) -> LLMGenerationRequest:
        return build_intent_request(question, today_iso=today.isoformat())

    @staticmethod
    def resolve_query(
        question: str, *, today: date, analysis_content: str | None
    ) -> ScheduleQuery | None:
        base = parse_schedule_query(question, today=today)
        analysis: ScheduleIntentAnalysis | None = (
            parse_intent_response(analysis_content) if analysis_content else None
        )
        return merge_intent_analysis(base, analysis)

    def prepare(
        self,
        request_id: str,
        question: str,
        *,
        today: date | None = None,
        num_predict: int | None = None,
        query: ScheduleQuery | None = None,
        expected_snapshot_version: str | None = None,
        pagination_only: bool = False,
    ) -> RollingSchedulePlan | None:
        if not self.settings.enabled:
            return None
        current_date = today or datetime.now(MOSCOW).date()
        query = query or parse_schedule_query(question, today=current_date)
        if not query.is_schedule_intent:
            return None
        started = time.perf_counter()
        status = self.store.status()
        metadata = self.store.metadata()
        snapshot_version = metadata.get("snapshot_sha256", "")
        snapshot_changed = bool(
            expected_snapshot_version
            and snapshot_version
            and expected_snapshot_version.lower() != snapshot_version.lower()
        )
        if snapshot_changed:
            query = replace(query, page=1)
        sync_time = _format_sync(
            status.get("last_successful_download_at") or metadata.get("downloaded_at")
        )
        warnings: list[str] = []
        records: list[ScheduleRecord] = []
        supplied: tuple[ScheduleRecord, ...] = ()
        appendix = ""
        llm_request: LLMGenerationRequest | None = None

        if snapshot_changed:
            warnings.append(
                "График обновился после открытия предыдущей страницы; показана "
                "первая страница новой версии."
            )

        if query.needs_clarification:
            answer = query.clarification_question or (
                "Уточните, пожалуйста, какой стан, период или продукция вас интересуют."
            )
        elif not self.store.available:
            answer = "Актуальный снимок графиков прокатов ещё не загружен."
            warnings.append("Нет успешно подготовленного снимка графиков прокатов.")
        else:
            records = self.store.search(query)
            all_products = self.store.search(
                ScheduleQuery(
                    raw="snapshot statistics",
                    is_schedule_intent=True,
                    products_only=True,
                    period_mode="all_available",
                )
            )
            count_scope_records = (
                self.store.search(
                    replace(
                        query,
                        date_from=None,
                        date_to=None,
                        date_explicit=True,
                        period_mode="all_available",
                        include_undated=True,
                    )
                )
                if query.query_type == "count"
                else all_products
            )
            broad_count = query.query_type == "count" and not query.sheet_names
            relevant_sheets = (
                ()
                if broad_count
                else query.sheet_names
                or tuple(dict.fromkeys(item.sheet_name for item in records))
            )
            diagnostics = self.store.sheet_diagnostics(relevant_sheets)
            diagnostic_warning = _diagnostic_warning(
                diagnostics,
                found_records=bool(records),
                details_matter=bool(
                    query.dimensions
                    or query.steel
                    or query.query_type == "product_search"
                ),
            )
            if diagnostic_warning:
                warnings.append(diagnostic_warning)
            if self.store.is_stale(self.settings.stale_after_hours):
                warnings.append(
                    f"Снимок старше {self.settings.stale_after_hours} часов; "
                    "проверьте ежедневное обновление."
                )
            if status.get("last_error"):
                warnings.append(
                    "Последнее обновление завершилось ошибкой; используется предыдущая "
                    "успешно обработанная версия."
                )

            if records:
                supplied = _select_representatives(records)
                dated = [item for item in records if item.start_date]
                first_date = min((item.start_date for item in dated), default=None)
                last_date = max(
                    (item.end_date or item.start_date for item in dated), default=None
                )
                matched_breakdown = _record_breakdown(records, today=current_date)
                payload = {
                    "question": question,
                    "validated_query": {
                        "type": query.query_type,
                        "stands": list(query.sheet_names),
                        "date_from": query.date_from.isoformat() if query.date_from else None,
                        "date_to": query.date_to.isoformat() if query.date_to else None,
                        "dimensions": list(query.dimensions),
                        "steel": query.steel,
                        "response_mode": query.response_mode,
                        "period_mode": query.period_mode,
                    },
                    "matching_records_total": len(records),
                    "matched_record_breakdown": matched_breakdown,
                    "effective_period": _period_description(query, records),
                    "production_records": sum(
                        item.record_kind == "product" for item in records
                    ),
                    "service_records": sum(
                        item.record_kind == "service" for item in records
                    ),
                    "undated_records": sum(item.start_date is None for item in records),
                    "date_span": {
                        "first": first_date.isoformat() if first_date else None,
                        "last": last_date.isoformat() if last_date else None,
                    },
                    "position_counts_by_stand": _top_counts(
                        [item.sheet_name for item in records]
                    ),
                    "position_counts_by_product_kind": _top_counts(
                        [item.product_kind or "unknown" for item in records]
                    ),
                    "service_event_names": (
                        _top_counts([item.product_name for item in records])
                        if query.record_kinds
                        else {}
                    ),
                    "examples": [_fact(item) for item in supplied],
                    "maximum_examples_to_list": LLM_LIST_LIMIT,
                    "full_result_attached": query.response_mode == "full",
                }
                answer = _fallback_answer(
                    query,
                    records,
                    today=current_date,
                    snapshot_records=count_scope_records,
                )
                if (
                    query.query_type not in {"count", "date_bounds", "date_list"}
                    and query.response_mode != "number_only"
                    and not pagination_only
                ):
                    llm_request = LLMGenerationRequest(
                        system_prompt=ROLLING_SYSTEM_PROMPT,
                        user_prompt=(
                            "Ответь на вопрос сотрудника по проверенным данным ниже.\n\n"
                            + json.dumps(payload, ensure_ascii=False, indent=2)
                        ),
                        num_predict=min(num_predict or 700, 900),
                    )
                if query.response_mode == "full":
                    appendix = _full_page(query, records)
            else:
                if query.query_type == "count" or query.response_mode == "number_only":
                    answer = _fallback_answer(
                        query,
                        records,
                        today=current_date,
                        snapshot_records=count_scope_records,
                    )
                elif diagnostic_warning:
                    answer = (
                        "Подходящих подтверждённых записей не найдено, но источник "
                        "заполнен не полностью, поэтому отсутствие плана подтвердить нельзя."
                    )
                else:
                    answer = (
                        "В текущем проверенном графике нет записей, точно соответствующих "
                        "указанным условиям."
                    )

        raw_fallback = answer + (("\n\n" + appendix) if appendix else "")
        decorated_fallback = (
            raw_fallback
            if query.response_mode == "number_only"
            else _with_metadata(raw_fallback, sync_time=sync_time, warnings=warnings)
        )
        elapsed = time.perf_counter() - started
        context_id = "rolling-schedule:" + metadata.get(
            "snapshot_sha256", "unavailable"
        )[:16]
        fallback = AnswerResponse(
            request_id=request_id,
            answer=decorated_fallback,
            status="completed",
            citation_validation="valid",
            citations=[],
            sources=[],
            source_attachments=[],
            related_images=[],
            warnings=warnings,
            latency=APILatency(total_seconds=elapsed),
            telemetry=APITelemetry(
                candidate_chunks=len(records),
                reranked_chunks=len(records),
                context_chunks=len(supplied),
                context_sources=len({item.sheet_name for item in records}),
                context_stats={"rolling_schedule_records": len(records)},
            ),
            model="structured-rolling-schedule-fallback",
            context_id=context_id,
            answer_kind="rolling_schedule",
            schedule_pagination=_pagination(
                query,
                records,
                snapshot_version=snapshot_version,
                snapshot_changed=snapshot_changed,
            ),
        )
        return RollingSchedulePlan(
            fallback=fallback,
            llm_request=llm_request,
            query=query,
            records=tuple(records),
            supplied_records=supplied,
            appendix=appendix,
            started=started,
        )

    def finish(
        self,
        plan: RollingSchedulePlan,
        generated: LLMGenerationResult,
        *,
        llm_queue_wait_seconds: float,
        llm_queue_position: int,
        generation_seconds: float,
    ) -> AnswerResponse:
        generated_answer = generated.content.strip()
        if not _answer_is_grounded(generated_answer, plan):
            return plan.fallback

        answer = generated_answer
        if plan.appendix:
            answer += "\n\n" + plan.appendix
        status = self.store.status()
        metadata = self.store.metadata()
        sync_time = _format_sync(
            status.get("last_successful_download_at") or metadata.get("downloaded_at")
        )
        answer = _with_metadata(
            answer, sync_time=sync_time, warnings=plan.fallback.warnings
        )

        latency = plan.fallback.latency.model_copy(
            update={
                "llm_queue_wait_seconds": llm_queue_wait_seconds,
                "queue_wait_seconds": llm_queue_wait_seconds,
                "generation_seconds": generation_seconds,
                "total_seconds": time.perf_counter() - plan.started,
            }
        )
        prompt_tokens = (
            StableTokenCounter().count(
                plan.llm_request.system_prompt + "\n\n" + plan.llm_request.user_prompt
            )
            if plan.llm_request
            else 0
        )
        telemetry = plan.fallback.telemetry.model_copy(
            update={
                "prompt_estimated_tokens": prompt_tokens,
                "prompt_eval_count": generated.prompt_eval_count,
                "prompt_eval_duration_seconds": generated.prompt_eval_duration_seconds,
                "eval_count": generated.eval_count,
                "eval_duration_seconds": generated.eval_duration_seconds,
                "load_duration_seconds": generated.load_duration_seconds,
                "total_duration_seconds": generated.total_duration_seconds,
                "done_reason": generated.done_reason,
                "llm_queue_position": llm_queue_position,
            }
        )
        return plan.fallback.model_copy(
            update={
                "answer": answer,
                "latency": latency,
                "telemetry": telemetry,
                "model": generated.model,
            }
        )

    def try_answer(
        self,
        request_id: str,
        question: str,
        *,
        today: date | None = None,
    ) -> AnswerResponse | None:
        plan = self.prepare(request_id, question, today=today)
        return plan.fallback if plan is not None else None
