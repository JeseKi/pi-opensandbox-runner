from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class McpRuntimeConfig:
    fingerprint: str
    contents: str
    required_environment: tuple[str, ...]


def build_runtime_config(*, enabled: bool) -> McpRuntimeConfig:
    payload: dict[str, object] = {"mcpServers": {}}
    if enabled:
        payload["mcpServers"] = {
            "litellm": {
                "transport": "streamable-http",
                "url": "http://litellm:4000/mcp/",
                "headers": {"x-litellm-api-key": "Bearer ${LITELLM_VIRTUAL_KEY}"},
                "requestTimeoutMs": 30_000,
            }
        }
    contents = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return McpRuntimeConfig(
        fingerprint=hashlib.sha256(contents.encode()).hexdigest(),
        contents=contents,
        required_environment=(),
    )


def write_runtime_config(path: Path, config: McpRuntimeConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(config.contents, encoding="utf-8")
    temporary.replace(path)
