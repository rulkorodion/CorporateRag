from __future__ import annotations

import re
from collections import Counter

from .models import TableRepresentation


TABLE_LINE_RE = re.compile(r"^Table:\s*(.+?)\s*$", re.MULTILINE)
HEADERS_LINE_RE = re.compile(r"^Headers:\s*(.+?)\s*$", re.MULTILINE)
GENERIC_COLUMN_RE = re.compile(r"^(?:column|col|колонка)[_\s-]*(\d+)$", re.IGNORECASE)


class TableContextRenderer:
    """Render table rows as explicit field/value records for smaller LLMs."""

    empty_value = "НЕ УКАЗАНО (ячейка пуста; не брать значение из соседнего поля)"

    def render(
        self,
        table: TableRepresentation,
        source_text: str,
        *,
        citation: str | None = None,
    ) -> str:
        title = self._table_title(source_text)
        headers, rows = self._effective_structure(table, source_text)
        width = max([len(headers), *(len(row) for row in rows)], default=0)
        labels = self._labels(headers, width)
        parts = [f"Таблица: {title}", "Поля таблицы: " + " | ".join(labels)]
        if not rows:
            parts.append("Строки: не указано")
            return "\n".join(parts)
        try:
            start = int(table.row_start) if table.row_start is not None else 1
        except (TypeError, ValueError):
            start = 1
        for offset, row in enumerate(rows):
            row_label = f"Строка {start + offset}"
            if citation:
                # Small models occasionally mistake an Excel row number for a
                # source number. Repeat the actual source citation per record.
                row_label += f" (цитировать как источник {citation})"
            parts.append(row_label + ":")
            for index, label in enumerate(labels):
                value = str(row[index]).strip() if index < len(row) else ""
                parts.append(f"{label}: {value or self.empty_value}")
        if citation:
            parts.append(
                f"Конец таблицы. Все строки выше относятся только к источнику {citation}; "
                f"для фактов из этой таблицы допустима только ссылка {citation}."
            )
        return "\n".join(parts)

    def _effective_structure(
        self, table: TableRepresentation, source_text: str
    ) -> tuple[list[str], list[list[str]]]:
        headers = [str(value).strip() for value in (table.render_headers or table.headers)]
        rows = [[str(value).strip() for value in row] for row in table.rows]
        parsed = self._headers_from_text(source_text)
        if not table.render_headers and self._informative(parsed) and len(parsed) >= max((len(row) for row in rows), default=0):
            headers = parsed
        if not self._informative(headers) and rows and self._looks_like_header_row(rows[0]):
            headers = list(rows[0])
            rows = rows[1:]
        return headers, rows

    def resolve_headers(
        self,
        headers: list[str],
        rows: list[list[str]],
        source_text: str,
        candidate_rows: list[list[str]],
    ) -> tuple[list[str], str]:
        width = max([len(headers), *(len(row) for row in rows)], default=0)
        parsed = self._headers_from_text(source_text)
        if self._informative(parsed) and len(parsed) >= width:
            return parsed[:width], "chunk_text_headers"
        if self._informative(headers) and len(headers) >= width:
            return list(headers[:width]), "table_metadata_headers"
        ranked = [
            row for row in candidate_rows
            if len(row) >= width and self._looks_like_header_row(row[:width])
        ]
        if ranked:
            ranked.sort(key=lambda row: (-self._header_score(row[:width]), tuple(row[:width])))
            return list(ranked[0][:width]), "related_table_header_row"
        if headers:
            return list(headers[:width]), "table_metadata_headers_unresolved"
        return [f"Колонка {index}" for index in range(1, width + 1)], "generated_column_labels"

    @staticmethod
    def _table_title(source_text: str) -> str:
        match = TABLE_LINE_RE.search(source_text)
        if match:
            return match.group(1).strip()
        first = next((line.strip() for line in source_text.splitlines() if line.strip()), "")
        return first or "без названия"

    @staticmethod
    def _headers_from_text(source_text: str) -> list[str]:
        match = HEADERS_LINE_RE.search(source_text)
        if not match:
            return []
        return [part.strip() for part in match.group(1).split("|")]

    @staticmethod
    def _informative(headers: list[str]) -> bool:
        values = [value.casefold().strip() for value in headers if value.strip()]
        if not values:
            return False
        unique_ratio = len(set(values)) / len(values)
        generic = sum(bool(GENERIC_COLUMN_RE.match(value)) for value in values)
        return unique_ratio >= 0.5 and generic < len(values)

    @staticmethod
    def _looks_like_header_row(row: list[str]) -> bool:
        keywords = (
            "ответствен",
            "получател",
            "канал",
            "способ связи",
            "к кому",
            "действие",
            "значение",
            "параметр",
            "наименование",
            "срок",
            "сумм",
            "цена",
        )
        normalized = [value.casefold().strip() for value in row if value.strip()]
        return len(normalized) >= 2 and sum(
            any(keyword in value for keyword in keywords) for value in normalized
        ) >= 2

    @staticmethod
    def _header_score(row: list[str]) -> int:
        keywords = (
            "ответствен",
            "получател",
            "канал",
            "способ связи",
            "к кому",
            "действие",
            "значение",
            "параметр",
            "наименование",
            "срок",
            "сумм",
            "цена",
            "тема обращения",
        )
        return sum(
            any(keyword in value.casefold() for keyword in keywords)
            for value in row if value.strip()
        )

    @staticmethod
    def _labels(headers: list[str], width: int) -> list[str]:
        base: list[str] = []
        for index in range(width):
            value = headers[index].strip() if index < len(headers) else ""
            generic = GENERIC_COLUMN_RE.match(value)
            if not value or generic:
                number = generic.group(1) if generic else str(index + 1)
                value = f"Колонка {number}"
            base.append(value)
        counts = Counter(value.casefold() for value in base)
        seen: Counter[str] = Counter()
        labels: list[str] = []
        for index, value in enumerate(base, start=1):
            key = value.casefold()
            seen[key] += 1
            if counts[key] > 1:
                labels.append(f"{value} (колонка {index})")
            else:
                labels.append(value)
        return labels
