#!/usr/bin/env python3
# Minimal control API that runs alongside Asterisk in the same pod.

import json
import os
import re
import secrets
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
# Epoch-ms heartbeat of the last successful reachability sweep. The
# agent-hub health card reads this to degrade stale data instead of
# trusting an optimistic "online" (AGH-6795).
STATUS_FEEDER_CHECKED_AT_KEY = os.environ.get(
    "STATUS_FEEDER_CHECKED_AT_KEY", "cx:trunks:checked_at"
)
MAX_BODY_BYTES = 64 * 1024

# ASTERISK_EXTERNAL_IP is the master NAT switch. If it's unset but a public
# media address was provided, fall back to that — otherwise endpoints are
# provisioned without rtp_symmetric/force_rport/rewrite_contact and Asterisk
# advertises the pod's private IP, so calls connect with no audio.
EXTERNAL_IP = (
    os.environ.get("ASTERISK_EXTERNAL_IP", "").strip()
    or os.environ.get("ASTERISK_EXTERNAL_MEDIA_ADDRESS", "").strip()
)
BEHIND_NAT = bool(EXTERNAL_IP) or \
    os.environ.get("ASTERISK_BEHIND_NAT", "").strip().lower() in ("1", "yes", "true")

# Legacy in-memory trunk dict — kept only as a fallback when the DB is
# unreachable. The canonical store is the sip_trunks Postgres table
# (sip_store.list_trunks / get_trunk / upsert_trunk / delete_trunk).
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


