from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, timedelta

from .models import ScheduleQuery
from .normalizer import RUSSIAN_MONTHS, extract_dimensions
from .settings import EXPECTED_SHEETS


SCHEDULE_INTENT_RE = re.compile(
    r"(график\w*\s+(?:прокат\w*|производств\w*)|"
    r"план\w*\s+(?:прокат\w*|производств\w*)|"
    r"(?:планир\w*|запланир\w*|прокат\w*)\s+(?:труб\w*|профил\w*|лист\w*|продукц\w*)|"
    r"(?:когда|где|на каком стане|какие|что)\s+.{0,40}(?:планир\w*|прокат\w*|стан\w*)|"
    r"(?:что|какие позиции)\s+.{0,20}(?:на\s+)?стан\w*|"
    r"(?:ремонт\w*|прост(?:ой|ои|оев|оям|оях)\w*|ппр|переналад\w*|выходн\w*)"
    r"\s+.{0,50}стан\w*|"
    r"есть ли\s+.{0,40}прокат\w*|"
    r"(?:объ[её]м|сколько)\s+.{0,40}запланир\w*)",
    re.IGNORECASE,
)

PRODUCTION_DOMAIN_RE = re.compile(
    r"\b(?:график\w*|план\w*|расписан\w*|прокат\w*|производ\w*|"
    r"выпуск\w*|изготавл\w*|изготов\w*|катат\w*|катают\w*|"
    r"делать|делают|загрузк\w*|очеред\w*|намеч\w*|запланир\w*)\b",
    re.IGNORECASE,
)
SCHEDULE_SCOPE_RE = re.compile(
    r"\b(?:стан\w*|труб\w*|профил\w*|лист\w*|типоразмер\w*|"
    r"сегодня|завтра|послезавтра|недел\w*|месяц\w*|дат\w*|"
    r"январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]|июн\w*|июл\w*|"
    r"август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)\b|"
    r"\d{1,5}(?:[.,]\d+)?\s*[xх×]\s*\d{1,5}",
    re.IGNORECASE,
)
DOCUMENT_PROCEDURE_RE = re.compile(
    r"\b(?:регламент\w*|инструкц\w*|порядок\w*|правил\w*|технологи\w*|"
    r"как\s+(?:оформить|согласовать|подать|заказать|работать|выполнять))\b",
    re.IGNORECASE,
)

SERVICE_QUERY_RE = re.compile(
    r"\b(?:ремонт\w*|прост(?:ой|ои|оев|оям|оях)\w*|ппр|переналад\w*|"
    r"выходн\w*|профилактик\w*|останов\w*|чистк\w*|перевалк\w*|"
    r"обслуживан\w*|служебн\w*\s+(?:работ|событ|операц)\w*|"
    r"техническ\w*\s+работ\w*)\b",
    re.IGNORECASE,
)

COUNT_QUERY_RE = re.compile(
    r"\b(?:сколько|количество)\b.{0,45}"
    r"\b(?:позици\w*|производственн\w*\s+запис\w*|строк\w*)\b",
    re.IGNORECASE,
)

TEXT_SUMMARY_QUERY_RE = re.compile(
    r"\b(?:анализ\w*|сравн\w*|сопостав\w*|сводк\w*|итог\w*|"
    r"кратк\w*|опиши\w*|описан\w*|охарактериз\w*)\b",
    re.IGNORECASE,
)

WITHOUT_TABLE_RE = re.compile(r"\bбез\s+таблиц\w*\b", re.IGNORECASE)

DATE_BOUNDS_QUERY_RE = re.compile(
    r"(?:с\s+какой\s+по\s+какую\s+дат\w*|"
    r"границ\w*\s+дат\w*|"
    r"(?:минимальн\w*|сам\w*\s+ранн\w*).{0,40}"
    r"(?:максимальн\w*|сам\w*\s+поздн\w*)\s+дат\w*)",
    re.IGNORECASE,
)

