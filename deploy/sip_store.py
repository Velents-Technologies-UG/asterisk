#!/usr/bin/env python3
"""
SIP provider + trunk-account store.

Owns two Postgres tables that sit next to Asterisk's PJSIP realtime
tables (ps_*) in the same database:

  sip_providers       — one row per carrier (innocalls, twilio, …).
                        Captures the carrier-side SIP coordinates
                        (server URI, transport, mode, carrier IP) and
                        the policy decision register vs ip-trunk.

  sip_trunk_accounts  — one row per account we hold on a provider.
                        Captures our identity (username, encrypted
                        password, from_user, channel limit, …) and
                        references its provider via provider_id.

The agent-hub UI talks to this via the sidecar's /control/sip/providers
and /control/sip/trunk-accounts routes. On account upsert, the sidecar
joins provider + account and feeds the flat record to _pjsip_upsert
from control_api.py, which is the same writer that has always handled
ps_*. The IP-trunk branch (registerEnabled=False + carrierIp) was
added in the parent commit and is the reason this refactor exists:
mode is now a per-provider attribute, not a per-account flag.

Bootstrap also patches the ps_endpoints schema if columns we depend on
are missing — see _DDL_PS_ENDPOINTS_PATCH. This guards against the
"stripped-down realtime schema" deployments out there (Asterisk 22's
contrib/realtime ships >40 ps_endpoints columns; many tenants
provisioned only a subset and lose from_user / from_domain / callerid
on the trunk side, causing every outbound INVITE to go out
`From: "Anonymous" <sip:anonymous@anonymous.invalid>`).

Passwords are stored ciphertext-only. AES-GCM-256 with a 32-byte key
derived from $TRUNK_SECRET_KEY (base64 OR raw hex OR raw bytes — we
detect). Ciphertext format: 'v1$<nonce-b64>$<ct+tag-b64>'. Versioned
so we can rotate keys without rewriting the table.

If TRUNK_SECRET_KEY is missing, the encryption path raises and the
sidecar returns 503 on any account write. Reads of accounts with
plaintext-empty passwords still work.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from typing import Iterable, Optional

try:
    import psycopg2
    import psycopg2.extras
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

log = logging.getLogger("control-api.sip-store")

# ── identifiers ────────────────────────────────────────

# Provider ids are lowercase + digits + dash (mirrors how we slug them
# in UI URLs). 1-40 chars keeps room for things like 'innocalls-eu'.
_SAFE_PROVIDER_ID = re.compile(r"^[a-z0-9-]{1,40}$")

# Account ids mirror the existing PJSIP endpoint id rule because that's
# what they become at the realtime layer. Permissive on case + underscore
# so users can call them 'inno-calls-saudi', 'TwilioMain', etc.
_SAFE_ACCOUNT_ID = re.compile(r"^[a-zA-Z0-9_-]{1,60}$")

_VALID_TRANSPORTS = {"udp", "tcp", "tls"}
_VALID_MODES      = {"register", "ip-trunk"}


class StoreError(RuntimeError):
    """Raised for validation, encryption, or persistence problems."""


class _NotFound(StoreError):
    pass


# ── schema ────────────────────────────────────────────

_DDL_PROVIDERS = r"""
CREATE TABLE IF NOT EXISTS sip_providers (
    id                     TEXT PRIMARY KEY,
    display_name           TEXT NOT NULL,
    description            TEXT,
    server_uri             TEXT NOT NULL,
    transport              TEXT NOT NULL DEFAULT 'tls',
    mode                   TEXT NOT NULL DEFAULT 'register',
    carrier_ip             INET,
    default_from_domain    TEXT,
    default_realm          TEXT,
    enabled                BOOLEAN NOT NULL DEFAULT TRUE,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT sip_providers_id_format
        CHECK (id ~ '^[a-z0-9-]{1,40}$'),
    CONSTRAINT sip_providers_transport_valid
        CHECK (transport IN ('udp','tcp','tls')),
    CONSTRAINT sip_providers_mode_valid
        CHECK (mode IN ('register','ip-trunk')),
    CONSTRAINT sip_providers_ip_required_for_ip_trunk
        CHECK (mode <> 'ip-trunk' OR carrier_ip IS NOT NULL)
);
"""

_DDL_ACCOUNTS = r"""
CREATE TABLE IF NOT EXISTS sip_trunk_accounts (
    id                     TEXT PRIMARY KEY,
    provider_id            TEXT NOT NULL REFERENCES sip_providers(id) ON DELETE RESTRICT,
    display_name           TEXT NOT NULL,
    username               TEXT NOT NULL,
    password_enc           TEXT NOT NULL,
    from_user              TEXT,
    from_domain            TEXT,
    channel_limit          INTEGER NOT NULL DEFAULT 50,
    expiration_seconds     INTEGER NOT NULL DEFAULT 3600,
    description            TEXT,
    enabled                BOOLEAN NOT NULL DEFAULT TRUE,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT sip_trunk_accounts_id_format
        CHECK (id ~ '^[a-zA-Z0-9_-]{1,60}$'),
    CONSTRAINT sip_trunk_accounts_channel_limit_range
        CHECK (channel_limit BETWEEN 1 AND 1000),
    CONSTRAINT sip_trunk_accounts_expiration_range
        CHECK (expiration_seconds BETWEEN 60 AND 86400)
);
CREATE INDEX IF NOT EXISTS sip_trunk_accounts_provider_id_idx
    ON sip_trunk_accounts (provider_id);
