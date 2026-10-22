from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import httpx


QUESTIONS = [
    ("regulation", "Как создать заявку на отгрузку автомобиля?"),
    ("exact_tu", "На какие трубы распространяется ТУ 24.20.13-004-40198874-2020?"),
    ("table", "Кто подаёт заявление на открытие отдельного счёта по ГОЗ?"),
    ("outlook", "Как настроить корпоративную подпись в Outlook?"),
    ("visual_png", "Сканирование ладони на сканере Biosmart"),
    ("insufficient", "Какой пароль Wi-Fi в переговорной директора?"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report: dict[str, Any] = {"base_url": args.base_url, "results": []}
    with httpx.Client(base_url=args.base_url, timeout=args.timeout, trust_env=False) as client:
        for endpoint in ("/health", "/ready", "/openapi.json"):
            response = client.get(endpoint)
            report[endpoint] = {
                "status_code": response.status_code,
                "body": response.json() if endpoint != "/openapi.json" else {"ok": response.status_code == 200},
            }
        for label, question in QUESTIONS:
            started = time.perf_counter()
            response = client.post("/api/answer", json={"question": question})
            body = response.json()
            report["results"].append(
                {
                    "label": label,
                    "question": question,
                    "status_code": response.status_code,
                    "client_seconds": round(time.perf_counter() - started, 3),
                    "answer": body.get("answer"),
                    "citation_validation": body.get("citation_validation"),
                    "citations": body.get("citations", []),
                    "sources": body.get("sources", []),
                    "related_images": body.get("related_images", []),
                    "warnings": body.get("warnings", []),
                    "latency": body.get("latency"),
                }
            )

    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    failed = any(item["status_code"] != 200 for item in report["results"])
    return 1 if failed or report["/ready"]["status_code"] != 200 else 0


if __name__ == "__main__":
    raise SystemExit(main())
