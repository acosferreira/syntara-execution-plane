#!/usr/bin/env python3
"""Local-dev stub for EP's credential-resolution callback.

The Execution Plane worker resolves a Cluster's ``credential_ref`` by posting a
``credential.resolve`` command to ``EP_CREDENTIAL_RESOLUTION_CALLBACK_URL``.
The real callback lives in the orchestrator (AO), which does not implement it
yet on this branch. This stub stands in for it: it mints a fresh
``syntara-dispatcher`` service-account token from the local kind cluster on
every request, so tokens never go stale.

Run it on the host (where kubectl and the kind kubeconfig live):

    uv run python tools/credential_resolution_stub.py

Then point the worker at it (compose.yaml wires the CA mount):

    EP_CREDENTIAL_RESOLUTION_CALLBACK_URL=https://host.containers.internal:8444 \
      uvx podman-compose up -d worker
"""

from __future__ import annotations

import json
import ssl
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CERT_FILE = PROJECT_ROOT / ".secrets" / "certs" / "credential-stub.crt"
KEY_FILE = PROJECT_ROOT / ".secrets" / "certs" / "credential-stub.key"
BIND_HOST = "0.0.0.0"
BIND_PORT = 8444
KUBECTL = ("kubectl", "--context", "kind-ep-kind", "-n", "execution-plane", "create", "token", "syntara-dispatcher")


def _dispatcher_token() -> str:
    """Mint a fresh service-account token from the kind cluster."""
    return subprocess.run(KUBECTL, capture_output=True, text=True, check=True).stdout.strip()


class Handler(BaseHTTPRequestHandler):
    """Answer ``credential.resolve`` command envelopes with a live token."""

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            message_id = body["message_id"]
        except (ValueError, KeyError) as error:
            self._reply(400, {"detail": f"malformed command envelope: {error}"})
            return
        command_type = body.get("command", {}).get("command_type")
        if command_type != "credential.resolve":
            self._reply(400, {"detail": f"unsupported command_type: {command_type!r}"})
            return
        try:
            credential = _dispatcher_token()
        except subprocess.CalledProcessError as error:
            self._reply(502, {"detail": f"token mint failed: {error.stderr.strip()}"})
            return
        # CredentialResolution: correlated message_id plus the secret value.
        self._reply(200, {"message_id": message_id, "credential": credential})

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if self.path == "/healthz":
            self._reply(200, {"status": "ok"})
            return
        self._reply(404, {"detail": "not found"})

    def _reply(self, status: int, payload: dict[str, object]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        sys.stderr.write("[stub] %s\n" % (format % args))


def main() -> int:
    """Serve the stub over TLS with the EP-CA-signed certificate."""
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERT_FILE, KEY_FILE)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print(f"[stub] credential-resolution stub listening on https://{BIND_HOST}:{BIND_PORT}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
