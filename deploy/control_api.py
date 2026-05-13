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
import shutil
import signal
import socket
import subprocess
import sys
import threading
import logging
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# psycopg2 is optional. With DATABASE_URL set, every trunk write also
# upserts the four PJSIP realtime rows (ps_endpoints / ps_aors /
# ps_auths / ps_registrations) Asterisk reads over ODBC, so the trunk
# REGISTERs and carries calls. Without it (or without DATABASE_URL),
# the API still works in memory-only mode for UI demos.
try:
    import psycopg2
    import psycopg2.extras
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False

PORT = int(os.environ.get("CONTROL_API_PORT", "8092"))
SECRET = os.environ.get("CONTROL_API_SECRET", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
ASTERISK_BIN = os.environ.get("ASTERISK_BIN", "asterisk")
DEFAULT_TRANSPORT = os.environ.get("PJSIP_TRANSPORT_NAME", "transport-udp")
DEFAULT_INBOUND_CONTEXT = os.environ.get("TRUNK_INBOUND_CONTEXT", "from-trunk")
DEFAULT_CODEC_ALLOW = os.environ.get("TRUNK_DEFAULT_ALLOW", "ulaw,alaw")
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


# ── trunk validation + projection ────────────────────────

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


# ── PJSIP realtime writer ────────────────────────────
#
# These four tables are what Asterisk reads via sorcery_realtime +
# extconfig (configs/samples/sorcery_realtime_agents.conf.sample,
# extconfig_realtime_agents.conf.sample). When we INSERT a row into
# ps_registrations and run `module reload res_pjsip.so`, Asterisk
# sends a SIP REGISTER to the upstream provider — that's how a trunk
# you create in the UI ends up actually carrying calls.
#
# Schema is the standard Asterisk-22 contrib shape. If a deployment
# has columns missing, the IntegrityError will surface as 502 on the
# API and the Postgres error in the pod logs — better than silently
# writing nothing.

class _DbError(RuntimeError):
    pass


def _db_enabled():
    return HAS_PSYCOPG2 and bool(DATABASE_URL)


def _db_conn():
    return psycopg2.connect(DATABASE_URL)


def _auth_id_for(trunk_id):
    return f"{trunk_id}-auth"


def _pick_transport(server_uri, explicit=None):
    """Resolve the static PJSIP transport object name for a trunk.

    The trunk form's `transport` field (if present) takes precedence and
    arrives as one of the short names in _VALID_TRANSPORTS ("udp", "tcp",
    "tls"), which we map to the static transport ids declared in
    pjsip_transport_tls.conf / pjsip_wss_agents.conf.

    When no transport is supplied, auto-pick: serverUri using scheme
    "sips:" or port 5061 -> transport-tls, otherwise DEFAULT_TRANSPORT.
    Carriers on :5061 are TLS-only in practice; sending UDP there gets
    silently dropped and the AOR contact stays Unavail with nan RTT.
    """
    if explicit:
        return f"transport-{explicit}"
    s = (server_uri or "").strip()
    if s.startswith("sips:"):
        return "transport-tls"
    rest = s.split("sip:", 1)[1] if s.startswith("sip:") else s
    if "@" in rest:
        rest = rest.split("@", 1)[1]
    hostport = rest.split(";", 1)[0].split("/", 1)[0]
    if ":" in hostport:
        try:
            if int(hostport.rsplit(":", 1)[1]) == 5061:
                return "transport-tls"
        except ValueError:
            pass
    return DEFAULT_TRANSPORT


def _rewrite_uri_to_ip(uri):
    """Resolve the hostname in a sip(s): URI to an A-record IP literal.

    Asterisk's bundled pjlib DNS resolver fails with EDNSNOANSWERREC on
    carrier hostnames that have no NAPTR/SRV records (typical for
    wholesale SIP providers like innocalls), which makes qualify probes
    and REGISTERs never leave the pod. The OS resolver here handles
    those names fine, so we resolve once at upsert-time and write the
    resulting IP into ps_aors.contact and ps_registrations.server_uri.
    client_uri keeps the original hostname so the SIP From: header
    still carries the carrier's expected domain.

    Falls back to the original URI if resolution fails (e.g. transient
    DNS error at upsert time) — better to write something Asterisk can
    try than to fail the save outright.
    """
    if not uri:
        return uri
    try:
        scheme, rest = uri.split(":", 1)
    except ValueError:
        return uri
    if scheme not in ("sip", "sips"):
        return uri
    rest, sep, params = rest.partition(";")
    if "@" in rest:
        userpart, _, hostpart = rest.partition("@")
        prefix = f"{scheme}:{userpart}@"
    else:
        hostpart = rest
        prefix = f"{scheme}:"
    host, _, port = hostpart.partition(":")
    if not host:
        return uri
    # If already an IP literal, leave it alone.
    try:
        socket.inet_aton(host)
        return uri
    except OSError:
        pass
    try:
        ip = socket.gethostbyname(host)
    except OSError as exc:
        log.warning("DNS resolve %s failed: %s; writing hostname as-is", host, exc)
        return uri
    out = f"{prefix}{ip}"
    if port:
        out += f":{port}"
    if sep:
        out += f";{params}"
    return out


def _pjsip_upsert(row, password):
    """Write the four ps_* rows so Asterisk picks the trunk up on reload."""
    if not _db_enabled():
        return
    has_auth = bool(password) and bool(row.get("username"))
    auth_id = _auth_id_for(row["id"]) if has_auth else None
    server_uri = row["server_uri"]
    # Pre-resolve the carrier hostname so pjlib's DNS resolver (which
    # mis-handles NAPTR/SRV NODATA) isn't in the qualify/REGISTER path.
    # client_uri keeps the hostname so the SIP From: header still
    # advertises the carrier's domain.
    target_uri = _rewrite_uri_to_ip(server_uri)
    transport = _pick_transport(server_uri, row.get("transport"))
    context = row.get("context") or DEFAULT_INBOUND_CONTEXT
    allow = row.get("allow") or DEFAULT_CODEC_ALLOW
    expiration = row.get("expiration") or 3600
    client_uri = row.get("client_uri") or (
        f"sip:{row['username']}@{row.get('from_domain') or _server_uri_host(server_uri)}"
        if row.get("username") else None
    )

    try:
        with _db_conn() as conn, conn.cursor() as cur:
            # 1) AOR — describes the contact Asterisk dials/sends INVITEs to.
            cur.execute("""
                INSERT INTO ps_aors (id, max_contacts, qualify_frequency, contact)
                VALUES (%s, 1, 60, %s)
                ON CONFLICT (id) DO UPDATE SET
                    contact = EXCLUDED.contact,
                    qualify_frequency = EXCLUDED.qualify_frequency
            """, (row["id"], target_uri))

            # 2) AUTH — only when the trunk has credentials. realm is
            #    left NULL so Asterisk's outbound digest authenticator
            #    matches whatever realm the carrier sends in its 401
            #    challenge (carriers' challenge realm rarely matches
            #    the hostname users type in the form — e.g. innocalls
            #    challenges with realm `sip.innocalls.net` even though
            #    the trunk talks to `cu622.sip.innocalls.net`).
            if has_auth:
                cur.execute("""
                    INSERT INTO ps_auths (id, auth_type, username, password, realm)
                    VALUES (%s, 'userpass', %s, %s, NULL)
                    ON CONFLICT (id) DO UPDATE SET
                        username = EXCLUDED.username,
                        password = EXCLUDED.password,
                        realm    = EXCLUDED.realm
                """, (auth_id, row["username"], password))
            else:
                cur.execute("DELETE FROM ps_auths WHERE id = %s", (_auth_id_for(row["id"]),))

            # 3) ENDPOINT — what Asterisk uses for incoming AND outgoing.
            cur.execute("""
                INSERT INTO ps_endpoints
                    (id, transport, context, aors, auth, allow, dtmf_mode,
                     identify_by, disallow, outbound_auth)
                VALUES (%s, %s, %s, %s, %s, %s, 'rfc4733', 'username,auth_username', 'all', %s)
                ON CONFLICT (id) DO UPDATE SET
                    transport     = EXCLUDED.transport,
                    context       = EXCLUDED.context,
                    aors          = EXCLUDED.aors,
                    auth          = EXCLUDED.auth,
                    allow         = EXCLUDED.allow,
                    outbound_auth = EXCLUDED.outbound_auth
            """, (row["id"], transport, context, row["id"], auth_id, allow, auth_id))

            # 4) REGISTRATION — only when enabled. Disabling the trunk
            #    drops the row so Asterisk stops re-registering.
            if row.get("enabled", True) and has_auth and client_uri:
                cur.execute("""
                    INSERT INTO ps_registrations
                        (id, transport, server_uri, client_uri, expiration,
                         retry_interval, outbound_auth)
                    VALUES (%s, %s, %s, %s, %s, 60, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        transport      = EXCLUDED.transport,
                        server_uri     = EXCLUDED.server_uri,
                        client_uri     = EXCLUDED.client_uri,
                        expiration     = EXCLUDED.expiration,
                        outbound_auth  = EXCLUDED.outbound_auth
                """, (row["id"], transport, target_uri, client_uri, expiration, auth_id))
            else:
                cur.execute("DELETE FROM ps_registrations WHERE id = %s", (row["id"],))
    except psycopg2.Error as exc:
        raise _DbError(f"pjsip realtime write failed: {exc}") from exc


def _pjsip_delete(trunk_id):
    if not _db_enabled():
        return
    try:
        with _db_conn() as conn, conn.cursor() as cur:
            # Order: drop registrations first (foreign-key-ish), then
            # endpoint, then auth, then aor.
            cur.execute("DELETE FROM ps_registrations WHERE id = %s", (trunk_id,))
            cur.execute("DELETE FROM ps_endpoints    WHERE id = %s", (trunk_id,))
            cur.execute("DELETE FROM ps_auths        WHERE id = %s", (_auth_id_for(trunk_id),))
            cur.execute("DELETE FROM ps_aors         WHERE id = %s", (trunk_id,))
    except psycopg2.Error as exc:
        raise _DbError(f"pjsip realtime delete failed: {exc}") from exc


def _server_uri_host(server_uri):
    """Extract host from sip:user@host:port or sip:host."""
    s = server_uri.replace("sips:", "").replace("sip:", "")
    if "@" in s:
        s = s.split("@", 1)[1]
    return s.split(":", 1)[0].split(";", 1)[0]


# ── route table ────────────────────────────────────

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

    # ── routing ─────────────────────────────────────

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

    # ── body helpers ──────────────────────────────────

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

    # ── /control/sip/trunks handlers ───────────────────────

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
        self._upsert_common(url_id=None)

    def upsert_trunk_by_id(self, trunk_id):
        self._upsert_common(url_id=trunk_id)

    def _upsert_common(self, url_id):
        body = self._read_json_body()
        if body is None:
            return  # response already sent
        if url_id is not None:
            body_id = body.get("id")
            if body_id is None or body_id == "":
                body["id"] = url_id
            elif str(body_id) != url_id:
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
            password = normalized.pop("_password", None)
            # Persist to Postgres BEFORE memory so a DB failure doesn't
            # leave the API claiming success while Asterisk has nothing.
            try:
                _pjsip_upsert({**normalized, "username": normalized.get("username")}, password)
            except _DbError as exc:
                log.error("trunk %s pjsip write failed: %s", normalized["id"], exc)
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                return
            # Stash password back so subsequent updates that omit it
            # can still REGISTER from the cached value.
            normalized["_password"] = password
            _trunks[normalized["id"]] = normalized

        self._send_json(HTTPStatus.OK, _to_row(normalized))

    def delete_trunk(self, trunk_id):
        with _lock:
            removed = _trunks.pop(trunk_id, None)
        # Delete from Postgres regardless of whether memory had it
        # (the rows could have been seeded out of band, and we want
        # idempotent deletes).
        try:
            _pjsip_delete(trunk_id)
        except _DbError as exc:
            log.error("trunk %s pjsip delete failed: %s", trunk_id, exc)
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return
        if removed is None and not _db_enabled():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "trunk not found"})
            return
        self._send_204()

    # ── /control/asterisk/reload ─────────────────────────
    #
    # Real reload via the asterisk CLI socket (which lives in
    # /var/run/asterisk and is owned by uid:gid 1000 — the same the
    # sidecar runs as inside the pod). Without this, new
    # ps_registrations rows aren't picked up and the trunk doesn't
    # send a SIP REGISTER. agent-hub's POST handler calls this right
    # after upsertTrunk.

    def reload_asterisk(self):
        body = self._read_json_body()
        if body is None:
            return
        module = str(body.get("moduleName") or body.get("module") or "").strip()
        if not module:
            module = "res_pjsip.so"
        # Whitelist: only reload PJSIP-related modules from this API
        # (don't let a typo'd request reload core, app_*, etc.).
        if module not in {"res_pjsip.so", "res_pjsip_endpoint_identifier_ip.so"}:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY,
                            {"error": f"module not allowed: {module}"})
            return

        if shutil.which(ASTERISK_BIN) is None:
            # Sidecar may run in a no-asterisk dev container; surface
            # honestly rather than pretending we reloaded.
            self._send_json(HTTPStatus.OK, {
                "reloaded": False,
                "stub": True,
                "module": module,
                "reason": f"{ASTERISK_BIN} binary not on PATH",
            })
            return

        try:
            proc = subprocess.run(
                [ASTERISK_BIN, "-rx", f"module reload {module}"],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except subprocess.TimeoutExpired:
            self._send_json(HTTPStatus.GATEWAY_TIMEOUT,
                            {"error": "asterisk -rx timed out after 10s"})
            return
        except OSError as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY,
                            {"error": f"asterisk exec failed: {exc}"})
            return

        ok = proc.returncode == 0
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        log.info("asterisk reload %s rc=%d stdout=%r stderr=%r",
                 module, proc.returncode, out[:200], err[:200])
        self._send_json(
            HTTPStatus.OK if ok else HTTPStatus.BAD_GATEWAY,
            {
                "reloaded": ok,
                "stub": False,
                "module": module,
                "rc": proc.returncode,
                "stdout": out,
                "stderr": err,
            },
        )


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

    if DATABASE_URL and not HAS_PSYCOPG2:
        log.warning(
            "DATABASE_URL is set but psycopg2 is not installed — falling back to "
            "in-memory mode. Trunks will NOT register or carry calls. Rebuild the "
            "image with python3-psycopg2."
        )
    db_mode = "postgres" if _db_enabled() else "memory-only"
    log.info(
        "listening on 0.0.0.0:%d (secret %s, trunk store=%s) — ready for /healthz and /control/*",
        PORT,
        "set" if SECRET else "MISSING",
        db_mode,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        log.info("control-api stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