"""

# Patches the standard PJSIP realtime ps_endpoints table to add columns
# our sidecar writes but some deployments don't ship. ADD COLUMN IF NOT
# EXISTS so this is idempotent on a fresh contrib schema or a hand-
# rolled subset. Asterisk's full ps_endpoints schema in contrib has
# >40 columns; the three below are the ones we actively populate from
# trunk-account fields and were missing on the velents tenant DB.
_DDL_PS_ENDPOINTS_PATCH = r"""
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS from_user   VARCHAR(40);
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS from_domain VARCHAR(190);
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS callerid    VARCHAR(40);
"""

# Seed the one provider we actively need so the UI has a selectable
# carrier on first boot. ON CONFLICT keeps re-seeding idempotent and
# preserves any operator edits.
_SEED_INNOCALLS = r"""
INSERT INTO sip_providers
    (id, display_name, description, server_uri, transport, mode,
     carrier_ip, default_from_domain)
VALUES
    ('innocalls', 'innocalls',
     'innocalls wholesale termination',
     'sip:cu622.sip.innocalls.net:5061', 'tls', 'ip-trunk',
     '77.75.224.252', 'cu622.sip.innocalls.net')
ON CONFLICT (id) DO NOTHING;
"""


def bootstrap(db_conn_factory) -> None:
    """Create tables + seed default rows. Idempotent.

    db_conn_factory: callable returning a fresh psycopg2 connection.
    Logs and swallows failures so a transient DB issue doesn't crash
    the sidecar — the routes that depend on these tables will surface
    the error on first request.

    The ps_endpoints ALTER is wrapped in its own try/except because
    ALTER TABLE on a table the sidecar doesn't own (it's the standard
    PJSIP realtime table) could in theory fail under a stricter RBAC
    setup. We still want sip_providers / sip_trunk_accounts to come up
    even if the patch can't be applied.
    """
    if not HAS_PSYCOPG2:
        log.warning("sip_store.bootstrap: psycopg2 unavailable; skipping")
        return
    try:
        with db_conn_factory() as conn, conn.cursor() as cur:
            cur.execute(_DDL_PROVIDERS)
            cur.execute(_DDL_ACCOUNTS)
            cur.execute(_SEED_INNOCALLS)
        log.info("sip_store.bootstrap: provider+account tables ensured + seeded")
    except Exception as exc:
        log.error("sip_store.bootstrap (providers/accounts) failed: %s", exc)

    try:
        with db_conn_factory() as conn, conn.cursor() as cur:
            cur.execute(_DDL_PS_ENDPOINTS_PATCH)
        log.info("sip_store.bootstrap: ps_endpoints from_user/from_domain/callerid columns ensured")
    except Exception as exc:
        log.warning(
            "sip_store.bootstrap (ps_endpoints patch) failed: %s — "
            "outbound INVITEs may go out as anonymous if these columns are missing",
            exc,
        )


# ── crypto ───────────────────────────────────────────

def _key_bytes() -> bytes:
    """Resolve the 32-byte AES key from the TRUNK_SECRET_KEY env var.

    Accepts (in order):
      1. 32 raw bytes (PROBABLY won't appear via env, but supported).
      2. 64 hex chars.
      3. base64-encoded 32 bytes (standard or urlsafe, with or without padding).

    Raises StoreError if the key is missing or has the wrong length so
    the operator gets a clear error rather than a cryptography-internal one.
    """
    raw = os.environ.get("TRUNK_SECRET_KEY", "").strip()
    if not raw:
        raise StoreError(
            "TRUNK_SECRET_KEY is not set; trunk-account writes are refused "
            "until DevOps wires a 32-byte secret (base64 or hex)."
        )
    # Hex (64 chars)
    if len(raw) == 64 and all(c in "0123456789abcdefABCDEF" for c in raw):
        try:
            return bytes.fromhex(raw)
        except ValueError:
            pass
    # base64 (standard or url-safe; pad if needed)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            padded = raw + "=" * (-len(raw) % 4)
            b = decoder(padded)
            if len(b) == 32:
                return b
        except Exception:
            continue
    # raw bytes
    b = raw.encode("utf-8")
    if len(b) == 32:
        return b
    raise StoreError(
        f"TRUNK_SECRET_KEY must decode to 32 bytes; got {len(b)} bytes from "
        "the raw value (and neither hex nor base64 decoded to 32 bytes)."
    )


def encrypt_password(plaintext: str) -> str:
    """Encrypt 'plaintext' under AES-GCM-256 using TRUNK_SECRET_KEY.

    Returns 'v1$<nonce-b64url>$<ct+tag-b64url>'. Empty plaintext is
    encoded as an empty ciphertext value so we can store 'no password'
    without a NULL.
    """
    if plaintext is None:
        plaintext = ""
    if not HAS_CRYPTO:
        raise StoreError(
            "python3-cryptography is not installed; trunk-account password "
            "encryption is unavailable. Rebuild the image with python3-cryptography."
        )
    key = _key_bytes()
    aes = AESGCM(key)
    nonce = os.urandom(12)
    ct = aes.encrypt(nonce, plaintext.encode("utf-8"), None)
    return "v1$%s$%s" % (
        base64.urlsafe_b64encode(nonce).rstrip(b"=").decode(),
        base64.urlsafe_b64encode(ct).rstrip(b"=").decode(),
    )


def decrypt_password(ciphertext: str) -> str:
    """Inverse of encrypt_password. Empty / falsy input → empty string."""
    if not ciphertext:
        return ""
    parts = ciphertext.split("$", 2)
    if len(parts) != 3 or parts[0] != "v1":
        raise StoreError(f"unrecognized ciphertext format: {parts[0]!r}")
    if not HAS_CRYPTO:
        raise StoreError(
            "python3-cryptography is not installed; cannot decrypt stored password."
        )
    nonce = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
    ct = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
    key = _key_bytes()
    aes = AESGCM(key)
    pt = aes.decrypt(nonce, ct, None)
    return pt.decode("utf-8")


# ── validation ────────────────────────────────────────

def _str_or_none(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _bool_or_default(v, default: bool) -> bool:
    if v is None:
        return default
    if not isinstance(v, bool):
        raise StoreError("must be a boolean")
    return v


def validate_provider_input(body: dict, partial: bool = False) -> dict:
    """Return a dict of column→value for INSERT/UPDATE, or raise StoreError.

    partial=True relaxes 'required' checks for PATCH-style updates,
    but the field-level type checks still apply.
    """
    out: dict = {}

    if not partial or "id" in body:
        pid = _str_or_none(body.get("id"))
        if not pid:
            raise StoreError("id is required")
        if not _SAFE_PROVIDER_ID.match(pid):
            raise StoreError(
                "id must be 1-40 chars of lowercase a-z, 0-9, or '-'"
            )
        out["id"] = pid

    if not partial or "displayName" in body:
        dn = _str_or_none(body.get("displayName"))
        if not dn:
            raise StoreError("displayName is required")
        out["display_name"] = dn

    if not partial or "serverUri" in body:
        su = _str_or_none(body.get("serverUri"))
        if not su:
            raise StoreError("serverUri is required")
        out["server_uri"] = su

    if "description" in body:
        out["description"] = _str_or_none(body.get("description"))

    if "transport" in body or not partial:
        t = body.get("transport") or "tls"
        if t not in _VALID_TRANSPORTS:
            raise StoreError(f"transport must be one of: {sorted(_VALID_TRANSPORTS)}")
        out["transport"] = t

    if "mode" in body or not partial:
        m = body.get("mode") or "register"
        if m not in _VALID_MODES:
            raise StoreError(f"mode must be one of: {sorted(_VALID_MODES)}")
        out["mode"] = m

    if "carrierIp" in body:
        out["carrier_ip"] = _str_or_none(body.get("carrierIp"))

    if "defaultFromDomain" in body:
        out["default_from_domain"] = _str_or_none(body.get("defaultFromDomain"))
    if "defaultRealm" in body:
        out["default_realm"] = _str_or_none(body.get("defaultRealm"))

    if "enabled" in body:
        out["enabled"] = _bool_or_default(body.get("enabled"), True)

    # Cross-field invariant. Without this the CHECK constraint would
    # fire on COMMIT with a less helpful error.
    final_mode = out.get("mode", "register")
    final_ip = out.get("carrier_ip")
    if final_mode == "ip-trunk" and not final_ip:
        # Allow PATCH that doesn't touch carrier_ip if existing row has one;
        # the upsert SQL will fall back to existing values.
        if not partial:
            raise StoreError("carrierIp is required when mode is 'ip-trunk'")

    return out


def validate_account_input(body: dict, partial: bool = False) -> tuple[dict, Optional[str]]:
    """Return (column->value, plaintext_password_or_None).

    The password (if provided) is returned separately so the caller
    can encrypt at write-time rather than logging it on validation error.
    """
    out: dict = {}

    if not partial or "id" in body:
        aid = _str_or_none(body.get("id"))
        if not aid:
            raise StoreError("id is required")
        if not _SAFE_ACCOUNT_ID.match(aid):
            raise StoreError(
                "id must be 1-60 chars, alphanumerics, underscore, or dash"
            )
        out["id"] = aid

    if not partial or "providerId" in body:
        pid = _str_or_none(body.get("providerId"))
        if not pid:
            raise StoreError("providerId is required")
        if not _SAFE_PROVIDER_ID.match(pid):
            raise StoreError("providerId is malformed")
        out["provider_id"] = pid

    if not partial or "displayName" in body:
        dn = _str_or_none(body.get("displayName"))
        if not dn:
            raise StoreError("displayName is required")
        out["display_name"] = dn

    if not partial or "username" in body:
        un = _str_or_none(body.get("username"))
        if not un:
            raise StoreError("username is required")
        out["username"] = un

    if "fromUser" in body:
        out["from_user"] = _str_or_none(body.get("fromUser"))
    if "fromDomain" in body:
        out["from_domain"] = _str_or_none(body.get("fromDomain"))
    if "description" in body:
        out["description"] = _str_or_none(body.get("description"))

    if "channelLimit" in body or not partial:
        try:
            cl = int(body.get("channelLimit", 50))
        except (TypeError, ValueError):
            raise StoreError("channelLimit must be an integer")
        if not 1 <= cl <= 1000:
            raise StoreError("channelLimit must be 1..1000")
        out["channel_limit"] = cl

    if "expirationSeconds" in body or "expiration" in body or not partial:
        v = body.get("expirationSeconds", body.get("expiration", 3600))
        try:
            es = int(v)
        except (TypeError, ValueError):
            raise StoreError("expirationSeconds must be an integer")
        if not 60 <= es <= 86400:
            raise StoreError("expirationSeconds must be 60..86400")
        out["expiration_seconds"] = es

    if "enabled" in body:
        out["enabled"] = _bool_or_default(body.get("enabled"), True)

    pw_field = body.get("password")
    plaintext: Optional[str] = None
    if pw_field is not None:
        plaintext = str(pw_field)
        # On full (non-partial) writes, require a non-empty password
        # — without one the carrier digest challenge for outbound INVITE
        # will fail. PATCH updates leave the old encrypted password.
        if not partial and not plaintext:
            raise StoreError("password is required for a new trunk account")

    return out, plaintext


# ── CRUD: providers ───────────────────────────────────

_PROVIDER_COLUMNS = (
    "id", "display_name", "description", "server_uri", "transport",
    "mode", "carrier_ip", "default_from_domain", "default_realm",
    "enabled", "created_at", "updated_at",
)


def _row_to_provider(row) -> dict:
    """Cast a DictRow into the API shape (camelCase booleans, ISO times)."""
    return {
        "id":                row["id"],
        "displayName":       row["display_name"],
        "description":       row["description"],
        "serverUri":         row["server_uri"],
        "transport":         row["transport"],
        "mode":              row["mode"],
        "carrierIp":         str(row["carrier_ip"]) if row["carrier_ip"] is not None else None,
        "defaultFromDomain": row["default_from_domain"],
        "defaultRealm":      row["default_realm"],
        "enabled":           bool(row["enabled"]),
        "createdAt":         row["created_at"].isoformat() if row["created_at"] else None,
        "updatedAt":         row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def list_providers(db_conn_factory) -> list[dict]:
    with db_conn_factory() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                f"SELECT {', '.join(_PROVIDER_COLUMNS)} "
                "FROM sip_providers ORDER BY display_name, id"
            )
            return [_row_to_provider(r) for r in cur.fetchall()]


def get_provider(db_conn_factory, provider_id: str) -> dict:
    with db_conn_factory() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                f"SELECT {', '.join(_PROVIDER_COLUMNS)} "
                "FROM sip_providers WHERE id = %s",
                (provider_id,),
            )
            row = cur.fetchone()
            if not row:
                raise _NotFound(f"provider {provider_id!r} not found")
            return _row_to_provider(row)


def upsert_provider(db_conn_factory, body: dict, url_id: Optional[str] = None) -> dict:
    if url_id and "id" not in body:
        body = {**body, "id": url_id}
    cols = validate_provider_input(body, partial=False)
    with db_conn_factory() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                INSERT INTO sip_providers
                    (id, display_name, description, server_uri, transport,
                     mode, carrier_ip, default_from_domain, default_realm, enabled,
                     updated_at)
                VALUES (%(id)s, %(display_name)s, %(description)s, %(server_uri)s,
                        %(transport)s, %(mode)s, %(carrier_ip)s,
                        %(default_from_domain)s, %(default_realm)s,
                        %(enabled)s, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    display_name        = EXCLUDED.display_name,
                    description         = EXCLUDED.description,
                    server_uri          = EXCLUDED.server_uri,
                    transport           = EXCLUDED.transport,
                    mode                = EXCLUDED.mode,
                    carrier_ip          = EXCLUDED.carrier_ip,
                    default_from_domain = EXCLUDED.default_from_domain,
                    default_realm       = EXCLUDED.default_realm,
                    enabled             = EXCLUDED.enabled,
                    updated_at          = NOW()
                """,
                {
                    "id":                  cols["id"],
                    "display_name":        cols["display_name"],
                    "description":         cols.get("description"),
                    "server_uri":          cols["server_uri"],
                    "transport":           cols["transport"],
                    "mode":                cols["mode"],
                    "carrier_ip":          cols.get("carrier_ip"),
                    "default_from_domain": cols.get("default_from_domain"),
                    "default_realm":       cols.get("default_realm"),
                    "enabled":             cols.get("enabled", True),
                },
            )
    return get_provider(db_conn_factory, cols["id"])


