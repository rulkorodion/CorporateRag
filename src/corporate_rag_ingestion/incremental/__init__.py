"""Safe per-document production knowledge-base updates."""

from .models import IncrementalPlan, IncrementalUpdateReport
from .service import IncrementalUpdater

__all__ = ["IncrementalPlan", "IncrementalUpdateReport", "IncrementalUpdater"]
