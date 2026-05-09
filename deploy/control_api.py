#!/usr/bin/env python3
# Minimal control API that runs alongside Asterisk in the same pod.
#
# This is the listener agent-hub talks to over the cross-cluster ingress
# (asterisk.velents.ai/control/*, /healthz). Today it is a stub: it
# answers /healthz, validates the bearer token, and returns an empty
# trunks list so the agent-hub trunks page renders without a 502. The
# remaining /control/* endpoints (SIP trunk CRUD, calls, dispositions,
# etc.) are wired up to ARI/AMI in follow-ups.
#
# Stdlib only (no pip): the runtime image installs python3-minimal but
# does not pip install anything.

import json
import os
import sys
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("CONTROL_API_PORT", "8092"))
SECRET = os.environ.get("CONTROL_API_SECRET", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="control-api %(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("control-api")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer_ok(self):
        if not SECRET:
            return False
        auth = self.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return False
        return auth[7:].strip() == SECRET

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def _dispatch(self, method):
        path = self.path.split("?", 1)[0]

        if path == "/healthz":
            self._send_json(HTTPStatus.OK, {"ok": True, "service": "call-engine-stub"})
            return

        if not path.startswith("/control/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found", "path": path})
            return

        if not SECRET:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "CONTROL_API_SECRET not set in call-engine env"},
            )
            return

        if not self._bearer_ok():
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid bearer"})
            return

        # Stub responses keep agent-hub pages rendering with empty data
        # instead of bubbling a 502. Replace these with real ARI/AMI
        # calls as we implement each surface.
        if method == "GET" and path == "/control/sip/trunks":
            self._send_json(HTTPStatus.OK, {"trunks": []})
            return

        self._send_json(
            HTTPStatus.NOT_IMPLEMENTED,
            {"error": "not implemented in call-engine stub", "method": method, "path": path},
        )


def main():
    if not SECRET:
        log.warning(
            "CONTROL_API_SECRET is not set; /control/* will return 503 until DevOps wires the secret."
        )
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("listening on 0.0.0.0:%d (secret %s)", PORT, "set" if SECRET else "MISSING")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down on SIGINT")
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
