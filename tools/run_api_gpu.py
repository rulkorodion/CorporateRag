from __future__ import annotations

"""Launch the single-worker FastAPI backend with the existing CUDA runtime."""

import os
import site
import sys
from pathlib import Path


os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import torch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_SITE_PACKAGES = PROJECT_ROOT / ".venv" / "Lib" / "site-packages"


def main() -> int:
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable in the selected Python environment")
    if not PROJECT_SITE_PACKAGES.is_dir():
        raise RuntimeError(f"project virtualenv is missing: {PROJECT_SITE_PACKAGES}")
    site.addsitedir(str(PROJECT_SITE_PACKAGES))
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

    import uvicorn

    from corporate_rag_ingestion.api.settings import APISettings

    settings = APISettings()
    # Exactly one worker is intentional: queues and loaded models are process-local.
    uvicorn.run(
        "corporate_rag_ingestion.api.app:app",
        host=settings.host,
        port=settings.port,
        workers=1,
        log_level="info",
        access_log=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

