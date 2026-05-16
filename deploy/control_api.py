#!/usr/bin/env python3
# Minimal control API that runs alongside Asterisk in the same pod.

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import logging
import urllib.parse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import psycopg2
    import psycopg2.extras
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False

try:
    import redis as redis_lib
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False

try:
    import sip_store
    HAS_SIP_STORE = True
except ImportError as _exc:
    HAS_SIP_STORE = False
    _sip_store_import_error = str(_exc)

PORT = int(os.environ.get("CONTROL_API_PORT", "8092"))
SECRET = os.environ.get("CONTROL_API_SECRET", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DATABASE_SSLMODE = os.environ.get("DATABASE_SSLMODE", "disable").strip()
REDIS_URL = os.environ.get("REDIS_URL", "").strip()
ASTERISK_BIN = os.environ.get("ASTERISK_BIN", "asterisk")
DEFAULT_TRANSPORT = os.environ.get("PJSIP_TRANSPORT_NAME", "transport-udp")
DEFAULT_INBOUND_CONTEXT = os.environ.get("TRUNK_INBOUND_CONTEXT", "from-trunk")
DEFAULT_CODEC_ALLOW = os.environ.get("TRUNK_DEFAULT_ALLOW", "ulaw,alaw")
CLICK_TO_DIAL_TIMEOUT = int(os.environ.get("CLICK_TO_DIAL_TIMEOUT", "30"))
STATUS_FEEDER_INTERVAL = int(os.environ.get("STATUS_FEEDER_INTERVAL", "5"))
STATUS_FEEDER_KEY = os.environ.get("STATUS_FEEDER_KEY", "cx:trunks:status")
STATUS_FEEDER_AGENTS_KEY = os.environ.get("STATUS_FEEDER_AGENTS_KEY", "cx:agents:sip-status")
MAX_BODY_BYTES = 64 * 1024

BEHIND_NAT = bool(os.environ.get("ASTERISK_EXTERNAL_IP", "").strip()) or \
    os.environ.get("ASTERISK_BEHIND_NAT", "").strip().lower() in ("1", "yes", "true")

_trunks: dict[str, dict] = {}
_lock = threading.RLock()

_SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,60}$")
_VALID_TRANSPORTS = {"udp", "tcp", "tls"}

_DEST_RE = re.compile(r"^[0-9+*#]{1,64}$")
_EXT_RE  = re.compile(r"^[a-zA-Z0-9_+*#-]{1,40}$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("control-api")


class _ValidationError(ValueError):
    pass


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


def _valid_ip_literal(value):
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not s:
        return False
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, s)
            return True
        except (OSError, ValueError):
            continue
    return False


def _validate_trunk_input(body):
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

    register_enabled = body.get("registerEnabled", True)
    if not isinstance(register_enabled, bool):
        raise _ValidationError("registerEnabled must be a boolean")

    carrier_ip = body.get("carrierIp")
    if carrier_ip is not None and carrier_ip != "":
        if not _valid_ip_literal(carrier_ip):
            raise _ValidationError("carrierIp must be a valid IPv4 or IPv6 literal")
        carrier_ip = str(carrier_ip).strip()
    else:
        carrier_ip = None

    if not register_enabled and not carrier_ip:
        raise _ValidationError("carrierIp is required when registerEnabled is false")

    row = {
        "id":               trunk_id,
        "display_name":     str(body["displayName"]),
        "server_uri":       str(body["serverUri"]),
        "username":         str(body["username"]),
        "channel_limit":    channel_limit,
        "enabled":          enabled,
        "register_enabled": register_enabled,
        "carrier_ip":       carrier_ip,
        "outbound_auth":    None,
        "identify_by":      None,
        "allow":            None,
    }
    for in_key, out_key in _INPUT_TO_ROW_NULLABLE.items():
        v = body.get(in_key)
        row[out_key] = None if v is None or v == "" else (
            int(v) if out_key in {"expiration"} else v
        )

    pw = body.get("password")
    row["_password"] = None if pw is None or pw == "" else str(pw)

    return row


def _to_row(stored):
    return {k: v for k, v in stored.items() if not k.startswith("_")}


class _DbError(RuntimeError):
    pass