DATE_LIST_QUERY_RE = re.compile(
    r"(?:какие\s+дат\w*\s+(?:есть|присутств\w*|указан\w*)|"
    r"(?:полн\w*\s+)?перечень\s+дат\w*|список\s+дат\w*)",
    re.IGNORECASE,
)

NUMBER_WORDS = {
    "один": 1,
    "одного": 1,
    "два": 2,
    "две": 2,
    "двух": 2,
    "три": 3,
    "трех": 3,
    "четыре": 4,
    "четырех": 4,
    "пять": 5,
    "пяти": 5,
    "шесть": 6,
    "шести": 6,
    "семь": 7,
    "семи": 7,
    "восемь": 8,
    "восьми": 8,
    "девять": 9,
    "девяти": 9,
    "десять": 10,
    "десяти": 10,
}

ALL_AVAILABLE_PERIOD_RE = re.compile(
    r"(?:за\s+весь\s+(?:доступн\w*\s+)?период|"
    r"во\s+всех\s+графиках|"
    r"включая\s+прошедш\w*\s+дат\w*|"
    r"за\s+вс[её]\s+время|"
    r"весь\s+доступн\w*\s+график)",
    re.IGNORECASE,
)

STAND_NUMBER_ALIASES = {
    "11": "Ситко",
    "14": "Элмаксан",
    "31": "Стан_ХК НСК",
    "35": "Китаец",
    "39": "Белорус",
    "42": "Стан_ГК НСК",
    "51": "51",
    "76": "76",
    "89": "89",
    "114": "114",
    "159": "159",
    "250": "250",
}

SHEET_NAME_ALIASES = {
    "кореец": "Кореец",
    "корейц": "Кореец",
    "королев": "Королев",
    "ситко": "Ситко",
    "элмаксан": "Элмаксан",
    "адафер": "Адафер",
    "китаец": "Китаец",
    "китайц": "Китаец",
    "белорус": "Белорус",
    "малтибест": "Малтибест",
    "челябинск": "Челябинск",
    "череп": "El_Череп",
    "ижевск хк 3": "Ижевск ХК 3",
    "ижевск хк 2": "Ижевск ХК 2",
    "ижевск хк": "Ижевск ХК",
    "стан хк нск": "Стан_ХК НСК",
    "хк нск": "Стан_ХК НСК",
    "стан гк нск": "Стан_ГК НСК",
    "гк нск": "Стан_ГК НСК",
    "гк влг": "ГК ВЛГ",
    "хк влг": "ХК ВЛГ",
    "лист дмц гк": "Лист ДМЦ ГК",
    "дмц гк": "Лист ДМЦ ГК",
    "лист дмц хк": "Лист ДМЦ ХК",
    "дмц хк": "Лист ДМЦ ХК",
}