def delete_provider(db_conn_factory, provider_id: str) -> None:
    """Refuses (via FK ON DELETE RESTRICT) if accounts reference it."""
    with db_conn_factory() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sip_providers WHERE id = %s", (provider_id,))
        if cur.rowcount == 0:
            raise _NotFound(f"provider {provider_id!r} not found")


# ── CRUD: trunk accounts ──────────────────────────────

_ACCOUNT_SELECT = """
SELECT
    a.id                   AS id,
    a.provider_id          AS provider_id,
    a.display_name         AS display_name,
    a.username             AS username,
    a.password_enc         AS password_enc,
    a.from_user            AS from_user,
    a.from_domain          AS from_domain,
    a.channel_limit        AS channel_limit,
    a.expiration_seconds   AS expiration_seconds,
    a.description          AS description,
    a.enabled              AS account_enabled,
    a.created_at           AS account_created_at,
    a.updated_at           AS account_updated_at,
    p.id                   AS p_id,
    p.display_name         AS p_display_name,
    p.server_uri           AS p_server_uri,
    p.transport            AS p_transport,
    p.mode                 AS p_mode,
    p.carrier_ip           AS p_carrier_ip,
    p.default_from_domain  AS p_default_from_domain,
    p.default_realm        AS p_default_realm,
    p.enabled              AS p_enabled
FROM sip_trunk_accounts a
JOIN sip_providers p ON p.id = a.provider_id
"""


