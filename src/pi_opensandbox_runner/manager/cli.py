from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser(prog="pi-runner-manager-cli")
    parser.add_argument("--base-url", default="http://127.0.0.1:8090")
    parser.add_argument("--token", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("subject_ref")
    reconcile_parser = subparsers.add_parser("reconcile")
    reconcile_parser.add_argument("subject_ref")
    stop_parser = subparsers.add_parser("stop")
    stop_parser.add_argument("subject_ref")
    destroy_parser = subparsers.add_parser("destroy")
    destroy_parser.add_argument("subject_ref")
    subparsers.add_parser("models")
    subparsers.add_parser("policies")
    args = parser.parse_args()
    method = "GET"
    path = ""
    headers: dict[str, str] = {}
    if args.command == "status":
        path = f"/v1/instances/{args.subject_ref}"
    elif args.command in {"reconcile", "stop", "destroy"}:
        method = "POST"
        path = f"/v1/instances/{args.subject_ref}:{args.command}"
        if args.command == "destroy":
            headers["X-Confirm-Destroy"] = args.subject_ref
    elif args.command == "models":
        path = "/v1/catalog/models"
    elif args.command == "policies":
        path = "/v1/catalog/policies"
    request = urllib.request.Request(
        f"{args.base_url.rstrip('/')}{path}",
        method=method,
        headers={"Authorization": f"Bearer {args.token}", **headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
    except urllib.error.HTTPError as exc:
        print(exc.read().decode(errors="replace"), file=sys.stderr)
        raise SystemExit(1) from exc
    if body:
        print(json.dumps(json.loads(body), ensure_ascii=False, indent=2))
