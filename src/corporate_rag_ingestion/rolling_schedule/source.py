from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import httpx


LOGGER = logging.getLogger(__name__)

# Verified against the public workbook on 2026-10-14. Runtime HTML discovery
# overrides this map, so a recreated tab automatically receives its new gid.
KNOWN_GIDS: dict[str, str] = {
    "159": "0",
    "Королев": "839209310",
    "Кореец": "1043051827",
    "ЭЗТМ": "1067623881",
    "51": "1074504540",
    "89": "893659128",
    "250": "1756547465",
    "ПИ": "1231142009",
    "Ситко": "1007749370",
    "Элмаксан": "122757128",
    "Адафер": "1495343610",
    "Китаец": "844355558",
    "Белорус": "394458907",
    "Малтибест": "177144745",
    "114": "1050618602",
    "76": "1310055600",
    "127ТМ": "1190974707",
    "76ТМ": "2108619200",
    "Челябинск": "162688732",
    "El_Череп": "1341880315",
    "Лист ДМЦ ГК": "934446126",
    "Лист ДМЦ ХК": "940677372",
    "Ижевск ХК": "2117756166",
    "Ижевск ХК 2": "1347830110",
    "Ижевск ХК 3": "550311929",
    "Стан_ХК НСК": "1065948994",
    "Стан_ГК НСК": "506626923",
    "ГК ВЛГ": "1955398343",
    "ХК ВЛГ": "1190039813",
}

_SHEET_METADATA = re.compile(
    r'\[(\d+),0,\\"(\d+)\\",\[\{\\"1\\":\[\[0,0,\\"([^\\"]+)\\"'
)


class ScheduleDownloadError(RuntimeError):
    pass


def parse_gid_map(html: str) -> dict[str, str]:
    return {name: gid for _position, gid, name in _SHEET_METADATA.findall(html)}


def _retryable_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def download_file(
    url: str,
    target: Path,
    *,
    timeout_seconds: float,
    retries: int,
    max_bytes: int,
    client: httpx.Client | None = None,
) -> int:
    owns_client = client is None
    active_client = client or httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(timeout_seconds),
    )
    try:
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                with active_client.stream("GET", url) as response:
                    if _retryable_status(response.status_code):
                        raise ScheduleDownloadError(
                            f"temporary HTTP {response.status_code} from schedule source"
                        )
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    written = 0
                    with target.open("wb") as handle:
                        for chunk in response.iter_bytes():
                            written += len(chunk)
                            if written > max_bytes:
                                raise ScheduleDownloadError(
                                    f"schedule XLSX exceeds {max_bytes} bytes"
                                )
                            handle.write(chunk)
                    if written < 4:
                        raise ScheduleDownloadError("schedule download is empty")
                    with target.open("rb") as handle:
                        signature = handle.read(4)
                    if signature != b"PK\x03\x04":
                        raise ScheduleDownloadError(
                            "downloaded response is not an XLSX ZIP archive "
                            f"(content-type={content_type or 'unknown'})"
                        )
                    return written
            except (httpx.HTTPError, OSError, ScheduleDownloadError) as exc:
                last_error = exc
                if target.exists():
                    target.unlink()
                if attempt >= retries:
                    break
                delay = min(8.0, 2.0 ** (attempt - 1))
                LOGGER.warning(
                    "rolling schedule download attempt %d/%d failed: %s; retry in %.1fs",
                    attempt,
                    retries,
                    exc,
                    delay,
                )
                time.sleep(delay)
        raise ScheduleDownloadError(
            f"schedule download failed after {retries} attempt(s): {last_error}"
        )
    finally:
        if owns_client:
            active_client.close()


def discover_gid_map(
    edit_url: str,
    *,
    timeout_seconds: float,
    client: httpx.Client | None = None,
) -> tuple[dict[str, str], str | None]:
    owns_client = client is None
    active_client = client or httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(timeout_seconds),
    )
    try:
        try:
            response = active_client.get(edit_url)
            response.raise_for_status()
            discovered = parse_gid_map(response.text)
            if not discovered:
                raise ScheduleDownloadError("public sheet metadata contained no tab gids")
            return {**KNOWN_GIDS, **discovered}, None
        except (httpx.HTTPError, ScheduleDownloadError) as exc:
            LOGGER.warning("failed to refresh Google Sheet gid map: %s", exc)
            return dict(KNOWN_GIDS), str(exc)
    finally:
        if owns_client:
            active_client.close()
