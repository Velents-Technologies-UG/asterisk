#!/usr/bin/env python3
# Minimal control API that runs alongside Asterisk in the same pod.
#
# Stub. Answers /healthz, enforces bearer auth, and now provides
# in-memory CRUD for SIP trunks under /control/sip/trunks/* so the
# agent-hub trunks page works end-to-end. The contract matches what
# agent-hub's `lib/cx/trunks.ts::upsertTrunk` already sends (camelCase
# input, snake_case TrunkRow output, `items` envelope on list).
#
# Storage is intentionally process-local — a pod restart wipes it.
# When the call-engine team takes over this surface, replace the
# in-memory dict with PJSIP realtime writes (ps_endpoints / ps_aors /
# ps_auths) and keep the same wire contract so agent-hub doesn't need
# to change.
#
# Stdlib only (no pip): the runtime image installs python3 but does
# not pip install anything.

import json
import os
import re
import signal
import sys
import threading
import logging
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("CONTROL_API_PORT", "8092"))
SECRET = os.environ.get("CONTROL_API_SECRET", "").strip()
MAX_BODY_BYTES = 64 * 1024  # 64 KiB — way more than a trunk row needs.

# In-memory trunk store. Key = trunk id; value = the canonical
# snake_case TrunkRow that we hand back on read, plus a private
# "_password" field that is NEVER serialized.
_trunks: dict[str, dict] = {}
_lock = threading.RLock()

# Mirrors agent-hub's SAFE_ID at lib/cx/trunks.ts. Reject early so a
# bad id doesn't propagate downstream where someone might forget to
# escape it (hello, ps_endpoints PRIMARY KEY).
_SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,60}$")
_VALID_TRANSPORTS = {"udp", "tcp", "tls"}

# stderr so logs are not buffered behind the supervisor's pipe in
# entrypoint.sh — `kubectl logs` then shows startup errors in real time.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("control-api")


# ── trunk validation + projection ────────────────────────────

class _ValidationError(ValueError):
    pass


# Camel-cased input keys (what agent-hub's upsertTrunk sends) → the
# snake_case TrunkRow keys lib/cx/trunks.ts consumes. Optional fields
# missing from the payload land in the row as `null`. Reserved keys
# (outbound_auth, identify_by, allow) are reserved for the real
# call-engine implementation that writes PJSIP realtime; the stub
# always emits them as null so the consumer's TS shape is satisfied.
_INPUT_TO_ROW_NULLABLE = {
    "provider":     "provider",
    "region":       "region",
    "description":  "description",
    "transport":    "transport",
    "context":      "context",
    "clientUri":    "client_uri",
    "fromUser":     "from_user",
    "fromDomain":   "from_domain",
    "expiration":   "expiration",
}


def _validate_trunk_input(body):
    """Return the canonical stored shape, or raise _ValidationError."""
    for required in ("id", "displayName", "serverUri", "username"):
        v = body.get(required)
        if v is None or (isinstance(v, str) and not v.strip()):
            raise _ValidationError(f"{required} required")

    trunk_id = str(body["id"]).strip()
    if not _SAFE_ID.match(trunk_id):
        raise _ValidationError("id must be 1-60 chars, alphanumerics + _ -")

    transport = body.get("transport")
    if transport is not None:
        if transport not in _VALID_TRANSPORTS:
            raise _ValidationError(
                "transport must be one of: " + ", ".join(sorted(_VALID_TRANSPORTS))
            )

    channel_limit = body.get("channelLimit", 50)
    try:
        channel_limit = int(channel_limit)
    except (TypeError, ValueError):
        raise _ValidationError("channelLimit must be an integer")
    if not 1 <= channel_limit <= 1000:
        raise _ValidationError("channelLimit must be between 1 and 1000")

    expiration = body.get("expiration")
    if expiration is not None:
        try:
            expiration = int(expiration)
        except (TypeError, ValueError):
            raise _ValidationError("expiration must be an integer")
        if not 60 <= expiration <= 86400:
            raise _ValidationError("expiration must be between 60 and 86400 seconds")

    enabled = body.get("enabled", True)
    if not isinstance(enabled, bool):
        raise _ValidationError("enabled must be a boolean")

    row = {
        "id":            trunk_id,
        "display_name":  str(body["displayName"]),
        "server_uri":    str(body["serverUri"]),
        "username":      str(body["username"]),
        "channel_limit": channel_limit,
        "enabled":       enabled,
        "outbound_auth": None,    # reserved
        "identify_by":   None,    # reserved
        "allow":         None,    # reserved
    }
    for in_key, out_key in _INPUT_TO_ROW_NULLABLE.items():
        v = body.get(in_key)
        row[out_key] = None if v is None or v == "" else (
            int(v) if out_key in {"expiration"} else v
        )

    # Password is stored under a private key so _to_row() can't
    # accidentally serialize it.
    pw = body.get("password")
    row["_password"] = None if pw is None or pw == "" else str(pw)

    return row


def _to_row(stored):
    """Public projection — strip private fields like _password."""
    return {k: v for k, v in stored.items() if not k.startswith("_")}


# ── route table ──────────────────────────────────────────────

