from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from ..schemas import NormalizedElement
from .settings import ChunkingSettings
from .tokens import TokenEstimator


MARKDOWN_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")
TABLE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_cell(value: Any) -> str:
    return " ".join(TABLE_CONTROL_RE.sub("", str(value)).split()).strip()


class _TableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self.header_rows: set[int] = set()
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._cell_is_header = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []
            self._cell_is_header = tag == "th"

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            value = _clean_cell("".join(self._cell))
            self._row.append(value)
            if self._cell_is_header:
                self.header_rows.add(len(self.rows))
            self._cell = None
            self._cell_is_header = False
        elif tag == "tr" and self._row is not None:
            if any(cell for cell in self._row):
                self.rows.append(self._row)
            self._row = None


@dataclass(slots=True)
class ParsedTable:
    title: str
    headers: list[str]
    rows: list[list[str]]
    row_labels: list[str]
    source_format: str
    original_html: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TablePart:
    text: str
    html: str
    headers: list[str]
    rows: list[list[str]]
    row_start: str | None
    row_end: str | None
    source_format: str
    original_html: str
    hard_max_exception: str | None = None


class TableChunker:
    def __init__(self, settings: ChunkingSettings, estimator: TokenEstimator) -> None:
        self.settings = settings
        self.estimator = estimator

    def parse(self, element: NormalizedElement) -> ParsedTable | None:
        metadata = element.metadata
        title = _clean_cell(metadata.get("table_title") or metadata.get("sheet_name") or "")
        html_value = str(metadata.get("html") or "").strip()
        if not html_value and (element.text or "").lstrip().casefold().startswith("<table"):
            html_value = str(element.text).strip()
        if html_value:
            parsed = _TableHTMLParser()
            parsed.feed(html_value)
            if parsed.rows:
                header_count = max(1, self.settings.table_header_rows)
                if parsed.header_rows:
                    header_count = max(parsed.header_rows) + 1
                header_rows = parsed.rows[:header_count]
                headers = self._combine_header_rows(header_rows)
                data_rows = parsed.rows[header_count:]
                first_data_label = header_count + 1
                if not self._headers_are_reasonable(headers):
                    width = max((len(row) for row in parsed.rows), default=0)
                    headers = [f"column_{index + 1}" for index in range(width)]
                    data_rows = parsed.rows
                    first_data_label = 1
                return ParsedTable(
                    title=title,
                    headers=headers,
                    rows=data_rows,
                    row_labels=[str(index) for index in range(first_data_label, len(parsed.rows) + 1)],
                    source_format="html",
                    original_html=html_value,
                )
        cells = metadata.get("cells")
        if isinstance(cells, list) and cells:
            return self._from_excel_cells(cells, title, metadata)
        text = str(element.text or "").strip()
        if text:
            markdown = self._from_markdown(text, title)
            if markdown is not None:
                return markdown
            lines = [_clean_cell(line) for line in text.splitlines() if _clean_cell(line)]
            if lines:
                return ParsedTable(
                    title=title,
                    headers=["value"],
                    rows=[[line] for line in lines],
                    row_labels=[str(index) for index in range(1, len(lines) + 1)],
                    source_format="plain_text_table",
                )
        if title:
            return ParsedTable(
                title=title,
                headers=[],
                rows=[],
                row_labels=[],
                source_format="table_title_only",
            )
        return None

    def split(self, table: ParsedTable) -> list[TablePart]:
        if not table.rows:
            text = self._render_text(table.title, table.headers, [], [])
            if not text:
                return []
            return [
                TablePart(
                    text=text,
                    html=self._render_html(table.headers, []),
                    headers=table.headers,
                    rows=[],
                    row_start=None,
                    row_end=None,
                    source_format=table.source_format,
                    original_html=table.original_html,
                )
            ]
        parts: list[TablePart] = []
        current_rows: list[list[str]] = []
        current_labels: list[str] = []
        for row, label in zip(table.rows, table.row_labels, strict=True):
            candidate_rows = [*current_rows, row]
            candidate_labels = [*current_labels, label]
            candidate_text = self._render_text(
                table.title, table.headers, candidate_rows, candidate_labels
            )
            if current_rows and self.estimator.count(candidate_text) > self.settings.hard_max_tokens:
                parts.extend(self._part_or_split(table, current_rows, current_labels))
                current_rows = []
                current_labels = []
            current_rows.append(row)
            current_labels.append(label)
            current_text = self._render_text(
                table.title, table.headers, current_rows, current_labels
            )
            if self.estimator.count(current_text) >= self.settings.target_tokens:
                parts.extend(self._part_or_split(table, current_rows, current_labels))
                current_rows = []
                current_labels = []
        if current_rows:
            parts.extend(self._part_or_split(table, current_rows, current_labels))
        return parts

    def _part_or_split(
        self, table: ParsedTable, rows: list[list[str]], labels: list[str]
    ) -> list[TablePart]:
        part = self._part(table, rows, labels)
        if part.hard_max_exception is None or len(rows) != 1:
            return [part]
        return self._split_single_row(table, rows[0], labels[0])

    def _split_single_row(
        self, table: ParsedTable, row: list[str], label: str
    ) -> list[TablePart]:
        """Split an exceptionally verbose row by cell while retaining its headers."""
        base_tokens = self.estimator.count(
            self._render_text(table.title, table.headers, [], [])
        )
        width = max(len(row), len(table.headers))
        parts: list[TablePart] = []
        for column, value in enumerate(row):
            if not value:
                continue
            header = (
                table.headers[column]
                if column < len(table.headers)
                else f"column_{column + 1}"
            )
            prefix_tokens = self.estimator.count(f"Row {label}.c{column + 1}: {header}=")
            budget = max(
                32,
                self.settings.hard_max_tokens - base_tokens - prefix_tokens - 12,
            )
            fragments = self.estimator.split_text(value, budget)
            for fragment_index, fragment in enumerate(fragments, start=1):
                fragment_row = [""] * width
                fragment_row[column] = fragment
                fragment_label = f"{label}.c{column + 1}.p{fragment_index}"
                parts.append(self._part(table, [fragment_row], [fragment_label]))
        return parts or [self._part(table, [row], [label])]

    def _part(
        self, table: ParsedTable, rows: list[list[str]], labels: list[str]
    ) -> TablePart:
        text = self._render_text(table.title, table.headers, rows, labels)
        exception = None
        if self.estimator.count(text) > self.settings.hard_max_tokens:
            exception = "single_structured_table_row_exceeds_hard_max"
        return TablePart(
            text=text,
            html=self._render_html(table.headers, rows),
            headers=list(table.headers),
            rows=[list(row) for row in rows],
            row_start=labels[0] if labels else None,
            row_end=labels[-1] if labels else None,
            source_format=table.source_format,
            original_html=table.original_html,
            hard_max_exception=exception,
        )

    @staticmethod
    def _combine_header_rows(rows: list[list[str]]) -> list[str]:
        width = max((len(row) for row in rows), default=0)
        headers: list[str] = []
        for column in range(width):
            values = [row[column] for row in rows if column < len(row) and row[column]]
            headers.append(" / ".join(dict.fromkeys(values)) or f"column_{column + 1}")
        return headers

    def _from_excel_cells(
        self, cells: list[dict[str, Any]], title: str, metadata: dict[str, Any]
    ) -> ParsedTable:
        values: dict[tuple[int, int], str] = {}
        for cell in cells:
            value = cell.get("value")
            if value is None or str(value).strip() == "":
                continue
            values[(int(cell["row"]), int(cell["column"]))] = _clean_cell(value)
        for merged in metadata.get("merged_cell_values") or []:
            value = merged.get("value")
            if value is None or str(value).strip() == "":
                continue
            cleaned = _clean_cell(value)
            for row in range(int(merged["min_row"]), int(merged["max_row"]) + 1):
                for column in range(
                    int(merged["min_column"]), int(merged["max_column"]) + 1
                ):
                    values.setdefault((row, column), cleaned)
        if not values:
            return ParsedTable(title=title, headers=[], rows=[], row_labels=[], source_format="xlsx")
        row_numbers = sorted({row for row, _ in values})
        min_column = min(column for _, column in values)
        max_column = max(column for _, column in values)
        matrix = [
            [values.get((row, column), "") for column in range(min_column, max_column + 1)]
            for row in row_numbers
        ]
        header_index = next(
            (index for index, row in enumerate(matrix) if sum(bool(value) for value in row) >= 2),
            0,
        )
        preheader = [value for row in matrix[:header_index] for value in row if value]
        if preheader:
            prefix = " — ".join(dict.fromkeys(preheader))
            title = f"{title}: {prefix}" if title else prefix
        headers = [value or f"column_{index + min_column}" for index, value in enumerate(matrix[header_index])]
        data_rows = matrix[header_index + 1 :]
        labels = [str(row) for row in row_numbers[header_index + 1 :]]
        if not self._headers_are_reasonable(headers):
            headers = [f"column_{index + min_column}" for index in range(len(matrix[header_index]))]
            data_rows = matrix[header_index:]
            labels = [str(row) for row in row_numbers[header_index:]]
        return ParsedTable(
            title=title,
            headers=headers,
            rows=data_rows,
            row_labels=labels,
            source_format="xlsx_cells",
            metadata={
                "sheet_name": metadata.get("sheet_name"),
                "used_range": metadata.get("used_range"),
                "merged_cells": metadata.get("merged_cells") or [],
                "merged_values_propagated": bool(metadata.get("merged_cell_values")),
            },
        )

    def _from_markdown(self, text: str, title: str) -> ParsedTable | None:
        rows: list[list[str]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if "|" not in stripped:
                continue
            values = [_clean_cell(value) for value in stripped.strip("|").split("|")]
            if values and all(MARKDOWN_SEPARATOR_RE.match(value.replace(" ", "")) for value in values):
                continue
            if any(values):
                rows.append(values)
        if not rows:
            return None
        headers = rows[0]
        data_rows = rows[1:]
        first_label = 2
        if not self._headers_are_reasonable(headers):
            headers = [f"column_{index + 1}" for index in range(len(rows[0]))]
            data_rows = rows
            first_label = 1
        return ParsedTable(
            title=title,
            headers=headers,
            rows=data_rows,
            row_labels=[str(index) for index in range(first_label, len(rows) + 1)],
            source_format="markdown_table",
        )

    def _headers_are_reasonable(self, headers: list[str]) -> bool:
        if not headers:
            return True
        combined = "Headers: " + " | ".join(headers)
        return (
            self.estimator.count(combined) <= self.settings.hard_max_tokens // 2
            and max(self.estimator.count(value) for value in headers)
            <= self.settings.hard_max_tokens // 3
        )

    @staticmethod
    def _render_text(
        title: str, headers: list[str], rows: list[list[str]], row_labels: list[str]
    ) -> str:
        lines: list[str] = []
        if title:
            lines.append(f"Table: {title}")
        if headers:
            lines.append("Headers: " + " | ".join(headers))
        for position, row in enumerate(rows):
            label = row_labels[position] if position < len(row_labels) else str(position + 1)
            values = []
            for index, value in enumerate(row):
                header = headers[index] if index < len(headers) else f"column_{index + 1}"
                if value:
                    values.append(f"{header}={value}")
            if values:
                lines.append(f"Row {label}: " + " | ".join(values))
        return "\n".join(lines).strip()

    @staticmethod
    def _render_html(headers: list[str], rows: list[list[str]]) -> str:
        if not headers and not rows:
            return ""
        parts = ["<table>"]
        if headers:
            parts.append("<thead><tr>")
            parts.extend(f"<th>{html.escape(value)}</th>" for value in headers)
            parts.append("</tr></thead>")
        parts.append("<tbody>")
        for row in rows:
            parts.append("<tr>")
            parts.extend(f"<td>{html.escape(value)}</td>" for value in row)
            parts.append("</tr>")
        parts.append("</tbody></table>")
        return "".join(parts)
