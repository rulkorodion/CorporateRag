from __future__ import annotations

from corporate_rag_ingestion.context.models import TableRepresentation
from corporate_rag_ingestion.context.table_rendering import TableContextRenderer


def test_maps_headers_to_cells_and_preserves_numbers() -> None:
    table = TableRepresentation(
        headers=["Действие", "Ответственный", "Сумма"],
        rows=[["Подать заявление", "Казначейство", "125 000 руб."]],
        html="<table>original</table>",
        original_html="<table>source</table>",
    )
    rendered = TableContextRenderer().render(table, "Table: ГОЗ")
    assert "Действие: Подать заявление" in rendered
    assert "Ответственный: Казначейство" in rendered
    assert "Сумма: 125 000 руб." in rendered
    assert table.html == "<table>original</table>"
    assert table.original_html == "<table>source</table>"


def test_empty_cells_are_explicit_and_never_shift_columns() -> None:
    table = TableRepresentation(
        headers=["Раздел", "Тема обращения", "К кому обращаться", "Способ связи"],
        rows=[["Канцелярия", "Заказать визитки", "", ""]],
    )
    rendered = TableContextRenderer().render(table, "Table: Контакты")
    assert "Раздел: Канцелярия" in rendered
    assert "К кому обращаться: НЕ УКАЗАНО (ячейка пуста; не брать значение из соседнего поля)" in rendered
    assert "Способ связи: НЕ УКАЗАНО (ячейка пуста; не брать значение из соседнего поля)" in rendered


def test_similar_columns_and_rows_remain_separate() -> None:
    table = TableRepresentation(
        headers=["Действие", "Ответственный", "Получатель", "Канал информации"],
        rows=[
            ["Подать заявление", "Казначейство", "Банк", "Клиент-банк"],
            ["Направить уведомление", "Казначейство", "Менеджер", "эл.почта"],
        ],
        row_start="28",
    )
    rendered = TableContextRenderer().render(table, "Table: ГОЗ")
    assert "Строка 28:" in rendered
    assert "Ответственный: Казначейство" in rendered
    assert "Получатель: Банк" in rendered
    assert "Канал информации: Клиент-банк" in rendered
    assert "Строка 29:" in rendered
    assert "Получатель: Менеджер" in rendered


def test_row_numbers_are_disambiguated_from_source_citations() -> None:
    table = TableRepresentation(
        headers=["Ответственный"],
        rows=[["Казначейство"]],
        row_start=28,
    )
    rendered = TableContextRenderer().render(table, "Table: ГОЗ", citation="[1]")
    assert "Строка 28 (цитировать как источник [1]):" in rendered
    assert "Все строки выше относятся только к источнику [1]" in rendered


def test_repeated_headers_get_deterministic_column_qualifiers() -> None:
    table = TableRepresentation(
        headers=["Действие", "Действие"],
        rows=[["Открытие счёта", "Подать заявление"]],
    )
    renderer = TableContextRenderer()
    first = renderer.render(table, "Table: ГОЗ")
    second = renderer.render(table, "Table: ГОЗ")
    assert first == second
    assert "Действие (колонка 1): Открытие счёта" in first
    assert "Действие (колонка 2): Подать заявление" in first


def test_resolves_multilevel_xlsx_header_from_related_table_rows() -> None:
    renderer = TableContextRenderer()
    repeated = ["РЕГЛАМЕНТ ГОЗ"] * 9
    semantic = [
        "Этапы", "Подэтап", "№ п/п", "Действие", "Описание действия",
        "На что обратить внимание", "Ответственный", "Получатель информации",
        "Канал информации",
    ]
    headers, reason = renderer.resolve_headers(
        repeated,
        [["III", "Выполнение", "9", "Открытие", "Подать", "", "Казначейство", "Банк", "Клиент-банк"]],
        "Table: ГОЗ\nHeaders: " + " | ".join(repeated),
        [semantic],
    )
    assert reason == "related_table_header_row"
    assert headers[6:] == ["Ответственный", "Получатель информации", "Канал информации"]


def test_long_and_ocr_tables_use_same_lossless_rendering() -> None:
    rows = [[f"Параметр {index}", str(index)] for index in range(1, 51)]
    table = TableRepresentation(
        headers=["OCR-параметр", "Значение"],
        rows=rows,
        html="<table>ocr html</table>",
        render_headers=["OCR-параметр", "Значение"],
        render_header_source="ocr_layout",
    )
    rendered = TableContextRenderer().render(table, "OCR table")
    assert rendered.count("Строка ") == 50
    assert "OCR-параметр: Параметр 50" in rendered
    assert "Значение: 50" in rendered
    assert table.html == "<table>ocr html</table>"
    assert table.render_header_source == "ocr_layout"
