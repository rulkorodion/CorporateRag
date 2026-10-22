from __future__ import annotations

"""Run retrieval with a pre-existing CUDA PyTorch environment.

The external environment is not modified. Its Python imports CUDA-enabled
torch first, then this launcher appends the RAG environment for retrieval-only
packages such as Qdrant, FastEmbed, and SentenceTransformers.
"""

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
        raise RuntimeError(
            "CUDA is unavailable in the selected Python environment; "
            "use the normal CLI for CPU fallback"
        )
    if not PROJECT_SITE_PACKAGES.is_dir():
        raise RuntimeError(f"project virtualenv is missing: {PROJECT_SITE_PACKAGES}")

    # Appending preserves the CUDA torch already loaded from the launcher env.
    site.addsitedir(str(PROJECT_SITE_PACKAGES))
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

    from corporate_rag_ingestion.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