def _db_enabled():
    return HAS_PSYCOPG2 and bool(DATABASE_URL)


def _db_conn():
    """Open a psycopg2 connection from DATABASE_URL."""
    u = urllib.parse.urlparse(DATABASE_URL)
    return psycopg2.connect(
        host=u.hostname,
        port=u.port,
        dbname=(u.path or "").lstrip("/") or None,
        user=urllib.parse.unquote(u.username) if u.username else None,
        password=urllib.parse.unquote(u.password) if u.password else None,
        sslmode=DATABASE_SSLMODE,
    )


def _auth_id_for(trunk_id):
    return f"{trunk_id}-auth"


def _identify_id_for(trunk_id):
    return f"{trunk_id}-identify"


def _pick_transport(server_uri, explicit=None):
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


def _build_client_uri(username, from_domain, server_uri):
    if username:
        domain = from_domain or _server_uri_host(server_uri)
        return f"sip:{username}@{domain}"
    return None


def _pjsip_upsert(row, password):
    """Write the four ps_* rows so Asterisk picks the trunk up on reload."""
    if not _db_enabled():
        return
    has_auth = bool(password) and bool(row.get("username"))
    auth_id = _auth_id_for(row["id"]) if has_auth else None
    identify_id = _identify_id_for(row["id"])
    server_uri = row["server_uri"]
    target_uri = _rewrite_uri_to_ip(server_uri)
    transport = _pick_transport(server_uri, row.get("transport"))
    context = row.get("context") or DEFAULT_INBOUND_CONTEXT
    allow = row.get("allow") or DEFAULT_CODEC_ALLOW
    expiration = row.get("expiration") or 3600
    client_uri = _build_client_uri(
        row.get("username"), row.get("from_domain"), server_uri,
    ) or row.get("client_uri") or None
    register_enabled = bool(row.get("register_enabled", True))
    carrier_ip = row.get("carrier_ip")
    identify_by = "ip,username,auth_username" if not register_enabled else "username,auth_username"

    # From identity for outbound INVITEs. Without these PJSIP defaults to
    # `From: "Anonymous" <sip:anonymous@anonymous.invalid>` which most
    # carriers (innocalls included) reject with 403 Forbidden because
    # they expect a billing-account caller-ID. Fall back to the SIP auth
    # username if no explicit from_user was configured — that's at least
    # *some* identity rather than anonymous.
    from_user_value = row.get("from_user") or row.get("username") or ""
    from_domain_value = row.get("from_domain") or _server_uri_host(server_uri) or ""
    # callerid is "Name" <number> shape; some carriers also check P-Asserted-Identity
    # which Asterisk derives from this field when send_pai is enabled.
    callerid_value = f"<{from_user_value}>" if from_user_value else ""

    try:
        with _db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO ps_aors (id, max_contacts, qualify_frequency, contact)
                VALUES (%s, 1, 60, %s)
                ON CONFLICT (id) DO UPDATE SET
                    contact = EXCLUDED.contact,
                    qualify_frequency = EXCLUDED.qualify_frequency
            """, (row["id"], target_uri))

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

            if BEHIND_NAT:
                cur.execute("""
                    INSERT INTO ps_endpoints
                        (id, transport, context, aors, auth, allow, dtmf_mode,
                         identify_by, disallow, outbound_auth,
                         from_user, from_domain, callerid,
                         rtp_symmetric, force_rport, direct_media)
                    VALUES (%s, %s, %s, %s, %s, %s, 'rfc4733',
                            %s, 'all', %s,
                            %s, %s, %s,
                            'yes', 'yes', 'no')
                    ON CONFLICT (id) DO UPDATE SET
                        transport     = EXCLUDED.transport,
                        context       = EXCLUDED.context,
                        aors          = EXCLUDED.aors,
                        auth          = EXCLUDED.auth,
                        allow         = EXCLUDED.allow,
                        identify_by   = EXCLUDED.identify_by,
                        outbound_auth = EXCLUDED.outbound_auth,
                        from_user     = EXCLUDED.from_user,
                        from_domain   = EXCLUDED.from_domain,
                        callerid      = EXCLUDED.callerid,
                        rtp_symmetric = EXCLUDED.rtp_symmetric,
                        force_rport   = EXCLUDED.force_rport,
                        direct_media  = EXCLUDED.direct_media
                """, (row["id"], transport, context, row["id"], auth_id, allow,
                      identify_by, auth_id,
                      from_user_value or None, from_domain_value or None,
                      callerid_value or None))
            else:
                cur.execute("""
                    INSERT INTO ps_endpoints
                        (id, transport, context, aors, auth, allow, dtmf_mode,
                         identify_by, disallow, outbound_auth,
                         from_user, from_domain, callerid)
                    VALUES (%s, %s, %s, %s, %s, %s, 'rfc4733', %s, 'all', %s,
                            %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        transport     = EXCLUDED.transport,
                        context       = EXCLUDED.context,
                        aors          = EXCLUDED.aors,
                        auth          = EXCLUDED.auth,
                        allow         = EXCLUDED.allow,
                        identify_by   = EXCLUDED.identify_by,
                        outbound_auth = EXCLUDED.outbound_auth,
                        from_user     = EXCLUDED.from_user,
                        from_domain   = EXCLUDED.from_domain,
                        callerid      = EXCLUDED.callerid
                """, (row["id"], transport, context, row["id"], auth_id, allow,
                      identify_by, auth_id,
                      from_user_value or None, from_domain_value or None,
                      callerid_value or None))

            if register_enabled:
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
                    """, (row["id"], transport, target_uri, client_uri,
                          expiration, auth_id))
                else:
                    cur.execute("DELETE FROM ps_registrations WHERE id = %s",
                                (row["id"],))
                cur.execute("DELETE FROM ps_identify WHERE id = %s",
                            (identify_id,))
            else:
                cur.execute("DELETE FROM ps_registrations WHERE id = %s",
                            (row["id"],))
                cur.execute("""
                    INSERT INTO ps_identify (id, endpoint, "match")
                    VALUES (%s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        endpoint = EXCLUDED.endpoint,
                        "match"  = EXCLUDED."match"
                """, (identify_id, row["id"], f"{carrier_ip}/32"))
    except psycopg2.Error as exc:
        raise _DbError(f"pjsip realtime write failed: {exc}") from exc


