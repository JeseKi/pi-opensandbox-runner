#!/usr/bin/env python3
"""Prepare an ephemeral Pi configuration before starting the bridge."""

from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path


def main() -> None:
    config_dir = Path(os.environ.setdefault("PI_CODING_AGENT_DIR", "/run/pi-agent"))
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = Path("/opt/pi-runner/runner-catalog.toml")
    persistent = Path(
        os.environ.setdefault("PI_MODEL_CATALOG_PATH", "/root/.pi/bridge/models.json")
    )
    persistent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not persistent.exists():
        catalog = tomllib.loads(source.read_text(encoding="utf-8"))
        configured_models = catalog.get("models") if isinstance(catalog, dict) else None
        if not isinstance(configured_models, list):
            raise RuntimeError("runner catalog must contain a models list")
        models = [
            {
                "id": model["slug"],
                "name": model["label"],
                "reasoning": model["reasoning"],
                "input": ["text"],
                "contextWindow": model["context_window"],
                "maxTokens": model["max_tokens"],
            }
            for model in configured_models
            if isinstance(model, dict)
        ]
        if not models or "coding-default" not in {model["id"] for model in models}:
            raise RuntimeError("runner catalog must include coding-default")
        persistent.write_text(
            json.dumps(
                {
                    "providers": {
                        "litellm": {
                            "baseUrl": "http://litellm:4000/v1",
                            "api": "openai-completions",
                            "apiKey": "$LITELLM_VIRTUAL_KEY",
                            "authHeader": True,
                            "models": models,
                        }
                    }
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        persistent.chmod(0o600)
    destination = config_dir / "models.json"
    destination.unlink(missing_ok=True)
    destination.symlink_to(persistent)
    os.execvp("python", ["python", "-m", "pi_opensandbox_runner", *sys.argv[1:]])


if __name__ == "__main__":
    main()