def _normalized_text(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def is_schedule_intent(question: str) -> bool:
    text = _normalized_text(question)
    if DOCUMENT_PROCEDURE_RE.search(text):
        return False
    if COUNT_QUERY_RE.search(text):
        return True
    if SCHEDULE_INTENT_RE.search(text):
        return True
    has_domain = bool(PRODUCTION_DOMAIN_RE.search(text))
    has_scope = bool(SCHEDULE_SCOPE_RE.search(text))
    known_named_sheet = any(
        _normalized_text(sheet.replace("_", " ")) in text
        for sheet in EXPECTED_SHEETS
        if not sheet.isdigit() and len(sheet) >= 3
    ) or any(alias in text for alias in SHEET_NAME_ALIASES)
    known_numeric_stand = bool(
        re.search(r"\b(?:11|14|31|35|39|42|51|76|89|114|159|250)\b", text)
    )
    # Broad natural-language routing is deliberate, but requires either an
    # explicit production-plan phrase or a concrete schedule scope. This keeps
    # procedural documents about production in the ordinary RAG pipeline.
    return has_domain and (has_scope or known_named_sheet or known_numeric_stand)


def is_schedule_candidate(question: str) -> bool:
    """Conservative pre-filter before the LLM intent classifier.

    It is intentionally broader than ``is_schedule_intent`` but still keeps
    procedural questions in the ordinary document RAG pipeline.
    """

    text = _normalized_text(question)
    if DOCUMENT_PROCEDURE_RE.search(text):
        return False
    return bool(
        is_schedule_intent(question)
        or SERVICE_QUERY_RE.search(text)
        or (
            PRODUCTION_DOMAIN_RE.search(text)
            and (
                SCHEDULE_SCOPE_RE.search(text)
                or re.search(r"\b(?:что|какие|где|когда|сколько|объ[её]м)\b", text)
            )
        )
    )


def _sheet_filters(text: str) -> tuple[str, ...]:
    normalized = _normalized_text(text)
    selected: list[str] = []
    for sheet in sorted(EXPECTED_SHEETS, key=len, reverse=True):
        token = _normalized_text(sheet.replace("_", " "))
        if not token.isdigit() and len(token) >= 3 and token in normalized:
            selected.append(sheet)
    for alias, sheet in sorted(
        SHEET_NAME_ALIASES.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if alias in normalized:
            selected.append(sheet)
    stand_matches = re.findall(
        r"\bстан(?:е|а|у|ом|ы)?\s*[№#_-]?\s*(\d{1,3})\b", normalized
    )
    stand_matches.extend(
        re.findall(r"\b(\d{1,3})\s*(?:-?й\s+)?стан(?:е|а|у|ом|ы)?\b", normalized)
    )
    if PRODUCTION_DOMAIN_RE.search(normalized):
        stand_matches.extend(
            number
            for number in re.findall(r"\b\d{1,3}\b", normalized)
            if number in STAND_NUMBER_ALIASES
        )
    for number in stand_matches:
        sheet = STAND_NUMBER_ALIASES.get(number)
        if sheet:
            selected.append(sheet)
    for token, sheet in (("127тм", "127ТМ"), ("76тм", "76ТМ")):
        if re.search(rf"\bстан(?:е|а|у|ом)?\s*{token}\b", normalized):
            selected.append(sheet)
    return tuple(dict.fromkeys(selected))


def _date_filters(text: str, today: date) -> tuple[date | None, date | None]:
    normalized = _normalized_text(text)
    if "послезавтра" in normalized:
        target = today + timedelta(days=2)
        return target, target
    if "завтра" in normalized:
        target = today + timedelta(days=1)
        return target, target
    if "сегодня" in normalized:
        return today, today
    if re.search(r"\b(?:на\s+)?следующ\w*\s+недел", normalized):
        next_monday = today + timedelta(days=(7 - today.weekday()))
        return next_monday, next_monday + timedelta(days=6)
    if re.search(r"\b(?:на\s+)?(?:этой|текущ\w*)\s+недел", normalized):
        return today, today + timedelta(days=(6 - today.weekday()))
    if re.search(r"\b(?:в\s+)?ближайш\w*\s+недел", normalized):
        return today, today + timedelta(days=7)
    number_words = "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
    nearest = re.search(
        rf"(?:ближайш\w*|следующ\w*)\s+"
        rf"(?:(\d{{1,2}}|{number_words})\s+)?дн",
        normalized,
    )
    if nearest:
        token = nearest.group(1)
        days = int(token) if token and token.isdigit() else NUMBER_WORDS.get(token or "", 7)
        days = min(max(days, 1), 31)
        return today, today + timedelta(days=days - 1)
    range_match = re.search(
        r"\bс\s+(\d{1,2})[./-](\d{1,2})(?:[./-](\d{4}))?\s+"
        r"(?:по|до)\s+(\d{1,2})[./-](\d{1,2})(?:[./-](\d{4}))?\b",
        normalized,
    )
    if range_match:
        try:
            start = date(
                int(range_match.group(3) or today.year),
                int(range_match.group(2)),
                int(range_match.group(1)),
            )
            end = date(
                int(range_match.group(6) or range_match.group(3) or today.year),
                int(range_match.group(5)),
                int(range_match.group(4)),
            )
            if start <= end:
                return start, end
        except ValueError:
            pass
    month_pattern = "|".join(sorted(RUSSIAN_MONTHS, key=len, reverse=True))
    named_range = re.search(
        rf"\bс\s+(\d{{1,2}})\s+({month_pattern})[а-яё]*\s+"
        rf"(?:по|до)\s+(\d{{1,2}})\s+({month_pattern})[а-яё]*\s*(\d{{4}})?\b",
        normalized,
    )
    if named_range:
        try:
            year = int(named_range.group(5) or today.year)
            start = date(
                year,
                RUSSIAN_MONTHS[named_range.group(2)[:3]],
                int(named_range.group(1)),
            )
            end_year = year
            end_month = RUSSIAN_MONTHS[named_range.group(4)[:3]]
            if end_month < start.month and named_range.group(5) is None:
                end_year += 1
            end = date(end_year, end_month, int(named_range.group(3)))
            if start <= end:
                return start, end
        except (KeyError, ValueError):
            pass
    same_month_range = re.search(
        rf"\bс\s+(\d{{1,2}})\s+(?:по|до)\s+(\d{{1,2}})\s+"
        rf"({month_pattern})[а-яё]*\s*(\d{{4}})?\b",
        normalized,
    )
    if same_month_range:
        try:
            year = int(same_month_range.group(4) or today.year)
            month = RUSSIAN_MONTHS[same_month_range.group(3)[:3]]
            start = date(year, month, int(same_month_range.group(1)))
            end = date(year, month, int(same_month_range.group(2)))
            if start <= end:
                return start, end
        except (KeyError, ValueError):
            pass
    full = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", normalized)
    if full:
        try:
            target = date(int(full.group(3)), int(full.group(2)), int(full.group(1)))
            return target, target
        except ValueError:
            pass
    short = re.search(r"\b(\d{1,2})[./-](\d{1,2})(?![./-]\d)\b", normalized)
    if short:
        try:
            target = date(today.year, int(short.group(2)), int(short.group(1)))
            return target, target
        except ValueError:
            pass
    named = re.search(
        rf"\b(\d{{1,2}})\s+({month_pattern})[а-яё]*\s*(\d{{4}})?\b", normalized
    )
    if named:
        try:
            target = date(
                int(named.group(3) or today.year),
                RUSSIAN_MONTHS[named.group(2)[:3]],
                int(named.group(1)),
            )
            return target, target
        except (KeyError, ValueError):
            pass
    month_only = re.search(
        rf"\b(?:в|на|за)\s+({month_pattern})[а-яё]*\s*(\d{{4}})?\b",
        normalized,
    )
    if month_only:
        try:
            month = RUSSIAN_MONTHS[month_only.group(1)[:3]]
            year = int(month_only.group(2) or today.year)
            return date(year, month, 1), date(year, month, monthrange(year, month)[1])
        except (KeyError, ValueError):
            pass
    return None, None


def _parse_open_date_token(value: str, *, today: date) -> date | None:
    normalized = _normalized_text(value)
    numeric = re.fullmatch(
        r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{4}))?", normalized
    )
    if numeric:
        try:
            return date(
                int(numeric.group(3) or today.year),
                int(numeric.group(2)),
                int(numeric.group(1)),
            )
        except ValueError:
            return None
    month_pattern = "|".join(sorted(RUSSIAN_MONTHS, key=len, reverse=True))
    named = re.fullmatch(
        rf"(\d{{1,2}})\s+({month_pattern})[\u0430-\u044fё]*"
        rf"(?:\s+(\d{{4}}))?(?:\s+года?)?",
        normalized,
    )
    if not named:
        return None
    try:
        return date(
            int(named.group(3) or today.year),
            RUSSIAN_MONTHS[named.group(2)[:3]],
            int(named.group(1)),
        )
    except (KeyError, ValueError):
        return None


