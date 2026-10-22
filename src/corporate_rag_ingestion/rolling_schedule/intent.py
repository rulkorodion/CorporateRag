from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..generation.models import LLMGenerationRequest
from .models import ScheduleQuery
from .search import SHEET_NAME_ALIASES, _normalized_text
from .settings import EXPECTED_SHEETS


QUERY_TYPES = (
    "product_search",
    "production_plan",
    "stand_schedule",
    "count",
    "service_events",
    "summary",
    "date_bounds",
    "date_list",
    "mixed",
    "unrelated",
)


class ScheduleIntentAnalysis(BaseModel):
    """Strict, untrusted output of the LLM question-understanding step."""

    model_config = ConfigDict(extra="forbid")

    is_schedule_question: bool
    query_type: Literal[
        "product_search",
        "production_plan",
        "stand_schedule",
        "count",
        "service_events",
        "summary",
        "date_bounds",
        "date_list",
        "mixed",
        "unrelated",
    ]
    stands: list[str] = Field(default_factory=list, max_length=8)
    product_kind: Literal["round_tube", "profile", "sheet"] | None = None
    dimensions: list[str] = Field(default_factory=list, max_length=4)
    steel: str | None = Field(default=None, max_length=40)
    period_text: str | None = Field(default=None, max_length=120)
    response_mode: Literal["brief", "full"] = "brief"
    needs_clarification: bool = False
    clarification_question: str | None = Field(default=None, max_length=300)


INTENT_SYSTEM_PROMPT = """\
Ты классификатор вопросов к производственным графикам КТЗ. Верни только JSON по
переданной схеме, без пояснений.

Графики содержат планы проката/производства, номенклатуру, даты, станы и площадки,
ремонты, ППР, чистки, перевалки, простои и выходные.

Правила:
1. Не вычисляй даты. Относительный период скопируй дословно в period_text.
2. Не исправляй и не дополняй типоразмер. Размеры возвращай ровно как в вопросе.
3. query_type=count используй для вопросов о количестве позиций/записей. Если
   пользователь спрашивает объём или тоннаж, используй product_search: система
   всё равно показывает только стан, дату и номенклатуру.
4. response_mode — лишь подсказка: окончательно формат выбирает backend. Запрос самого
   плана/графика означает таблицу, а количество, анализ, сравнение, краткая сводка или
   явное «без таблицы» — текстовый ответ.
5. Если вопрос одновременно о технологических возможностях из документов и о
   текущем плане, используй query_type=mixed и попроси уточнить, какой ответ нужен.
6. Вопросы о регламентах, командировках, согласованиях и документах — unrelated.
7. Если стан, период или продукция действительно необходимы, но не указаны и без
   них нельзя дать осмысленный ответ, задай один короткий уточняющий вопрос.
8. «График прокатов», «график/план производства» и график по названному стану —
   однозначно вопросы о текущих производственных графиках, а не о технологических
   возможностях. Не помечай их mixed и не проси уточнение.
9. Вопрос о границах дат графика — date_bounds, вопрос о перечне дат — date_list.
"""


def build_intent_request(question: str, *, today_iso: str) -> LLMGenerationRequest:
    schema = ScheduleIntentAnalysis.model_json_schema()
    return LLMGenerationRequest(
        system_prompt=INTENT_SYSTEM_PROMPT,
        user_prompt=(
            f"Текущая дата в Europe/Moscow: {today_iso}.\n"
            f"Вопрос: {question}\n"
            "Извлеки намерение и параметры. Даты не рассчитывай."
        ),
        num_predict=320,
        response_format=schema,
    )


def parse_intent_response(content: str) -> ScheduleIntentAnalysis | None:
    value = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, re.DOTALL)
    if fenced:
        value = fenced.group(1)
    try:
        return ScheduleIntentAnalysis.model_validate(json.loads(value))
    except (json.JSONDecodeError, ValidationError, TypeError):
        return None


def _validated_stands(values: list[str], *, question: str) -> tuple[str, ...]:
    known = {_normalized_text(item.replace("_", " ")): item for item in EXPECTED_SHEETS}
    normalized_question = _normalized_text(question.replace("_", " "))
    selected: list[str] = []
    for value in values:
        normalized = _normalized_text(value.replace("_", " "))
        sheet = known.get(normalized) or SHEET_NAME_ALIASES.get(normalized)
        if sheet and normalized in normalized_question and sheet not in selected:
            selected.append(sheet)
    return tuple(selected)


def merge_intent_analysis(
    base: ScheduleQuery,
    analysis: ScheduleIntentAnalysis | None,
) -> ScheduleQuery | None:
    """Merge only validated semantic hints; deterministic facts always win."""

    if analysis is None:
        return base if base.is_schedule_intent else None
    genuine_mixed = bool(
        re.search(
            r"\b(?:что|какие)\s+(?:мы\s+)?можем\s+производить\b|"
            r"\bтехнологическ\w*\s+возможност\w*\b",
            _normalized_text(base.raw),
        )
    )
    # A deterministic schedule match is stronger than an LLM suggestion. This
    # prevents clear requests such as "График производства по стану Ситко" from
    # being turned into a capabilities-vs-plan clarification.
    if analysis.query_type == "mixed" and (not base.is_schedule_intent or genuine_mixed):
        return replace(
            base,
            is_schedule_intent=True,
            query_type="mixed",
            needs_clarification=True,
            clarification_question=(
                analysis.clarification_question
                or "Уточните, пожалуйста: вас интересуют технологические возможности "
                "из документов или фактический производственный план на период?"
            ),
        )
    if not analysis.is_schedule_question and not base.is_schedule_intent:
        return None

    sheet_names = base.sheet_names or _validated_stands(
        analysis.stands, question=base.raw
    )
    product_kinds = base.product_kinds
    if not product_kinds and analysis.product_kind:
        product_kinds = (analysis.product_kind,)

    query_type = base.query_type
    deterministic_type = base.query_type in {
        "count",
        "service_events",
        "product_search",
        "stand_schedule",
        "date_bounds",
        "date_list",
    }
    if (
        not deterministic_type
        and analysis.query_type in QUERY_TYPES
        and analysis.query_type not in {"unrelated", "mixed"}
    ):
        query_type = analysis.query_type
    service_only = query_type == "service_events"
    products_only = False if service_only else base.products_only or query_type in {
        "product_search",
        "production_plan",
        "stand_schedule",
        "count",
        "date_bounds",
        "date_list",
    }

    return replace(
        base,
        is_schedule_intent=True,
        sheet_names=sheet_names,
        record_kinds=("service",) if service_only else base.record_kinds,
        product_kinds=product_kinds,
        steel=(
            base.steel
            or (
                analysis.steel
                if analysis.steel
                and _normalized_text(analysis.steel) in _normalized_text(base.raw)
                else None
            )
        ),
        products_only=products_only,
        query_type=query_type,
        # Output shape is security- and UX-sensitive. Only the deterministic
        # parser may enable a full table or an exact numeric-only response.
        response_mode=base.response_mode,
        needs_clarification=(analysis.needs_clarification if not base.is_schedule_intent else False),
        clarification_question=(
            analysis.clarification_question if not base.is_schedule_intent else None
        ),
    )