def _row_to_account(row) -> dict:
    """Map a joined account+provider row to the JSON API shape.

    Plaintext password is NEVER included. The encrypted blob is dropped
    from the projection too — a sentinel 'hasPassword' flag is exposed
    instead so the UI can show whether a value is set.
    """
    carrier_ip = row["p_carrier_ip"]
    return {
        "id":                row["id"],
        "providerId":        row["provider_id"],
        "displayName":       row["display_name"],
        "username":          row["username"],
        "hasPassword":       bool(row["password_enc"]),
        "fromUser":          row["from_user"],
        "fromDomain":        row["from_domain"],
        "channelLimit":      row["channel_limit"],
        "expirationSeconds": row["expiration_seconds"],
        "description":       row["description"],
        "enabled":           bool(row["account_enabled"]),
        "createdAt":         row["account_created_at"].isoformat() if row["account_created_at"] else None,
        "updatedAt":         row["account_updated_at"].isoformat() if row["account_updated_at"] else None,
        "provider": {
            "id":                row["p_id"],
            "displayName":       row["p_display_name"],
            "serverUri":         row["p_server_uri"],
            "transport":         row["p_transport"],
            "mode":              row["p_mode"],
            "carrierIp":         str(carrier_ip) if carrier_ip is not None else None,
            "defaultFromDomain": row["p_default_from_domain"],
            "defaultRealm":      row["p_default_realm"],
            "enabled":           bool(row["p_enabled"]),
        },
    }