def _open_period_filters(
    text: str, *, today: date
) -> tuple[date | None, date | None, str] | None:
    """Parse periods with one data-defined boundary or the complete snapshot."""

    normalized = _normalized_text(text)
    if ALL_AVAILABLE_PERIOD_RE.search(normalized):
        return None, None, "all_available"
    month_pattern = "|".join(sorted(RUSSIAN_MONTHS, key=len, reverse=True))
    token = (
        rf"(?:\d{{1,2}}[./-]\d{{1,2}}(?:[./-]\d{{4}})?|"
        rf"\d{{1,2}}\s+(?:{month_pattern})[\u0430-\u044fё]*"
        rf"(?:\s+\d{{4}})?(?:\s+года?)?)"
    )
    to_end = re.search(
        rf"\b(?:с|начиная\s+с)\s+({token})\s+"
        rf"до\s+конца\s+(?:доступн\w*\s+)?графика",
        normalized,
    )
    if to_end:
        start = _parse_open_date_token(to_end.group(1), today=today)
        if start:
            return start, None, "open_from"
    after = re.search(rf"\bпосле\s+({token})", normalized)
    if after:
        start = _parse_open_date_token(after.group(1), today=today)
        if start:
            return start + timedelta(days=1), None, "open_after"
    starting = re.search(rf"\bначиная\s+с\s+({token})", normalized)
    if starting:
        start = _parse_open_date_token(starting.group(1), today=today)
        if start:
            return start, None, "open_from"
    if re.search(r"до\s+конца\s+(?:доступн\w*\s+)?графика", normalized):
        return today, None, "open_from"
    return None


