"""Production HTTP API for the corporate RAG pipeline."""

from typing import Any


def create_app(*args: Any, **kwargs: Any) -> Any:
    """Load the heavy API runtime only when the application is requested."""

    from .app import create_app as factory

    return factory(*args, **kwargs)

__all__ = ["create_app"]