def list_accounts(db_conn_factory) -> list[dict]:
    with db_conn_factory() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(_ACCOUNT_SELECT + " ORDER BY a.display_name, a.id")
            return [_row_to_account(r) for r in cur.fetchall()]


def get_account(db_conn_factory, account_id: str) -> dict:
    with db_conn_factory() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(_ACCOUNT_SELECT + " WHERE a.id = %s", (account_id,))
            row = cur.fetchone()
            if not row:
                raise _NotFound(f"account {account_id!r} not found")
            return _row_to_account(row)


def _build_sidecar_trunk_row(account_view: dict, plaintext_password: str) -> tuple[dict, str]:
    """Translate a joined account+provider view into the flat row shape
    that control_api._pjsip_upsert understands.

    Returns (row_for_pjsip_upsert, plaintext_password).
    """
    p = account_view["provider"]
    register_enabled = (p["mode"] == "register") and bool(p["enabled"])
    carrier_ip = p["carrierIp"]
    row = {
        "id":              account_view["id"],
        "display_name":    account_view["displayName"],
        "server_uri":      p["serverUri"],
        "username":        account_view["username"],
        "channel_limit":   account_view["channelLimit"],
        "enabled":         bool(account_view["enabled"]) and bool(p["enabled"]),
        "transport":       p["transport"],
        "context":         None,
        "client_uri":      None,
        "from_user":       account_view["fromUser"],
        "from_domain":     account_view["fromDomain"] or p["defaultFromDomain"],
        "expiration":      account_view["expirationSeconds"],
        "allow":           None,
        "outbound_auth":   None,
        "identify_by":     None,
        # New IP-trunk fields consumed by _pjsip_upsert.
        "register_enabled": register_enabled,
        "carrier_ip":       carrier_ip if not register_enabled else None,
    }
    return row, plaintext_password