def _pjsip_delete(trunk_id):
    if not _db_enabled():
        return
    try:
        with _db_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM ps_registrations WHERE id = %s", (trunk_id,))
            cur.execute("DELETE FROM ps_identify      WHERE id = %s", (_identify_id_for(trunk_id),))
            cur.execute("DELETE FROM ps_endpoints     WHERE id = %s", (trunk_id,))
            cur.execute("DELETE FROM ps_auths         WHERE id = %s", (_auth_id_for(trunk_id),))
            cur.execute("DELETE FROM ps_aors          WHERE id = %s", (trunk_id,))
    except psycopg2.Error as exc:
        raise _DbError(f"pjsip realtime delete failed: {exc}") from exc


def _server_uri_host(server_uri):
    s = server_uri.replace("sips:", "").replace("sip:", "")
    if "@" in s:
        s = s.split("@", 1)[1]
    return s.split(":", 1)[0].split(";", 1)[0]


_CONTACT_STATE_PREFIXES = (
    "avail", "unavail", "nonqual", "removed", "created", "rejected", "unknown",
)


def _parse_pjsip_endpoints(output):
    for line in output.splitlines():
        s = line.strip()
        if not s.startswith("Endpoint:"):
            continue
        if "<Endpoint" in s:
            continue
        rest = s[len("Endpoint:"):].strip()
        parts = re.split(r"\s{2,}", rest)
        if len(parts) < 2:
            continue
        ep_id = parts[0].strip()
        state = parts[1].strip()
        if ep_id:
            yield ep_id, state


def _parse_pjsip_identifies(output):
    for line in output.splitlines():
        s = line.strip()
        if not s.startswith("Identify:"):
            continue
        if "<Identify" in s:
            continue
        rest = s[len("Identify:"):].strip()
        if "/" not in rest:
            continue
        endpoint_part = rest.rsplit("/", 1)[1].strip()
        endpoint_id = re.split(r"\s+", endpoint_part, maxsplit=1)[0].strip()
        if endpoint_id:
            yield endpoint_id


