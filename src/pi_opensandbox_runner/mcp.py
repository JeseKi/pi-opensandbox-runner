from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .catalog import McpServerRecord

ENV_REFERENCE = re.compile(r"\$\{(MCP_[A-Z0-9_]+)\}")


@dataclass(frozen=True)
class McpRuntimeConfig:
    fingerprint: str
    contents: str
    required_environment: tuple[str, ...]


def build_runtime_config(servers: list[McpServerRecord]) -> McpRuntimeConfig:
    payload = {
        "mcpServers": {
            server.name: {
                "transport": "streamable-http"
                if server.transport == "streamable_http"
                else server.transport,
                "url": server.url,
                "headers": server.headers_template,
                "requestTimeoutMs": server.request_timeout_ms,
            }
            for server in servers
        }
    }
    contents = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    required = sorted(
        {
            variable
            for server in servers
            for template in server.headers_template.values()
            for variable in ENV_REFERENCE.findall(template)
        }
    )
    return McpRuntimeConfig(
        fingerprint=hashlib.sha256(contents.encode()).hexdigest(),
        contents=contents,
        required_environment=tuple(required),
    )


def missing_environment(config: McpRuntimeConfig) -> list[str]:
    return [name for name in config.required_environment if not os.environ.get(name)]


def write_runtime_config(path: Path, config: McpRuntimeConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(config.contents, encoding="utf-8")
    temporary.replace(path)