def parse_schedule_query(question: str, *, today: date) -> ScheduleQuery:
    normalized = _normalized_text(question)
    intent = is_schedule_intent(question)
    date_bounds_query = bool(DATE_BOUNDS_QUERY_RE.search(normalized))
    date_list_query = bool(DATE_LIST_QUERY_RE.search(normalized))
    dimensions = extract_dimensions(normalized)
    sheet_names = _sheet_filters(question)
    service_only = bool(SERVICE_QUERY_RE.search(normalized))
    kinds: tuple[str, ...] = ()
    if "лист" in normalized:
        kinds = ("sheet",)
    elif "проф" in normalized:
        kinds = ("profile",)
    elif "труб" in normalized:
        kinds = ("round_tube", "profile") if len(dimensions) != 2 else ("round_tube",)
    steel_match = re.search(
        r"\b(09г2с|08пс|08кп|ст\s*\d{1,2}(?:пс|сп|кп)?)\b", normalized
    )
    steel = None
    if steel_match:
        steel = re.sub(r"\s+", "", steel_match.group(1)).upper().replace("СТ", "Ст")
    open_period = _open_period_filters(normalized, today=today)
    if open_period:
        date_from, date_to, period_mode = open_period
    else:
        date_from, date_to = _date_filters(normalized, today)
        period_mode = "explicit" if date_from is not None or date_to is not None else "default_future"
        # A single leading "до <даты>" is an open lower boundary, not
        # a one-day request. Closed "с ... до ..." ranges were already parsed above.
        if date_from and date_to and date_from == date_to:
            month_pattern = "|".join(sorted(RUSSIAN_MONTHS, key=len, reverse=True))
            date_token = (
                rf"(?:\d{{1,2}}[./-]\d{{1,2}}(?:[./-]\d{{4}})?|"
                rf"\d{{1,2}}\s+(?:{month_pattern})[\u0430-\u044fё]*"
                rf"(?:\s+\d{{4}})?(?:\s+года?)?)"
            )
            closed_marker = re.search(
                r"\bс\s+\d.{0,50}\b(?:по|до)\s+\d", normalized
            )
            if not closed_marker and re.search(
                rf"\b(?:по|до)\s+{date_token}", normalized
            ):
                date_from = None
                period_mode = "open_until"
            elif re.search(rf"\bс\s+{date_token}(?:\s|$)", normalized):
                date_to = None
                period_mode = "open_from"
    if date_bounds_query or date_list_query:
        date_from = None
        date_to = None
        period_mode = "all_available"
    has_explicit_date = period_mode != "default_future"
    # This dataset is a production plan. Without an explicit date expression,
    # return current/upcoming positions rather than already completed history.
    if intent and date_from is None and date_to is None:
        if period_mode == "default_future":
            date_from = today
    # A number of source rows contain trustworthy nomenclature but no
    # trustworthy date because helper formulas are missing. Include them when
    # a stand or exact nomenclature is requested; they remain visibly undated.
    include_undated = bool(
        intent
        and not has_explicit_date
        and (
            service_only
            or bool(sheet_names)
            or bool(dimensions)
        )
    )
    production_focus = bool(
        re.search(
            r"\b(?:прокат\w*|производ\w*|план\w*|планир\w*|запланир\w*|"
            r"выпуск\w*|изготавл\w*|"
            r"изготов\w*|катат\w*|катают\w*|труб\w*|профил\w*|"
            r"лист\w*|продукц\w*|позиц\w*)\b",
            normalized,
        )
    )
    count_query = bool(COUNT_QUERY_RE.search(normalized))
    if date_bounds_query:
        query_type = "date_bounds"
    elif date_list_query:
        query_type = "date_list"
    elif service_only:
        query_type = "service_events"
    elif count_query:
        query_type = "count"
    elif re.search(r"\bобъ[её]м\b", normalized):
        # Production quantities are intentionally not exposed. Keep the
        # nomenclature filters so the answer can still show stand and date.
        query_type = "product_search"
    elif dimensions:
        query_type = "product_search"
    elif sheet_names:
        query_type = "stand_schedule"
    elif re.search(r"\b(?:сводк\w*|итог\w*|общ\w*)\b", normalized):
        query_type = "summary"
    else:
        query_type = "production_plan"
    products_only = not service_only and bool(
        dimensions
        or production_focus
        or query_type
        in {
            "product_search",
            "production_plan",
            "stand_schedule",
            "count",
            "date_bounds",
            "date_list",
        }
    )
    page_match = re.search(r"\b(?:страниц\w*|стр\.)\s*(\d{1,3})\b", normalized)
    number_only = bool(re.search(r"\bтолько\s+(?:цифр\w*|число)\b", normalized))
    text_only = bool(
        WITHOUT_TABLE_RE.search(normalized) or TEXT_SUMMARY_QUERY_RE.search(normalized)
    )
    explicit_full = bool(
        page_match
        or re.search(
            r"\b(?:покажи|выведи|перечисли|дай)\b.{0,35}"
            r"\b(?:весь|всю|все|полн\w*|целиком)\b.{0,35}"
            r"\b(?:график\w*|план\w*|позици\w*|список\w*)\b",
            normalized,
        )
        or re.search(
            r"\b(?:весь|полн\w*)\s+(?:график\w*|список\w*)\b",
            normalized,
        )
    )
    # A request for a production plan or rolling schedule means that the user
    # wants to see the actual records.  The table decision is deterministic:
    # the LLM may introduce it, but cannot replace it with a handful of examples.
    table_by_intent = query_type in {"production_plan", "stand_schedule"}
    response_mode = (
        "number_only"
        if number_only
        else "brief"
        if count_query
        or text_only
        or query_type
        in {"service_events", "summary", "date_bounds", "date_list"}
        else "full"
        if explicit_full or table_by_intent
        else "brief"
    )
    return ScheduleQuery(
        raw=question,
        is_schedule_intent=intent,
        sheet_names=sheet_names,
        record_kinds=("service",) if service_only else (),
        product_kinds=kinds,
        dimensions=dimensions,
        steel=steel,
        date_from=date_from,
        date_to=date_to,
        date_explicit=has_explicit_date,
        period_mode=period_mode,
        include_undated=include_undated,
        products_only=products_only,
        query_type=query_type,
        response_mode=response_mode,
        page=max(int(page_match.group(1)), 1) if page_match else 1,
    )