def _parse_pjsip_aors(output):
    aors = {}
    current = None
    for line in output.splitlines():
        s = line.strip()
        if s.startswith("Aor:"):
            rest = s[len("Aor:"):].strip()
            if "<Aor" in rest:
                current = None
                continue
            parts = re.split(r"\s+", rest, maxsplit=1)
            if parts and parts[0]:
                current = parts[0].strip()
                aors.setdefault(current, [])
        elif s.startswith("Contact:") and current:
            rest = s[len("Contact:"):].strip()
            if "<Aor/Contact" in rest or "<Contact" in rest:
                continue
            tokens = rest.split()
            for tok in tokens[1:]:
                tl = tok.lower()
                for prefix in _CONTACT_STATE_PREFIXES:
                    if tl == prefix or tl.startswith(prefix):
                        aors[current].append(tok)
                        break
                else:
                    continue
                break

    for aor_id, states in aors.items():
        if not states:
            yield aor_id, "offline"
            continue
        good = False
        for state in states:
            sl = state.lower()
            if "unavail" in sl or "removed" in sl or "rejected" in sl:
                continue
            if "avail" in sl or "nonqual" in sl or "created" in sl:
                good = True
                break
        yield aor_id, "online" if good else "offline"


def _state_to_status(state):
    s = (state or "").lower()
    if "not in use" in s or "in use" in s or s.startswith("avail"):
        return "online"
    if "unavailable" in s or s.startswith("unavail"):
        return "offline"
    return "unknown"


