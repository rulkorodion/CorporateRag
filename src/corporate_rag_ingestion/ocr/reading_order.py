from __future__ import annotations

from .models import OCRBlock


def ordered_blocks(blocks: list[OCRBlock], page_height: int) -> list[OCRBlock]:
    """Prefer model reading order; use geometry for records without it."""

    if not blocks:
        return []
    if all(block.reading_order is not None for block in blocks):
        return sorted(blocks, key=lambda block: int(block.reading_order or 0))
    tolerance = max(8.0, page_height * 0.004)
    source = sorted(
        blocks,
        key=lambda block: (
            (block.bounding_box.y_min + block.bounding_box.y_max) / 2,
            block.bounding_box.x_min,
        ),
    )
    lines: list[list[OCRBlock]] = []
    centers: list[float] = []
    for block in source:
        center = (block.bounding_box.y_min + block.bounding_box.y_max) / 2
        if not lines or abs(center - centers[-1]) > tolerance:
            lines.append([block])
            centers.append(center)
        else:
            lines[-1].append(block)
            centers[-1] = sum(
                (item.bounding_box.y_min + item.bounding_box.y_max) / 2
                for item in lines[-1]
            ) / len(lines[-1])
    return [item for line in lines for item in sorted(line, key=lambda block: block.bounding_box.x_min)]


def normalized_block_text(blocks: list[OCRBlock], page_height: int) -> str:
    return "\n".join(
        block.text.strip() for block in ordered_blocks(blocks, page_height) if block.text.strip()
    )
