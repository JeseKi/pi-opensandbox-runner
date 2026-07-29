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


def test_profiles_are_default_deny_and_include_internal_litellm() -> None:
    result = run_resolver("", "github", "npm-global")
    assert result.returncode == 0, result.stderr

    state = json.loads(result.stdout)
    assert state["profiles"] == ["github", "npm-global"]
    assert state["custom_domains"] == []
    assert state["policy"]["defaultAction"] == "deny"
    targets = {rule["target"] for rule in state["policy"]["egress"]}
    assert {"litellm", "github.com", "registry.npmjs.org"} <= targets


def test_custom_allowlist_rejects_urls_and_preserves_wildcards(tmp_path: Path) -> None:
    allowlist = tmp_path / "allowlist.txt"
    allowlist.write_text("# comment\napi.example.com\n*.trusted.example.net\n", encoding="utf-8")
    result = run_resolver(str(allowlist))
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["custom_domains"] == ["*.trusted.example.net", "api.example.com"]

    allowlist.write_text("https://example.com\n", encoding="utf-8")
    result = run_resolver(str(allowlist))
    assert result.returncode != 0
    assert "Invalid domain" in result.stderr