def _status_feeder_loop(stop_event):
    if not REDIS_URL:
        log.info("status feeder: REDIS_URL unset; skipping")
        return
    if not HAS_REDIS:
        log.warning(
            "status feeder: python3-redis not installed; UI badge stays 'unknown'."
        )
        return
    try:
        client = redis_lib.from_url(REDIS_URL, decode_responses=True,
                                    socket_connect_timeout=3, socket_timeout=3)
        client.ping()
    except Exception as exc:
        log.error("status feeder: cannot connect to %s: %s", REDIS_URL, exc)
        return
    log.info(
        "status feeder started: interval=%ds trunks_key=%s agents_key=%s",
        STATUS_FEEDER_INTERVAL, STATUS_FEEDER_KEY, STATUS_FEEDER_AGENTS_KEY,
    )
    while not stop_event.is_set():
        try:
            if shutil.which(ASTERISK_BIN) is None:
                stop_event.wait(STATUS_FEEDER_INTERVAL)
                continue
            ep_proc = subprocess.run(
                [ASTERISK_BIN, "-rx", "pjsip show endpoints"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            id_proc = subprocess.run(
                [ASTERISK_BIN, "-rx", "pjsip show identifies"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            aor_proc = subprocess.run(
                [ASTERISK_BIN, "-rx", "pjsip show aors"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            ip_trunk_endpoints = (
                set(_parse_pjsip_identifies(id_proc.stdout))
                if id_proc.returncode == 0 else set()
            )
            endpoint_ids = (
                {ep for ep, _ in _parse_pjsip_endpoints(ep_proc.stdout)}
                if ep_proc.returncode == 0 else set()
            )
            trunk_endpoints = endpoint_ids | ip_trunk_endpoints

            if ep_proc.returncode == 0:
                trunk_updates = {}
                for ep_id, state in _parse_pjsip_endpoints(ep_proc.stdout):
                    if ep_id in ip_trunk_endpoints:
                        trunk_updates[ep_id] = "online"
                    else:
                        trunk_updates[ep_id] = _state_to_status(state)
                if trunk_updates:
                    client.hset(STATUS_FEEDER_KEY, mapping=trunk_updates)

            if aor_proc.returncode == 0:
                agent_updates = {}
                for aor_id, status in _parse_pjsip_aors(aor_proc.stdout):
                    if aor_id in trunk_endpoints:
                        continue
                    agent_updates[aor_id] = status
                if agent_updates:
                    client.hset(STATUS_FEEDER_AGENTS_KEY, mapping=agent_updates)
        except Exception as exc:
            log.warning("status feeder iteration failed: %s", exc)
        stop_event.wait(STATUS_FEEDER_INTERVAL)


_ROUTES = [
    ("GET",    re.compile(r"^/control/sip/trunks/?$"),       "list_trunks"),
    ("POST",   re.compile(r"^/control/sip/trunks/?$"),       "create_trunk"),
    ("GET",    re.compile(r"^/control/sip/trunks/([^/]+)$"), "show_trunk"),
    ("POST",   re.compile(r"^/control/sip/trunks/([^/]+)$"), "upsert_trunk_by_id"),
    ("PUT",    re.compile(r"^/control/sip/trunks/([^/]+)$"), "upsert_trunk_by_id"),
    ("DELETE", re.compile(r"^/control/sip/trunks/([^/]+)$"), "delete_trunk"),
    ("GET",    re.compile(r"^/control/sip/providers/?$"),       "list_providers"),
    ("POST",   re.compile(r"^/control/sip/providers/?$"),       "create_provider"),
    ("GET",    re.compile(r"^/control/sip/providers/([^/]+)$"), "show_provider"),
    ("POST",   re.compile(r"^/control/sip/providers/([^/]+)$"), "upsert_provider_by_id"),
    ("PUT",    re.compile(r"^/control/sip/providers/([^/]+)$"), "upsert_provider_by_id"),
    ("DELETE", re.compile(r"^/control/sip/providers/([^/]+)$"), "delete_provider"),
    ("GET",    re.compile(r"^/control/sip/trunk-accounts/?$"),       "list_accounts"),
    ("POST",   re.compile(r"^/control/sip/trunk-accounts/?$"),       "create_account"),
    ("GET",    re.compile(r"^/control/sip/trunk-accounts/([^/]+)$"), "show_account"),
    ("POST",   re.compile(r"^/control/sip/trunk-accounts/([^/]+)$"), "upsert_account_by_id"),
    ("PUT",    re.compile(r"^/control/sip/trunk-accounts/([^/]+)$"), "upsert_account_by_id"),
    ("DELETE", re.compile(r"^/control/sip/trunk-accounts/([^/]+)$"), "delete_account"),
    ("POST",   re.compile(r"^/control/sip/originate/?$"),    "originate_call"),
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

    def do_GET(self):    self._dispatch("GET")
    def do_POST(self):   self._dispatch("POST")
    def do_PUT(self):    self._dispatch("PUT")
    def do_DELETE(self): self._dispatch("DELETE")
    def do_PATCH(self):  self._dispatch("PATCH")

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

    def _read_json_body(self):
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

    def _require_store(self):
        if not HAS_SIP_STORE:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": f"sip_store unavailable: {_sip_store_import_error}"})
            return False
        if not _db_enabled():
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "DATABASE_URL not configured"})
            return False
        return True

    def _store_error(self, exc):
        status = HTTPStatus.UNPROCESSABLE_ENTITY
        msg = str(exc)
        if isinstance(exc, sip_store._NotFound):
            status = HTTPStatus.NOT_FOUND
        elif "TRUNK_SECRET_KEY" in msg or "cryptography" in msg:
            status = HTTPStatus.SERVICE_UNAVAILABLE
        self._send_json(status, {"error": msg})

    def list_trunks(self):
        with _lock:
            items = [_to_row(t) for t in _trunks.values()]
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
            return
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
            try:
                _pjsip_upsert({**normalized, "username": normalized.get("username")}, password)
            except _DbError as exc:
                log.error("trunk %s pjsip write failed: %s", normalized["id"], exc)
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                return
            normalized["_password"] = password
            _trunks[normalized["id"]] = normalized

        self._send_json(HTTPStatus.OK, _to_row(normalized))

    def delete_trunk(self, trunk_id):
        with _lock:
            removed = _trunks.pop(trunk_id, None)
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

    def list_providers(self):
        if not self._require_store():
            return
        try:
            items = sip_store.list_providers(_db_conn)
        except sip_store.StoreError as exc:
            self._store_error(exc); return
        self._send_json(HTTPStatus.OK, {"items": items})

    def show_provider(self, provider_id):
        if not self._require_store():
            return
        try:
            self._send_json(HTTPStatus.OK, sip_store.get_provider(_db_conn, provider_id))
        except sip_store.StoreError as exc:
            self._store_error(exc)

    def create_provider(self):
        self._provider_upsert_common(None)

    def upsert_provider_by_id(self, provider_id):
        self._provider_upsert_common(provider_id)

    def _provider_upsert_common(self, url_id):
        if not self._require_store():
            return
        body = self._read_json_body()
        if body is None:
            return
        if url_id is not None and body.get("id") and str(body["id"]) != url_id:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY,
                            {"error": "id in URL and body must match"})
            return
        try:
            out = sip_store.upsert_provider(_db_conn, body, url_id=url_id)
        except sip_store.StoreError as exc:
            self._store_error(exc); return
        self._send_json(HTTPStatus.OK, out)

    def delete_provider(self, provider_id):
        if not self._require_store():
            return
        try:
            sip_store.delete_provider(_db_conn, provider_id)
        except sip_store.StoreError as exc:
            self._store_error(exc); return
        except psycopg2.errors.ForeignKeyViolation:
            self._send_json(HTTPStatus.CONFLICT,
                            {"error": "provider has trunk accounts; delete those first"})
            return
        except psycopg2.Error as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": f"db error: {exc}"})
            return
        self._send_204()

    def list_accounts(self):
        if not self._require_store():
            return
        try:
            items = sip_store.list_accounts(_db_conn)
        except sip_store.StoreError as exc:
            self._store_error(exc); return
        self._send_json(HTTPStatus.OK, {"items": items})

    def show_account(self, account_id):
        if not self._require_store():
            return
        try:
            self._send_json(HTTPStatus.OK, sip_store.get_account(_db_conn, account_id))
        except sip_store.StoreError as exc:
            self._store_error(exc)

    def create_account(self):
        self._account_upsert_common(None)

    def upsert_account_by_id(self, account_id):
        self._account_upsert_common(account_id)

    def _account_upsert_common(self, url_id):
        if not self._require_store():
            return
        body = self._read_json_body()
        if body is None:
            return
        if url_id is not None and body.get("id") and str(body["id"]) != url_id:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY,
                            {"error": "id in URL and body must match"})
            return
        try:
            out = sip_store.upsert_account(_db_conn, body, _pjsip_upsert, url_id=url_id)
        except sip_store.StoreError as exc:
            self._store_error(exc); return
        except _DbError as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, out)

    def delete_account(self, account_id):
        if not self._require_store():
            return
        try:
            sip_store.delete_account(_db_conn, account_id, _pjsip_delete)
        except sip_store.StoreError as exc:
            self._store_error(exc); return
        except _DbError as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return
        self._send_204()

    def originate_call(self):
        body = self._read_json_body()
        if body is None:
            return
        destination = str(body.get("destination") or "").strip()
        trunk_id = str(body.get("trunkId") or "").strip()
        target_endpoint = str(body.get("targetEndpoint") or "").strip()
        extension = str(body.get("extension") or "s").strip()
        context = str(body.get("context") or DEFAULT_INBOUND_CONTEXT).strip()
        from_agent = str(body.get("fromAgent") or "").strip()

        click_to_dial = bool(from_agent and destination and trunk_id and not target_endpoint)

        if click_to_dial:
            if not _SAFE_ID.match(from_agent):
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY,
                                {"error": "fromAgent format invalid"})
                return
            if not _DEST_RE.match(destination):
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY,
                                {"error": "destination must be 1-64 chars: digits, + * #"})
                return
            if not _SAFE_ID.match(trunk_id):
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY,
                                {"error": "trunkId must be 1-60 chars, alphanumerics + _ -"})
                return
            channel = f"PJSIP/{from_agent}"
            mode = "click-to-dial"
        elif target_endpoint:
            if not _SAFE_ID.match(target_endpoint):
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY,
                                {"error": "targetEndpoint must be 1-60 chars, alphanumerics + _ -"})
                return
            channel = f"PJSIP/{target_endpoint}"
            mode = "peer"
        else:
            if not _DEST_RE.match(destination):
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY,
                                {"error": "destination must be 1-64 chars: digits, + * #"})
                return
            if not _SAFE_ID.match(trunk_id):
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY,
                                {"error": "trunkId must be 1-60 chars, alphanumerics + _ -"})
                return
            channel = f"PJSIP/{destination}@{trunk_id}"
            mode = "probe"

        if mode in ("peer", "probe"):
            if not _EXT_RE.match(extension):
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY,
                                {"error": "extension format invalid"})
                return
            if not _SAFE_ID.match(context):
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY,
                                {"error": "context format invalid"})
                return
        if from_agent and not _SAFE_ID.match(from_agent):
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY,
                            {"error": "fromAgent format invalid"})
            return

        if shutil.which(ASTERISK_BIN) is None:
            self._send_json(HTTPStatus.OK, {
                "queued": False, "stub": True,
                "reason": f"{ASTERISK_BIN} binary not on PATH",
            })
            return

        if mode == "click-to-dial":
            dial_args = f"PJSIP/{destination}@{trunk_id},{CLICK_TO_DIAL_TIMEOUT},t"
            cli = f"originate {channel} application Dial {dial_args}"
            extension_reported = f"Dial({dial_args})"
        else:
            cli = f"originate {channel} extension {extension}@{context}"
            extension_reported = f"{extension}@{context}"

        try:
            proc = subprocess.run(
                [ASTERISK_BIN, "-rx", cli],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except subprocess.TimeoutExpired:
            self._send_json(HTTPStatus.GATEWAY_TIMEOUT,
                            {"error": "asterisk -rx originate timed out"})
            return
        except OSError as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY,
                            {"error": f"asterisk exec failed: {exc}"})
            return

        ok = proc.returncode == 0
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        log.info(
            "originate %s mode=%s rc=%d fromAgent=%r stdout=%r stderr=%r",
            channel, mode, proc.returncode, from_agent or "-", out[:200], err[:200],
        )
        self._send_json(
            HTTPStatus.OK if ok else HTTPStatus.BAD_GATEWAY,
            {
                "queued": ok,
                "mode": mode,
                "channel": channel,
                "extension": extension_reported,
                "fromAgent": from_agent or None,
                "rc": proc.returncode,
                "stdout": out,
                "stderr": err,
            },
        )

    def reload_asterisk(self):
        body = self._read_json_body()
        if body is None:
            return
        module = str(body.get("moduleName") or body.get("module") or "").strip()
        if not module:
            module = "res_pjsip.so"
        if module not in {"res_pjsip.so", "res_pjsip_endpoint_identifier_ip.so"}:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY,
                            {"error": f"module not allowed: {module}"})
            return
        if shutil.which(ASTERISK_BIN) is None:
            self._send_json(HTTPStatus.OK, {
                "reloaded": False, "stub": True, "module": module,
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
            {"reloaded": ok, "stub": False, "module": module,
             "rc": proc.returncode, "stdout": out, "stderr": err},
        )


def main():
    if not SECRET:
        log.warning(
            "CONTROL_API_SECRET is not set; /control/* will return 503 until DevOps wires the secret."
        )

    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as exc:
        log.error("failed to bind 0.0.0.0:%d: %s", PORT, exc)
        return 1

    stop_event = threading.Event()

    def _graceful(signum, _frame):
        log.info("received signal %d; shutting down", signum)
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)

    if HAS_SIP_STORE and _db_enabled():
        sip_store.bootstrap(_db_conn)
    elif HAS_SIP_STORE and not _db_enabled():
        log.info("sip_store: DATABASE_URL unset; provider routes will 503")
    elif not HAS_SIP_STORE:
        log.warning("sip_store import failed: %s", _sip_store_import_error)

    feeder_thread = threading.Thread(
        target=_status_feeder_loop, args=(stop_event,),
        name="status-feeder", daemon=True,
    )
    feeder_thread.start()

    if DATABASE_URL and not HAS_PSYCOPG2:
        log.warning(
            "DATABASE_URL is set but psycopg2 is not installed — falling back to "
            "in-memory mode. Trunks will NOT register or carry calls."
        )
    db_mode = "postgres" if _db_enabled() else "memory-only"
    log.info(
        "listening on 0.0.0.0:%d (secret=%s store=%s nat=%s redis=%s sip_store=%s sslmode=%s)",
        PORT,
        "set" if SECRET else "MISSING",
        db_mode,
        "yes" if BEHIND_NAT else "no",
        "yes" if (REDIS_URL and HAS_REDIS) else "no",
        "yes" if HAS_SIP_STORE else "no",
        DATABASE_SSLMODE,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        log.info("control-api stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
