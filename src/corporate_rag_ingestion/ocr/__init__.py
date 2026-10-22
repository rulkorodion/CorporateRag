"""Neutral OCR subsystem for imported and future MonkeyOCR results."""

from .models import (
    OCRBlock,
    OCRBoundingBox,
    OCRFingerprint,
    OCRLayoutElement,
    OCRPageIdentity,
    OCRPageResult,
    OCRProvenance,
)
from .settings import MonkeyOCRSettings

__all__ = [
    "MonkeyOCRSettings",
    "OCRBlock",
    "OCRBoundingBox",
    "OCRFingerprint",
    "OCRLayoutElement",
    "OCRPageIdentity",
    "OCRPageResult",
    "OCRProvenance",
]
