#!/bin/sh

# Docker's embedded DNS installs an OUTPUT jump for 127.0.0.11 before the
# egress sidecar starts.  On named Docker networks that jump would otherwise
# win over egress's appended DNS redirect, bypassing FQDN-policy resolution.
# Keep egress's redirect first, while preserving its SO_MARK escape hatch for
# the proxy's own upstream lookup.
set -eu

install_dns_redirect_priority() {
  for protocol in udp tcp; do
    # Remove the sidecar's appended copy, then re-add it at the top. Repeating
    # this is idempotent and handles the brief interval before egress starts.
    iptables -t nat -D OUTPUT -p "$protocol" --dport 53 -m mark --mark 0x1 -j RETURN 2>/dev/null || true
    iptables -t nat -D OUTPUT -d 127.0.0.11/32 -p "$protocol" --dport 53 -m mark --mark 0x1 -j DOCKER_OUTPUT 2>/dev/null || true
    # Docker's embedded resolver actually listens on a per-container port.
    # Send the proxy's marked lookup through Docker's DNAT jump, then keep the
    # ordinary marked escape hatch and finally the sandbox DNS redirect.
    iptables -t nat -I OUTPUT 1 -d 127.0.0.11/32 -p "$protocol" --dport 53 -m mark --mark 0x1 -j DOCKER_OUTPUT
    iptables -t nat -I OUTPUT 2 -p "$protocol" --dport 53 -m mark --mark 0x1 -j RETURN
    iptables -t nat -D OUTPUT -p "$protocol" --dport 53 -j REDIRECT --to-ports 15353 2>/dev/null || true
    iptables -t nat -I OUTPUT 3 -p "$protocol" --dport 53 -j REDIRECT --to-ports 15353
  done
}

/opt/opensandbox-egress/supervisor \
  --pre-start=/opt/opensandbox-egress/cleanup.sh \
  --name=egress \
  --grace-period=20s \
  -- /opt/opensandbox-egress/egress &
supervisor_pid=$!

while kill -0 "$supervisor_pid" 2>/dev/null; do
  install_dns_redirect_priority || true
  sleep 1
done

wait "$supervisor_pid"