_ROUTES = [
    ("GET",    re.compile(r"^/control/sip/trunks/?$"),       "list_trunks"),
    ("POST",   re.compile(r"^/control/sip/trunks/?$"),       "create_trunk"),
    ("GET",    re.compile(r"^/control/sip/trunks/([^/]+)$"), "show_trunk"),
    # POST to /control/sip/trunks/{id} also upserts (some callers
    # PUT, some POST). Both land in the same handler.
    ("POST",   re.compile(r"^/control/sip/trunks/([^/]+)$"), "upsert_trunk_by_id"),
    ("PUT",    re.compile(r"^/control/sip/trunks/([^/]+)$"), "upsert_trunk_by_id"),
    ("DELETE", re.compile(r"^/control/sip/trunks/([^/]+)$"), "delete_trunk"),
    ("POST",   re.compile(r"^/control/asterisk/reload/?$"),  "reload_asterisk"),
]


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

    # ── routing ──────────────────────────────────────────────

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

        for verb, pattern, handler_name in _ROUTES:
            if verb != method:
                continue
            m = pattern.match(path)
            if not m:
                continue
            getattr(self, handler_name)(*m.groups())
            return

        self._send_json(
            HTTPStatus.NOT_IMPLEMENTED,
            {"error": "not implemented in call-engine stub", "method": method, "path": path},
        )

    # ── body helpers ─────────────────────────────────────────

    def _read_json_body(self):
        """Return parsed JSON dict, or None if a response was already sent."""
        ctype = self.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if ctype and ctype != "application/json":
            self._send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                            {"error": "content-type must be application/json"})
            return None

        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid content-length"})
            return None
        if length > MAX_BODY_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                            {"error": "body too large"})
            return None
        if length == 0:
            return {}

        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
            return None
        if not isinstance(data, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "body must be a JSON object"})
            return None
        return data

    def _send_204(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("content-length", "0")
        self.end_headers()

    # ── /control/sip/trunks handlers ─────────────────────────

    def list_trunks(self):
        with _lock:
            items = [_to_row(t) for t in _trunks.values()]
        # Stable order so the list page doesn't shuffle on every refresh.
        items.sort(key=lambda r: r["id"])
        self._send_json(HTTPStatus.OK, {"items": items})

    def show_trunk(self, trunk_id):
        with _lock:
            t = _trunks.get(trunk_id)
        if t is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "trunk not found"})
            return
        self._send_json(HTTPStatus.OK, _to_row(t))

    def create_trunk(self):
        body = self._read_json_body()
        if body is None:
            return  # response already sent
        try:
            normalized = _validate_trunk_input(body)
        except _ValidationError as exc:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
            return

        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with _lock:
            existing = _trunks.get(normalized["id"])
            normalized["created_at"] = (existing or {}).get("created_at", now)
            normalized["updated_at"] = now
            _trunks[normalized["id"]] = normalized

        self._send_json(HTTPStatus.OK, _to_row(normalized))

    def upsert_trunk_by_id(self, trunk_id):
        body = self._read_json_body()
        if body is None:
            return
        # If the body omits id, take it from the URL. If both are
        # present and disagree, reject — silently rewriting is worse.
        body_id = body.get("id")
        if body_id is None or body_id == "":
            body["id"] = trunk_id
        elif str(body_id) != trunk_id:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY,
                            {"error": "id in URL and body must match"})
            return
        try:
            normalized = _validate_trunk_input(body)
        except _ValidationError as exc:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
            return

        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with _lock:
            existing = _trunks.get(normalized["id"])
            normalized["created_at"] = (existing or {}).get("created_at", now)
            normalized["updated_at"] = now
            _trunks[normalized["id"]] = normalized
        self._send_json(HTTPStatus.OK, _to_row(normalized))

    def delete_trunk(self, trunk_id):
        with _lock:
            removed = _trunks.pop(trunk_id, None)
        if removed is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "trunk not found"})
            return
        self._send_204()

    # ── /control/asterisk/reload no-op ──────────────────────
    #
    # agent-hub's POST handler fires this right after upsertTrunk and
    # swallows failures, so a 501 here is harmless — but it pollutes
    # the network tab. Honest stub: 200 with reloaded:false. The real
    # implementation will exec asterisk -rx "module reload <name>" via
    # AMI / ARI and flip the flag.

    def reload_asterisk(self):
        body = self._read_json_body()
        if body is None:
            return
        module = str(body.get("moduleName") or body.get("module") or "").strip()
        self._send_json(HTTPStatus.OK, {
            "reloaded": False,
            "stub": True,
            "module": module or None,
        })


def main():
    if not SECRET:
        log.warning(
            "CONTROL_API_SECRET is not set; /control/* will return 503 until DevOps wires the secret."
        )

    # Bind explicitly before declaring readiness. If this raises
    # (port in use, permissions, address family), the exception
    # surfaces in stderr and the supervisor in entrypoint.sh restarts
    # us with backoff — which is preferable to silently dying.
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as exc:
        log.error("failed to bind 0.0.0.0:%d: %s", PORT, exc)
        return 1

    # k8s sends SIGTERM on pod stop; default action would kill us
    # without closing the socket cleanly, leaving the port in TIME_WAIT
    # for the next pod. shutdown() exits serve_forever() in the main
    # thread, then we close the socket in the finally below.
    def _graceful(signum, _frame):
        log.info("received signal %d; shutting down", signum)
        # shutdown() blocks if called from the same thread that's in
        # serve_forever(). Spawn a tiny thread to do the call.
        import threading
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)

    log.info(
        "listening on 0.0.0.0:%d (secret %s) — ready for /healthz and /control/*",
        PORT,
        "set" if SECRET else "MISSING",
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        log.info("control-api stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