def upsert_account(
    db_conn_factory,
    body: dict,
    pjsip_upsert,
    url_id: Optional[str] = None,
) -> dict:
    """Validate, encrypt-and-store the account, then push the joined view
    through pjsip_upsert. Raises StoreError on validation / DB problems.

    `pjsip_upsert` is a callable accepting (flat_row_dict, plaintext_password)
    matching control_api._pjsip_upsert's signature. Decoupling it here
    keeps this module unit-testable without importing control_api.
    """
    if url_id and "id" not in body:
        body = {**body, "id": url_id}
    cols, plaintext = validate_account_input(body, partial=False)

    # Resolve plaintext: if not provided, fetch the existing ciphertext
    # and decrypt so we can re-supply it to _pjsip_upsert.
    if plaintext is None:
        existing = None
        try:
            existing = get_account(db_conn_factory, cols["id"])
        except _NotFound:
            raise StoreError("password is required for a new trunk account")
        # Pull the raw ciphertext separately to decrypt; the projection
        # intentionally strips it.
        with db_conn_factory() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT password_enc FROM sip_trunk_accounts WHERE id = %s",
                (cols["id"],),
            )
            ct_row = cur.fetchone()
            ciphertext = ct_row[0] if ct_row else ""
        plaintext = decrypt_password(ciphertext)
        _ = existing  # silence linter

    ciphertext = encrypt_password(plaintext)

    with db_conn_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sip_trunk_accounts
                    (id, provider_id, display_name, username, password_enc,
                     from_user, from_domain, channel_limit, expiration_seconds,
                     description, enabled, updated_at)
                VALUES (%(id)s, %(provider_id)s, %(display_name)s, %(username)s,
                        %(password_enc)s, %(from_user)s, %(from_domain)s,
                        %(channel_limit)s, %(expiration_seconds)s,
                        %(description)s, %(enabled)s, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    provider_id        = EXCLUDED.provider_id,
                    display_name       = EXCLUDED.display_name,
                    username           = EXCLUDED.username,
                    password_enc       = EXCLUDED.password_enc,
                    from_user          = EXCLUDED.from_user,
                    from_domain        = EXCLUDED.from_domain,
                    channel_limit      = EXCLUDED.channel_limit,
                    expiration_seconds = EXCLUDED.expiration_seconds,
                    description        = EXCLUDED.description,
                    enabled            = EXCLUDED.enabled,
                    updated_at         = NOW()
                """,
                {
                    "id":                 cols["id"],
                    "provider_id":        cols["provider_id"],
                    "display_name":       cols["display_name"],
                    "username":           cols["username"],
                    "password_enc":       ciphertext,
                    "from_user":          cols.get("from_user"),
                    "from_domain":        cols.get("from_domain"),
                    "channel_limit":      cols["channel_limit"],
                    "expiration_seconds": cols["expiration_seconds"],
                    "description":        cols.get("description"),
                    "enabled":            cols.get("enabled", True),
                },
            )

    account_view = get_account(db_conn_factory, cols["id"])
    pjsip_row, pw = _build_sidecar_trunk_row(account_view, plaintext)
    pjsip_upsert(pjsip_row, pw)
    return account_view


def delete_account(db_conn_factory, account_id: str, pjsip_delete) -> None:
    with db_conn_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sip_trunk_accounts WHERE id = %s", (account_id,))
            removed = cur.rowcount > 0
    pjsip_delete(account_id)
    if not removed:
        raise _NotFound(f"account {account_id!r} not found")


__all__ = [
    "StoreError",
    "bootstrap",
    "encrypt_password",
    "decrypt_password",
    "list_providers",
    "get_provider",
    "upsert_provider",
    "delete_provider",
    "list_accounts",
    "get_account",
    "upsert_account",
    "delete_account",
]
