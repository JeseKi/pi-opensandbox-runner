#!/usr/bin/env python3
# ruff: noqa: E501
"""Enable egress sidecars on Pi runner's named Docker network.

OpenSandbox v0.2.2 creates an egress sidecar on Docker's default bridge and
therefore rejects a per-sandbox networkPolicy when ``docker.network_mode`` is
a named network. Pi runner needs the sidecar itself on ``pi-runner-internal``:
the sandbox then shares its network namespace, preserving private LiteLLM DNS
while the sidecar enforces the policy.

This is intentionally a narrow, version-pinned source patch. Every replacement
must match once so an upstream image change fails the image build safely.
"""

from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path("/app/opensandbox_server/services/docker/networking.py")


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError(f"expected exactly one occurrence: {old[:100]!r}")
    return source.replace(old, new)


def main() -> None:
    source = TARGET.read_text()
    source = replace_once(
        source,
        '''        # User-defined networks cannot be combined with networkPolicy: the egress sidecar
        # always runs on the default bridge, which would silently discard the configured network.
        if self._is_user_defined_network():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": SandboxErrorCodes.INVALID_PARAMETER,
                    "message": (
                        f"networkPolicy is not supported when docker network_mode='{self.network_mode}' "
                        "(user-defined network). Use network_mode='bridge' to enable network policy enforcement."
                    ),
                },
            )

''',
        '''        # Pi runner runs the egress sidecar on the configured named network and
        # the sandbox shares that sidecar's namespace. This retains private-service DNS
        # while still enforcing networkPolicy. The patched sidecar creation below must
        # therefore use self.network_mode instead of Docker's default bridge.

''',
    )
    source = replace_once(
        source,
        '''        base_sidecar_host_config_kwargs: dict[str, Any] = {
            "network_mode": BRIDGE_NETWORK_MODE,
            "cap_add": ["NET_ADMIN"],
''',
        '''        base_sidecar_host_config_kwargs: dict[str, Any] = {
            "network_mode": self.network_mode,
            "cap_add": ["NET_ADMIN"],
        ''',
    )
    source = replace_once(
        source,
        '''            # Sandboxes created with egress sidecar share the sidecar network namespace, so the
            # main container's private IP is not a stable proxy target. In that case, treat the
            # server-proxy target as the server-local host-mapped endpoint instead of a container IP.
            if labels.get(SANDBOX_EGRESS_AUTH_TOKEN_METADATA_KEY):
                return self._resolve_host_mapped_endpoint(
                    self._resolve_proxy_host(),
                    labels,
                    port,
                    include_egress_auth_headers=False,
                )
''',
        '''            # The sandbox shares its sidecar namespace. For sandbox traffic, proxy directly
            # to the sidecar's private address on the configured Docker network. Host-loopback
            # port mappings are intentionally reserved for host-admin egress policy management.
            if labels.get(SANDBOX_EGRESS_AUTH_TOKEN_METADATA_KEY):
                if port == 18080:
                    return self._resolve_host_mapped_endpoint(
                        self._resolve_proxy_host(),
                        labels,
                        port,
                        include_egress_auth_headers=False,
                    )
                sidecar = self.docker_client.containers.get(f"sandbox-egress-{sandbox_id}")
                return self._resolve_internal_endpoint(sidecar, port)
''',
    )
    source = replace_once(
        source,
        '''        deadline = time.monotonic() + timeout_seconds
        url = f"http://{self._resolve_proxy_host()}:{host_port}/healthz"
''',
        '''        deadline = time.monotonic() + timeout_seconds
        # The server and sidecar share the configured Docker network. Do not use
        # the sidecar's host-loopback binding here: a container cannot reach that
        # address through Docker's host-gateway alias.
        sidecar = self.docker_client.containers.get(f"sandbox-egress-{sandbox_id}")
        sidecar.reload()
        url = f"http://{self._extract_bridge_ip(sidecar)}:18080/healthz"
''',
    )
    TARGET.write_text(source)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"OpenSandbox networking patch failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
