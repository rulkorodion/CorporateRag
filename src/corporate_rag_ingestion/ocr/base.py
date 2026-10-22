from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from PIL import Image

from .models import OCREnginePage


class OCREngine(ABC):
    @abstractmethod
    def prepare(self) -> dict[str, Any]:
        """Load resources once for the worker process and return runtime metadata."""

    @abstractmethod
    def recognize_page(
        self,
        image: Image.Image,
        *,
        page_number: int,
        preprocessing_profile: str,
    ) -> OCREnginePage:
        """Recognize one already rendered page."""
