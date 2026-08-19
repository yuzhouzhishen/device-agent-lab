#!/usr/bin/env python3
"""Expose the sibling Firmware RAG FastAPI app over JSON lines on stdio."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient

from firmware_knowledge_agent.api import create_app
from firmware_knowledge_agent.service import FirmwareKnowledgeService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    service = FirmwareKnowledgeService(args.catalog)
    try:
        with TestClient(create_app(service)) as client:
            for line in sys.stdin:
                try:
                    request = json.loads(line)
                    response = client.request(
                        str(request.get("method", "GET")),
                        str(request.get("path", "/")),
                        json=request.get("json"),
                    )
                    result = {
                        "status_code": response.status_code,
                        "headers": {
                            "content-type": response.headers.get(
                                "content-type",
                                "application/json",
                            )
                        },
                        "content": response.text,
                    }
                except Exception as exc:  # noqa: BLE001
                    result = {
                        "status_code": 500,
                        "headers": {"content-type": "application/json"},
                        "content": json.dumps(
                            {
                                "detail": str(exc),
                                "error_type": type(exc).__name__,
                            }
                        ),
                    }
                print(
                    json.dumps(result, ensure_ascii=False),
                    flush=True,
                )
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
