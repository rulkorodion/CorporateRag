from __future__ import annotations

"""Start the single-process production API using env-driven settings."""

import uvicorn

from corporate_rag_ingestion.api.settings import APISettings


def main() -> int:
    settings = APISettings()
    uvicorn.run(
        "corporate_rag_ingestion.api.app:app",
        host=settings.host,
        port=settings.port,
        workers=1,
        log_level="info",
        access_log=True,
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
