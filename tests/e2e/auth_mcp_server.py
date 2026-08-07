"""Minimal Streamable HTTP MCP server used for local end-to-end verification.

It deliberately has no third-party dependencies and rejects every request that does
not carry the configured Bearer token.  It implements only the MCP methods the
Pi extension needs: initialize, tools/list, and tools/call.
"""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOKEN = os.environ.get("TEST_MCP_BEARER_TOKEN", "test-mcp-secret")


class Handler(BaseHTTPRequestHandler):
    server_version = "pi-runner-auth-mcp/1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send(HTTPStatus.OK, {"status": "ok"})
            return
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/mcp":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(HTTPStatus.UNAUTHORIZED, {"error": "missing or invalid bearer token"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON-RPC request"})
            return
        if not isinstance(request, dict):
            self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON-RPC request"})
            return
        response = self._response(request)
        if response is None:
            self.send_response(HTTPStatus.ACCEPTED)
            self.end_headers()
            return
        self._send(HTTPStatus.OK, response)

    def _response(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        if request_id is None:
            return None
        method = request.get("method")
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "authenticated-test-mcp", "version": "1.0.0"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "authenticated_echo",
                        "description": "Returns a value only after Bearer-token authentication.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"message": {"type": "string"}},
                            "required": ["message"],
                        },
                    }
                ]
            }
        elif method == "tools/call":
            params = request.get("params")
            arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
            message = arguments.get("message") if isinstance(arguments, dict) else None
            if not isinstance(params, dict) or params.get("name") != "authenticated_echo":
                return self._error(request_id, -32602, "unknown tool")
            if not isinstance(message, str):
                return self._error(request_id, -32602, "message must be a string")
            result = {"content": [{"type": "text", "text": f"authenticated:{message}"}]}
        else:
            return self._error(request_id, -32601, "method not found")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def _send(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8766), Handler).serve_forever()