def _reload_res_pjsip_async():
    """Fire-and-forget reload of res_pjsip.so after a trunk write.

    Without this, the row lands in ps_endpoints but the running endpoint
    keeps its old config (transport, from_user, callerid, …) until the
    next registration interval (~3600s by default). A UI save would
    appear to succeed and the trunk would stay "unknown" for the better
    part of an hour. Reloading is cheap enough to do on every write.

    Runs in a background thread so the HTTP response isn't blocked on
    Asterisk's reload time. Failures are logged but don't propagate;
    the DB write is what's durable.
    """
    def _do_reload():
        try:
            if shutil.which(ASTERISK_BIN) is None:
                return
            proc = subprocess.run(
                [ASTERISK_BIN, "-rx", "module reload res_pjsip.so"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if proc.returncode != 0:
                log.warning(
                    "post-trunk-write res_pjsip reload rc=%d stderr=%r",
                    proc.returncode, (proc.stderr or "").strip()[:200],
                )
        except Exception as exc:
            log.warning("post-trunk-write res_pjsip reload failed: %s", exc)

    threading.Thread(target=_do_reload, name="post-write-reload", daemon=True).start()


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
    # A registering trunk can ALSO receive inbound calls from the carrier's
    # media IP. Registration authenticates OUTBOUND only; for inbound INVITEs
    # to match this endpoint Asterisk needs 'ip' in identify_by plus a
    # ps_identify row for the carrier IP. Without it, inbound from the carrier
    # hits "No matching endpoint found" and is rejected before the dialplan.
    identify_by = ("ip,username,auth_username"
                   if (not register_enabled or carrier_ip)
                   else "username,auth_username")

    # Carrier compatibility, proven against innocalls:
    #
    #   - From URI user MUST match the SIP auth username. The trunk admin
    #     form labels its "FROM USER" field as "caller-id user the
    #     carrier expects" — that's the DID, NOT the From URI user.
    #     Wholesale carriers 403 the INVITE when From != auth identity.
    #     We always write `from_user = username` to ps_endpoints; the
    #     form's `fromUser` value drives `callerid` instead. Operators
    #     that genuinely need From != auth can pass `fromSipUser` in
    #     the trunk body.
    #
    #   - callerid carries the DID as `"X" <X>`. PJSIP parses this for
    #     both CALLERID(name|num) and the P-Asserted-Identity header.
    #
    #   - trust_id_outbound = yes stops Asterisk from rewriting the
    #     From display name to "Anonymous" when no CALLERID(name) is
    #     set. Carriers read Anonymous as a policy violation.
    #
    #   - send_pai = yes emits a P-Asserted-Identity header with the
    #     DID. Carriers validate this against the registered account.
    #
    #   - send_rpid = yes emits a Remote-Party-ID header in parallel
    #     with PAI. Some wholesale SBCs (innocalls observed) don't honor
    #     PAI on its own and 403 the INVITE — RPID is the legacy header
    #     they fall back to. Cheap to send both; carriers ignore what
    #     they don't recognise.
    #
    #   - direct_media = no keeps RTP relayed through Asterisk. With
    #     direct_media = yes the carrier and the WebRTC agent try to
    #     exchange RTP peer-to-peer; the call answers but has no audio.
    #
    #   - rewrite_contact = yes makes asterisk rewrite the registered
    #     Contact URI to whatever it actually saw on the wire (after
    #     NAT). Without this the carrier sometimes routes dialog-internal
    #     requests (re-INVITE, BYE) back to the pod's private IP and the
    #     call appears to hang.
    #
    #   - rtp_symmetric / force_rport = yes are required whenever
    #     asterisk is behind any NAT or load balancer — i.e. nearly
    #     always in a Kubernetes deployment. No-op when there's truly
    #     no NAT, so safe to set unconditionally.
    from_sip_user = (
        row.get("from_sip_user")
        or row.get("from_user_override")
        or row.get("username")
        or ""
    )
    from_domain_value = row.get("from_domain") or _server_uri_host(server_uri) or ""
    callerid_source = row.get("from_user") or row.get("username") or ""
    callerid_value = (
        f'"{callerid_source}" <{callerid_source}>' if callerid_source else ""
    )

    # Trunk-side media encryption. The canonical sip_trunks shape uses
    # 'none' for plain RTP; Asterisk's media_encryption column expects
    # 'no'. Translate here so the writer stays canonical-aware. Empty
    # / unset → 'no' (carrier default for everything we've onboarded;
    # SDES has to be opted into explicitly).
    media_enc_raw = (row.get("media_encryption") or "no").strip().lower()
    if media_enc_raw in ("none", "off", "disabled", ""):
        media_encryption_value = "no"
    elif media_enc_raw in ("no", "sdes", "dtls"):
        media_encryption_value = media_enc_raw
    else:
        media_encryption_value = "no"

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

            # Both NAT / non-NAT branches now write the SAME column set
            # for the carrier-compat flags. The two branches existed
            # historically because rtp_symmetric/force_rport/rewrite_contact
            # were thought of as NAT-only knobs — but in a Kubernetes pod
            # the source IP advertised in SDP (10.0.x.x) is never the
            # actual public IP the carrier sees (post-NLB / SNAT), so
            # asterisk is effectively behind NAT regardless of the
            # ASTERISK_BEHIND_NAT env flag. Setting these unconditionally
            # is the safe default and matches what the runtime SQL fix
            # already applied to the existing inno-calls endpoint.
            if BEHIND_NAT:
                cur.execute("""
                    INSERT INTO ps_endpoints
                        (id, transport, context, aors, auth, allow, dtmf_mode,
                         identify_by, disallow, outbound_auth,
                         from_user, from_domain, callerid,
                         media_encryption,
                         rtp_symmetric, force_rport, rewrite_contact,
                         direct_media,
                         trust_id_outbound, send_pai, send_rpid)
                    VALUES (%s, %s, %s, %s, %s, %s, 'rfc4733',
                            %s, 'all', %s,
                            %s, %s, %s,
                            %s,
                            'yes', 'yes', 'yes',
                            'no',
                            'yes', 'yes', 'yes')
                    ON CONFLICT (id) DO UPDATE SET
                        transport         = EXCLUDED.transport,
                        context           = EXCLUDED.context,
                        aors              = EXCLUDED.aors,
                        auth              = EXCLUDED.auth,
                        allow             = EXCLUDED.allow,
                        identify_by       = EXCLUDED.identify_by,
                        outbound_auth     = EXCLUDED.outbound_auth,
                        from_user         = EXCLUDED.from_user,
                        from_domain       = EXCLUDED.from_domain,
                        callerid          = EXCLUDED.callerid,
                        media_encryption  = EXCLUDED.media_encryption,
                        rtp_symmetric     = EXCLUDED.rtp_symmetric,
                        force_rport       = EXCLUDED.force_rport,
                        rewrite_contact   = EXCLUDED.rewrite_contact,
                        direct_media      = EXCLUDED.direct_media,
                        trust_id_outbound = EXCLUDED.trust_id_outbound,
                        send_pai          = EXCLUDED.send_pai,
                        send_rpid         = EXCLUDED.send_rpid
                """, (row["id"], transport, context, row["id"], auth_id, allow,
                      identify_by, auth_id,
                      from_sip_user or None, from_domain_value or None,
                      callerid_value or None,
                      media_encryption_value))
            else:
                cur.execute("""
                    INSERT INTO ps_endpoints
                        (id, transport, context, aors, auth, allow, dtmf_mode,
                         identify_by, disallow, outbound_auth,
                         from_user, from_domain, callerid,
                         media_encryption,
                         rtp_symmetric, force_rport, rewrite_contact,
                         direct_media,
                         trust_id_outbound, send_pai, send_rpid)
                    VALUES (%s, %s, %s, %s, %s, %s, 'rfc4733',
                            %s, 'all', %s,
                            %s, %s, %s,
                            %s,
                            'yes', 'yes', 'yes',
                            'no',
                            'yes', 'yes', 'yes')
                    ON CONFLICT (id) DO UPDATE SET
                        transport         = EXCLUDED.transport,
                        context           = EXCLUDED.context,
                        aors              = EXCLUDED.aors,
                        auth              = EXCLUDED.auth,
                        allow             = EXCLUDED.allow,
                        identify_by       = EXCLUDED.identify_by,
                        outbound_auth     = EXCLUDED.outbound_auth,
                        from_user         = EXCLUDED.from_user,
                        from_domain       = EXCLUDED.from_domain,
                        callerid          = EXCLUDED.callerid,
                        media_encryption  = EXCLUDED.media_encryption,
                        rtp_symmetric     = EXCLUDED.rtp_symmetric,
                        force_rport       = EXCLUDED.force_rport,
                        rewrite_contact   = EXCLUDED.rewrite_contact,
                        direct_media      = EXCLUDED.direct_media,
                        trust_id_outbound = EXCLUDED.trust_id_outbound,
                        send_pai          = EXCLUDED.send_pai,
                        send_rpid         = EXCLUDED.send_rpid
                """, (row["id"], transport, context, row["id"], auth_id, allow,
                      identify_by, auth_id,
                      from_sip_user or None, from_domain_value or None,
                      callerid_value or None,
                      media_encryption_value))

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
                # Keep an inbound IP identify when the carrier also delivers
                # calls to us from a known media IP — a registering trunk still
                # needs this for INBOUND (registration only covers outbound).
                if carrier_ip:
                    cur.execute("""
                        INSERT INTO ps_identify (id, endpoint, "match")
                        VALUES (%s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            endpoint = EXCLUDED.endpoint,
                            "match"  = EXCLUDED."match"
                    """, (identify_id, row["id"], f"{carrier_ip}/32"))
                else:
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

    # Kick a res_pjsip reload so the new endpoint config takes effect now,
    # not on the next periodic registration cycle.
    _reload_res_pjsip_async()


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

    _reload_res_pjsip_async()


def _provision_agent(tenant_id, agent_id, display_name, context, rotate=False):
    """Create or rotate a per-agent WebRTC PJSIP endpoint.

    The deployed call-engine is this Python sidecar (Dockerfile.prod
    COPYs deploy/control_api.py to /usr/local/bin/control-api).

    Endpoint id is `staff_t<tenant_prefix>_<agentId>`:
      - velentsAgents (Laravel, stancl/tenancy database-per-tenant)
        scopes agent ids per-tenant, so tenant A's agent id 2 is a
        different person from tenant B's. ps_endpoints lives in a
        single Asterisk realtime DB shared by every tenant, so without
        the tenant prefix the second REGISTER overwrites the first.
      - Click-to-dial / Stasis args[2] / WSS REGISTER all see this
        full namespaced id; the Stasis handler parses tenant + agent
        out of it.
      - The queue-dispatcher dialplan ([from-agents] in
        extensions_ai_runtime.conf, `Local/agent_X.@from-agents/n`)
        still uses `PJSIP/staff_${EXTEN:6}` and is therefore single-
        tenant only as of this commit. Multi-tenant queue dispatch
        needs the dialplan to learn the tenant — out of scope here.

    Context defaults to `from-wss-agents-out` because that's the
    dialplan context that runs the outbound rule pipeline (Stasis →
    trunk leg) for every dialed number; `from-agents` only matched
    queue-dispatched inbound and made softphone dial-out a no-op.
    """
    if not _db_enabled():
        raise _DbError("DATABASE_URL not configured")
    if not tenant_id:
        raise _DbError("tenant_id is required for agent provisioning")
    pjsip_id = sip_store.pjsip_agent_endpoint_id(tenant_id, agent_id)
    ctx = context or "from-wss-agents-out"
    display = display_name or f"Staff {agent_id}"
    callerid = f'"{display}" <{pjsip_id}>'

    try:
        with _db_conn() as conn, conn.cursor() as cur:
            # Stable credentials: REUSE the existing password unless an explicit
            # rotate is requested. The softphone calls this endpoint on every
            # mount/reconnect; rotating the password each time invalidated the
            # agent's live REGISTER, producing endless "Failed to authenticate"
            # loops so the desktop flapped out of registration and inbound calls
            # couldn't ring it. Generate a fresh secret only on first create or
            # when rotate=true. (24 random bytes → 32-char urlsafe base64,
            # matching the Node version's crypto.randomBytes(24).base64url.)
            existing_password = None
            if not rotate:
                cur.execute("SELECT password FROM ps_auths WHERE id = %s", (pjsip_id,))
                _row = cur.fetchone()
                existing_password = _row[0] if _row else None
            password = existing_password or secrets.token_urlsafe(24)

            cur.execute("""
                INSERT INTO ps_aors
                    (id, max_contacts, remove_existing, qualify_frequency, support_path)
                VALUES (%s, 1, 'yes', 60, 'yes')
                ON CONFLICT (id) DO UPDATE SET
                    max_contacts      = EXCLUDED.max_contacts,
                    remove_existing   = EXCLUDED.remove_existing,
                    qualify_frequency = EXCLUDED.qualify_frequency,
                    support_path      = EXCLUDED.support_path
            """, (pjsip_id,))

            cur.execute("""
                INSERT INTO ps_auths
                    (id, auth_type, username, password, realm)
                VALUES (%s, 'userpass', %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    auth_type = EXCLUDED.auth_type,
                    username  = EXCLUDED.username,
                    password  = EXCLUDED.password,
                    realm     = EXCLUDED.realm
            """, (pjsip_id, pjsip_id, password, display))

            cur.execute("""
                INSERT INTO ps_endpoints (
                    id, transport, aors, auth, context,
                    disallow, allow, callerid,
                    direct_media, ice_support, use_avpf, rtcp_mux,
                    media_encryption, dtls_verify, dtls_setup,
                    dtls_auto_generate_cert,
                    rtp_symmetric, force_rport, rewrite_contact,
                    webrtc, allow_subscribe, send_pai,
                    agent_id
                )
                VALUES (
                    %s, 'transport-wss', %s, %s, %s,
                    'all', 'ulaw,alaw', %s,
                    'no', 'yes', 'yes', 'yes',
                    'dtls', 'fingerprint', 'actpass',
                    'yes',
                    'yes', 'yes', 'yes',
                    'yes', 'yes', 'yes',
                    %s
                )
                ON CONFLICT (id) DO UPDATE SET
                    transport = EXCLUDED.transport,
                    aors      = EXCLUDED.aors,
                    auth      = EXCLUDED.auth,
                    context   = EXCLUDED.context,
                    disallow  = EXCLUDED.disallow,
                    allow     = EXCLUDED.allow,
                    callerid  = EXCLUDED.callerid,
                    agent_id  = EXCLUDED.agent_id
            """, (pjsip_id, pjsip_id, pjsip_id, ctx, callerid, agent_id))
    except psycopg2.Error as exc:
        raise _DbError(f"agent provisioning failed: {exc}") from exc

    # Make the rotated password / new endpoint visible to res_pjsip now
    # rather than on the next registration cycle. The softphone widget
    # immediately REGISTERs after the credentials response, so without
    # this reload the first REGISTER would hit a stale auth row.
    _reload_res_pjsip_async()
    return {"username": pjsip_id, "password": password}


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


def _build_trunk_id_reverse_map():
    """Map PJSIP realtime endpoint ids back to user-facing trunk slugs.

    Realtime ps_endpoints.id for trunks is now `t<tenant_prefix>_<slug>`
    (the namespaced form sip_store.pjsip_trunk_endpoint_id produces),
    while the UI's status hash is keyed by the user-facing slug
    (`innov2`, not `tdefault_innov2`). Without the reverse, the
    trunk badge in the UI stays grey/"unknown" even when the trunk
    is happily registered. Query sip_trunks once per feeder tick and
    return realtime_id -> slug.
    """
    if not HAS_SIP_STORE or not _db_enabled():
        return {}
    try:
        with _db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT tenant_id, id FROM sip_trunks")
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("status feeder: trunk reverse map query failed: %s", exc)
        return {}
    out = {}
    for tenant_id, slug in rows:
        out[sip_store.pjsip_trunk_endpoint_id(tenant_id, slug)] = slug
    return out


_AGENT_ID_NAMESPACED_RE = re.compile(r"^staff_t[a-z0-9]{1,8}_(?P<rest>.+)$")


def _decorate_trunks_with_live_state(items, tenant_id):
    """Embed `state` and `activeChannels` into each trunk row in-place.

    The Redis-based status feeder only works when asterisk and agent-hub
    share a Redis instance. When agent-hub runs in a different cluster
    (test on GCP while asterisk lives on AWS), there's no shared Redis
    and the UI's badge stays grey. Embedding state here makes the
    /control/sip/trunks response self-sufficient — agent-hub gets the
    badge value over the same HTTPS call it already makes.

    Reads `pjsip show endpoints` once for the whole list. Mutates each
    item dict (camelCase fields: state, activeChannels) and returns
    nothing.
    """
    if not items:
        return
    if shutil.which(ASTERISK_BIN) is None:
        return
    try:
        ep_proc = subprocess.run(
            [ASTERISK_BIN, "-rx", "pjsip show endpoints"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        id_proc = subprocess.run(
            [ASTERISK_BIN, "-rx", "pjsip show identifies"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("trunk live-state decorate failed: %s", exc)
        return
    if ep_proc.returncode != 0:
        return

    # Map slug -> live state. PJSIP endpoint ids are tenant-namespaced,
    # so for each row compute the expected pjsip id and look it up.
    state_by_pjsip = {}
    for ep_id, state in _parse_pjsip_endpoints(ep_proc.stdout):
        # Strip trailing /contact suffix some pjsip versions add.
        head = ep_id.split("/", 1)[0]
        state_by_pjsip[head] = _state_to_status(state)
    ip_trunks = (
        set(_parse_pjsip_identifies(id_proc.stdout))
        if id_proc.returncode == 0 else set()
    )

    for row in items:
        slug = row.get("id")
        if not slug:
            continue
        pjsip_id = sip_store.pjsip_trunk_endpoint_id(tenant_id, slug)
        if pjsip_id in ip_trunks:
            row["state"] = "online"
        else:
            row["state"] = state_by_pjsip.get(pjsip_id, "unknown")
        # activeChannels needs a separate channel-show round-trip;
        # leave at 0 here so the badge color is at least correct.
        # The UI's util read still works through Redis when it's
        # configured; this just guarantees the *state* part isn't
        # blocked by Redis being unreachable.
        row.setdefault("activeChannels", 0)


def _user_facing_agent_id(pjsip_id):
    """Strip the tenant prefix from a namespaced agent endpoint id.

    `staff_tdefault_2` -> `staff_2`. Non-namespaced legacy ids are
    returned unchanged so a single deployment can still surface
    pre-migration rows while they exist.
    """
    m = _AGENT_ID_NAMESPACED_RE.match(pjsip_id)
    if not m:
        return pjsip_id
    return f"staff_{m.group('rest')}"


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

            # Map namespaced realtime ids back to user-facing slugs so
            # the UI's lookup by row.id matches what's in Redis. The
            # reverse map covers every row in sip_trunks; endpoints
            # not present in sip_trunks (e.g. agent endpoints that
            # leaked in here) fall through unchanged.
            trunk_reverse = _build_trunk_id_reverse_map()

            def _trunk_slug(ep_id):
                # `pjsip show endpoints` sometimes emits the endpoint
                # as `endpoint/contact` once a contact has registered;
                # we only want the endpoint part for the UI join.
                head = ep_id.split("/", 1)[0]
                return trunk_reverse.get(head, head)

            if ep_proc.returncode == 0:
                trunk_updates = {}
                for ep_id, state in _parse_pjsip_endpoints(ep_proc.stdout):
                    slug = _trunk_slug(ep_id)
                    if ep_id.split("/", 1)[0] in ip_trunk_endpoints:
                        trunk_updates[slug] = "online"
                    else:
                        trunk_updates[slug] = _state_to_status(state)
                if trunk_updates:
                    client.hset(STATUS_FEEDER_KEY, mapping=trunk_updates)
                # Stamp the sweep time whenever Asterisk answered, even if
                # no trunk rows matched — the freshness signal is "did we
                # just verify reachability", independent of trunk count.
                client.set(
                    STATUS_FEEDER_CHECKED_AT_KEY, int(time.time() * 1000)
                )

            if aor_proc.returncode == 0:
                agent_updates = {}
                for aor_id, status in _parse_pjsip_aors(aor_proc.stdout):
                    head = aor_id.split("/", 1)[0]
                    if head in trunk_endpoints or head in trunk_reverse:
                        continue
                    # Agents are keyed in Redis by the un-namespaced
                    # form `staff_<id>` so the agent-hub UI (which
                    # doesn't know about tenant prefixes) joins
                    # cleanly. Trunks come from sip_trunks; agents
                    # don't, so use the regex-based stripper.
                    agent_updates[_user_facing_agent_id(head)] = status
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
    ("POST",   re.compile(r"^/control/sip/agents/([^/]+)/credentials/?$"),
                                                             "provision_agent_credentials"),
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

    def _tenant_from_request(self, body=None):
        """Extract the caller's tenant id.

        agent-hub stamps a `X-Tenant-Id` header on every forwarded
        request from the existing `tenantFrom(req)` helper; the body
        fallback exists only for tools that don't go through agent-hub
        (curl, internal scripts). Returns None if neither carries it —
        the caller responds 401, never silently defaults.
        """
        header = self.headers.get("x-tenant-id") or self.headers.get("X-Tenant-Id")
        if header and header.strip():
            return header.strip()
        if isinstance(body, dict):
            t = body.get("tenantId") or body.get("tenant_id")
            if t:
                return str(t).strip()
        return None

    def _require_tenant_or_401(self, body=None):
        t = self._tenant_from_request(body=body)
        if not t:
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"error": "missing tenant context (X-Tenant-Id header)"},
            )
            return None
        return t

    def list_trunks(self):
        if not self._require_store():
            return
        tenant_id = self._require_tenant_or_401()
        if tenant_id is None:
            return
        try:
            items = sip_store.list_trunks(_db_conn, tenant_id)
        except sip_store.StoreError as exc:
            self._store_error(exc); return
        _decorate_trunks_with_live_state(items, tenant_id)
        self._send_json(HTTPStatus.OK, {"items": items})

    def show_trunk(self, trunk_id):
        if not self._require_store():
            return
        tenant_id = self._require_tenant_or_401()
        if tenant_id is None:
            return
        try:
            row = sip_store.get_trunk(_db_conn, tenant_id, trunk_id)
        except sip_store.StoreError as exc:
            self._store_error(exc); return
        _decorate_trunks_with_live_state([row], tenant_id)
        self._send_json(HTTPStatus.OK, row)

    def create_trunk(self):
        self._trunk_upsert_common(None)

    def upsert_trunk_by_id(self, trunk_id):
        self._trunk_upsert_common(trunk_id)

    def _trunk_upsert_common(self, url_id):
        if not self._require_store():
            return
        body = self._read_json_body()
        if body is None:
            return
        tenant_id = self._require_tenant_or_401(body=body)
        if tenant_id is None:
            return
        if url_id is not None and body.get("id") and str(body["id"]) != url_id:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY,
                            {"error": "id in URL and body must match"})
            return
        try:
            out = sip_store.upsert_trunk(
                _db_conn, tenant_id, body, _pjsip_upsert, url_id=url_id,
            )
        except sip_store.StoreError as exc:
            self._store_error(exc); return
        except _DbError as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, out)

    def delete_trunk(self, trunk_id):
        if not self._require_store():
            return
        tenant_id = self._require_tenant_or_401()
        if tenant_id is None:
            return
        try:
            sip_store.delete_trunk(_db_conn, tenant_id, trunk_id, _pjsip_delete)
        except sip_store.StoreError as exc:
            self._store_error(exc); return
        except _DbError as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return
        self._send_204()

    # ── legacy routes, kept only to point operators at the new one ──
    # The provider+account split is the bug class the trunk rebuild
    # removes. These routes (list/show/upsert/delete) all return 410
    # Gone for one release; the next deploy can drop them entirely.

    def _gone(self, _arg=None, *_rest):
        self._send_json(
            HTTPStatus.GONE,
            {
                "error": "endpoint removed; use /control/sip/trunks",
                "replacement": "/control/sip/trunks",
                "note": (
                    "SIP trunks are now modeled as one flat carrier-credential "
                    "row (name + address + protocol + mediaEncryption + "
                    "authUsername + authPassword + numbers). The "
                    "provider/account split has been collapsed."
                ),
            },
        )

    list_providers           = _gone
    show_provider            = _gone
    create_provider          = _gone
    upsert_provider_by_id    = _gone
    delete_provider          = _gone
    list_accounts            = _gone
    show_account             = _gone
    create_account           = _gone
    upsert_account_by_id     = _gone
    delete_account           = _gone

    def originate_call(self):
        body = self._read_json_body()
        if body is None:
            return
        destination = str(body.get("destination") or "").strip()
        trunk_id = str(body.get("trunkId") or "").strip()
        target_endpoint = str(body.get("targetEndpoint") or "").strip()
        target_agent_id = str(body.get("targetAgentId") or "").strip()
        extension = str(body.get("extension") or "s").strip()
        context = str(body.get("context") or DEFAULT_INBOUND_CONTEXT).strip()
        from_agent = str(body.get("fromAgent") or "").strip()

        click_to_dial = bool(
            from_agent and destination and trunk_id
            and not target_endpoint and not target_agent_id
        )

        # Tenant context is required for click-to-dial (we need to
        # resolve the trunk owner) and for any path that names an
        # agent or trunk by user-facing id. Probe mode that names
        # only an already-namespaced endpoint can run without it.
        needs_tenant = bool(
            click_to_dial or trunk_id or from_agent or target_agent_id
        )
        tenant_id = None
        if needs_tenant:
            tenant_id = self._require_tenant_or_401(body=body)
            if tenant_id is None:
                return

        # Backward-compat: accept fromAgent either as the bare agent id
        # ("2") or as a legacy `staff_<id>` string. Either way, the
        # channel uses the namespaced realtime endpoint id computed
        # from the caller's tenant — no cross-tenant spoof possible.
        def _agent_to_pjsip(raw_agent: str) -> str:
            bare = raw_agent
            if bare.startswith("staff_"):
                bare = bare[len("staff_"):]
            return sip_store.pjsip_agent_endpoint_id(tenant_id, bare)

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
            # Verify the trunk exists AND belongs to the caller's
            # tenant. get_trunk raises _NotFound otherwise — that's
            # the actual security barrier against using another
            # tenant's trunk.
            try:
                sip_store.get_trunk(_db_conn, tenant_id, trunk_id)
            except sip_store.StoreError as exc:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": f"trunk not found in your tenant: {exc}"},
                )
                return
            channel = f"PJSIP/{_agent_to_pjsip(from_agent)}"
            mode = "click-to-dial"
        elif target_agent_id:
            # Internal peer call by bare agent id. Tenant context comes
            # from the X-Tenant-Id header; we namespace into the
            # realtime endpoint name so cross-tenant peer calls aren't
            # possible (a different tenant's staff_t<other>_42 won't
            # match the staff_t<this>_42 we compute).
            if not _SAFE_ID.match(target_agent_id):
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY,
                                {"error": "targetAgentId format invalid"})
                return
            channel = (
                f"PJSIP/"
                f"{sip_store.pjsip_agent_endpoint_id(tenant_id, target_agent_id)}"
            )
            mode = "peer"
        elif target_endpoint:
            # Legacy raw-endpoint path. Kept so probe scripts that
            # craft a `targetEndpoint: <pjsip_id>` body keep working;
            # new callers should use `targetAgentId` and let the
            # tenant prefix happen here.
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
            try:
                sip_store.get_trunk(_db_conn, tenant_id, trunk_id)
            except sip_store.StoreError as exc:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": f"trunk not found in your tenant: {exc}"},
                )
                return
            channel = (
                f"PJSIP/{destination}"
                f"@{sip_store.pjsip_trunk_endpoint_id(tenant_id, trunk_id)}"
            )
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
            pjsip_trunk = sip_store.pjsip_trunk_endpoint_id(tenant_id, trunk_id)
            dial_args = (
                f"PJSIP/{destination}@{pjsip_trunk},{CLICK_TO_DIAL_TIMEOUT},t"
            )
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

    def provision_agent_credentials(self, agent_id):
        body = self._read_json_body()
        if body is None:
            return
        if not _SAFE_ID.match(agent_id):
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY,
                            {"error": "agentId must be 1-60 chars, alphanumerics + _ -"})
            return
        if not _db_enabled():
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "DATABASE_URL not configured"})
            return
        tenant_id = self._require_tenant_or_401(body=body)
        if tenant_id is None:
            return
        display_name = str(body.get("displayName") or "").strip()
        context = str(body.get("context") or "").strip()
        if context and not _SAFE_ID.match(context):
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY,
                            {"error": "context format invalid"})
            return
        try:
            result = _provision_agent(
                tenant_id, agent_id, display_name, context or None,
                rotate=bool(body.get("rotate")),
            )
        except _DbError as exc:
            log.error(
                "agent %s (tenant=%s) provision failed: %s",
                agent_id, tenant_id, exc,
            )
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return
        log.info(
            "agent %s (tenant=%s) provisioned: endpoint=%s",
            agent_id, tenant_id, result["username"],
        )
        self._send_json(HTTPStatus.OK, result)

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
