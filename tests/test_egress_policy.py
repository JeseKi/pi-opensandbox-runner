from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_resolver(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail; source "$1/scripts/lib.sh"; shift; '
            'resolve_egress_policy "$1" "${@:2}"; printf "%s\\n" "$RESOLVED_EGRESS_STATE"',
            "bash",
            str(ROOT),
            *args,
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_catalog_policy_is_default_deny_and_includes_internal_services() -> None:
    result = run_resolver("consumer-default")
    assert result.returncode == 0, result.stderr

    state = json.loads(result.stdout)
    assert state["policy_slug"] == "consumer-default"
    assert state["policy"]["defaultAction"] == "deny"
    targets = {rule["target"] for rule in state["policy"]["egress"]}
    assert {"litellm", "opensandbox", "github.com", "registry.npmjs.org"} <= targets


def test_unknown_catalog_policy_is_rejected() -> None:
    result = run_resolver("does-not-exist")
    assert result.returncode != 0
    assert "Unknown runner catalog policy" in result.stderr
