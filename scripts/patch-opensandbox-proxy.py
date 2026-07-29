#!/usr/bin/env python3
"""Add Pi runner's external bridge-token guard to OpenSandbox v0.2.2 proxy.py.

The upstream Docker runtime does not support ``secureAccess``.  This narrowly
scoped patch protects only Pi runner bridge API routes and leaves OpenSandbox's
own lifecycle API unchanged.  It deliberately relies on upstream's sensitive
header filtering so the validated bearer token never reaches the sandbox.
"""

from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path("/app/opensandbox_server/api/proxy.py")
TOKEN_HASH_METADATA_KEY = "pi-runner.bridge-proxy-token-sha256"


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError(f"expected exactly one occurrence: {old[:80]!r}")
    return source.replace(old, new)


def main() -> None:
    source = TARGET.read_text()
    source = replace_once(
        source, "import hmac\n", "import base64\nimport hashlib\nimport hmac\n"
    )
    source = replace_once(
        source,
        'router = APIRouter(tags=["Sandboxes"])\n',
        'router = APIRouter(tags=["Sandboxes"])\n\n'
        f'BRIDGE_PROXY_TOKEN_HASH_METADATA_KEY = "{TOKEN_HASH_METADATA_KEY}"\n',
    )
    marker = (
        "\ndef _verify_secure_access("
        "endpoint: Endpoint, caller_headers: Mapping[str, str]) -> None:\n"
    )
    guard = '''

def _bridge_proxy_token(headers: Mapping[str, str]) -> str | None:
    """Return a strict Bearer credential without forwarding it downstream."""
    for key, value in headers.items():
        if key.lower() != "authorization":
            continue
        scheme, separator, credential = value.partition(" ")
        if (
            scheme.lower() == "bearer"
            and separator
            and credential
            and credential == credential.strip()
        ):
            return credential
        return None
    return None


def _requires_bridge_proxy_token(port: int, full_path: str) -> bool:
    return port == 8765 and (full_path == "v1" or full_path.startswith("v1/"))


def _bridge_proxy_token_hash(sandbox_id: str, port: int) -> str | None:
    if port != 8765:
        return None
    sandbox = lifecycle.sandbox_service.get_sandbox(sandbox_id)
    metadata = sandbox.metadata or {}
    return metadata.get(BRIDGE_PROXY_TOKEN_HASH_METADATA_KEY)


def _verify_bridge_proxy_token(
    sandbox_id: str,
    port: int,
    full_path: str,
    caller_headers: Mapping[str, str],
) -> None:
    """Validate Pi runner's external token against its metadata digest."""
    if not _requires_bridge_proxy_token(port, full_path):
        return
    expected_hash = _bridge_proxy_token_hash(sandbox_id, port)
    if not expected_hash:
        return
    token = _bridge_proxy_token(caller_headers)
    actual_hash = (
        "h"
        + base64.urlsafe_b64encode(hashlib.sha256(token.encode()).digest())
        .decode()
        .rstrip("=")
        if token
        else ""
    )
    if not hmac.compare_digest(actual_hash, expected_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "MISSING_OR_INVALID_BRIDGE_PROXY_TOKEN",
                "message": "A valid bridge proxy bearer token is required.",
            },
        )
'''
    source = replace_once(source, marker, guard + marker)
    source = replace_once(
        source,
        "    endpoint = lifecycle.sandbox_service.get_endpoint("
        "sandbox_id, port, resolve_internal=True)\n"
        "    _verify_secure_access(endpoint, request.headers)\n",
        "    endpoint = lifecycle.sandbox_service.get_endpoint("
        "sandbox_id, port, resolve_internal=True)\n"
        "    _verify_bridge_proxy_token(sandbox_id, port, full_path, request.headers)\n"
        "    _verify_secure_access(endpoint, request.headers)\n",
    )
    source = replace_once(
        source,
        "    try:\n"
        "        _verify_secure_access(endpoint, dict(websocket.headers))\n"
        "    except HTTPException:\n",
        "    try:\n"
        "        _verify_bridge_proxy_token(sandbox_id, port, full_path, dict(websocket.headers))\n"
        "        _verify_secure_access(endpoint, dict(websocket.headers))\n"
        "    except HTTPException:\n",
    )
    source = replace_once(
        source,
        "        # Forwarded headers are stripped above and rebuilt from the connection\n",
        "        # Legacy sandboxes validate the token inside Bridge. Preserve their\n"
        "        # behavior until explicitly recreated; protected sandboxes never receive it.\n"
        "        if port == 8765 and not _bridge_proxy_token_hash(sandbox_id, port):\n"
        "            authorization = request.headers.get(\"authorization\")\n"
        "            if authorization:\n"
        "                headers[\"Authorization\"] = authorization\n"
        "        # Forwarded headers are stripped above and rebuilt from the connection\n",
    )
    source = replace_once(
        source,
        "    _set_forwarded_headers(headers, websocket)\n",
        "    if port == 8765 and not _bridge_proxy_token_hash(sandbox_id, port):\n"
        "        authorization = websocket.headers.get(\"authorization\")\n"
        "        if authorization:\n"
        "            headers[\"Authorization\"] = authorization\n"
        "    _set_forwarded_headers(headers, websocket)\n",
    )
    if '"authorization",' not in source.split("SENSITIVE_HEADERS = {", 1)[1].split("}", 1)[0]:
        raise RuntimeError("upstream proxy no longer filters Authorization")
    TARGET.write_text(source)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"OpenSandbox proxy patch failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
