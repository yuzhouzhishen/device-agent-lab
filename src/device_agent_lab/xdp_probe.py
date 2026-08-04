from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from collections.abc import Mapping
from typing import Any

from dotenv import load_dotenv

from device_agent_lab.device_gateway import McpDeviceGateway
from device_agent_lab.runtime import RuntimeSettings, create_runtime


_SENSITIVE_KEY_TOKENS = {
    "psn",
    "token",
    "url",
    "ssid",
    "bssid",
    "mac",
    "serial",
}
_SENSITIVE_COMPACT_KEYS = {
    "accesstoken",
    "devicepsn",
    "endpointurl",
    "macaddress",
    "serialnumber",
}
_URL_PATTERN = re.compile(r"https?://[^\s\"']+")
_LONG_IDENTIFIER_PATTERN = re.compile(r"(?<!\d)\d{10,}(?!\d)")
_MAC_PATTERN = re.compile(
    r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}:){5}[0-9a-f]{2}(?![0-9a-f])"
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run an allowlisted, read-only probe against the configured "
            "XDP MCP endpoint."
        ),
    )
    parser.parse_args()
    load_dotenv()
    try:
        asyncio.run(run_probe(dict(os.environ)))
    except (RuntimeError, ValueError) as exc:
        parser.error(redact_sensitive_text(str(exc)))


async def run_probe(values: Mapping[str, str]) -> dict[str, Any]:
    runtime_values = dict(values)
    runtime_values["DEVICE_BACKEND"] = "xdp"
    runtime_values["DEVICE_PLANNER"] = "rules"
    settings = RuntimeSettings.from_mapping(runtime_values)
    runtime = await create_runtime(settings)
    try:
        if not isinstance(runtime.gateway, McpDeviceGateway):
            raise RuntimeError("XDP probe did not create an MCP gateway")
        report = await runtime.gateway.probe_xdp_read_only()
        safe_report = redact_sensitive_values(report)
        print(json.dumps(safe_report, ensure_ascii=False, indent=2))
        return safe_report
    finally:
        await runtime.aclose()


def redact_sensitive_values(value: Any, *, key: str = "") -> Any:
    if _is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            item_key: redact_sensitive_values(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_values(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def redact_sensitive_text(value: str) -> str:
    redacted = _URL_PATTERN.sub("[REDACTED_URL]", value)
    redacted = _MAC_PATTERN.sub("[REDACTED_MAC]", redacted)
    return _LONG_IDENTIFIER_PATTERN.sub("[REDACTED_ID]", redacted)


def _is_sensitive_key(key: str) -> bool:
    snake_key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).lower()
    token_list = re.findall(r"[a-z0-9]+", snake_key)
    tokens = set(token_list)
    compact = "".join(token_list)
    return bool(tokens & _SENSITIVE_KEY_TOKENS) or (
        compact in _SENSITIVE_COMPACT_KEYS
    )


if __name__ == "__main__":
    main()
