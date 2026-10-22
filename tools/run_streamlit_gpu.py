from __future__ import annotations

"""Launch Streamlit with CUDA torch from the existing GPU environment."""

import os
import site
import sys
from pathlib import Path


os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import torch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_SITE_PACKAGES = PROJECT_ROOT / ".venv" / "Lib" / "site-packages"
APP_PATH = PROJECT_ROOT / "streamlit_app.py"


def main() -> int:
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable in the selected Python environment")
    if not PROJECT_SITE_PACKAGES.is_dir():
        raise RuntimeError(f"project virtualenv is missing: {PROJECT_SITE_PACKAGES}")
    site.addsitedir(str(PROJECT_SITE_PACKAGES))
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    from streamlit.web import cli as streamlit_cli

    sys.argv = [
        "streamlit",
        "run",
        str(APP_PATH),
        "--server.address=127.0.0.1",
        "--server.port=8501",
        "--browser.gatherUsageStats=false",
    ]
    return int(streamlit_cli.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
