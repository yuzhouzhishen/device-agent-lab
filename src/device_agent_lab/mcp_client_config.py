from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def build_device_mcp_connection(project_root: Path | None = None) -> dict[str, Any]:
    """Build a stdio MCP connection config for the local device MCP server."""
    if project_root is None:
        project_root = Path(__file__).resolve().parents[2]

    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-m", "device_agent_lab.mcp_server"],
        "cwd": str(project_root),
    }


def build_remote_mcp_connection(
    url: str,
    *,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Build a remote MCP connection without persisting credentials."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("remote MCP URL must be an absolute http(s) URL")

    path = parsed.path.rstrip("/")
    if path.endswith("/mcp"):
        return {
            "transport": "streamable_http",
            "url": url,
            "timeout": timeout,
            "sse_read_timeout": 60.0,
        }
    if path.endswith("/sse"):
        return {
            "transport": "sse",
            "url": url,
            "timeout": timeout,
            "sse_read_timeout": 60.0,
        }
    raise ValueError("remote MCP URL must end with /mcp or /sse")
