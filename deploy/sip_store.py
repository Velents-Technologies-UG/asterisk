#!/usr/bin/env python3
"""
SIP trunk store.

Owns one canonical Postgres table that sits next to Asterisk's PJSIP
realtime tables (ps_*) in the same database:

  sip_trunks  — one row per SIP credential set the carrier hands us.
                Modeled on the flat object shape carriers actually
                ship to operators, e.g. innocalls:

                    {
                      "name": "innov2",
                      "address": "cu622.sip.innocalls.net:50760",
                      "numbers": ["622101"],
                      "authUsername": "622101",
                      "authPassword": "************",
                      "protocol": "udp",
                      "mediaEncryption": "none"
                    }

                One object = one usable SIP. Operators that hold
                multiple SIPs (whether at the same carrier or different
                carriers) get one row per SIP — there is no shared
                "provider" parent. The earlier provider/account split
                turned out to be a leaky abstraction: two SIPs at the
                same carrier can live on different endpoints, different
                transports, different media policies, so the "shared
                parent" assumption silently routed REGISTERs at the
                wrong endpoint for days.

The agent-hub UI talks to this via the sidecar's /control/sip/trunks
route. On upsert, the sidecar feeds the row to _pjsip_upsert from
control_api.py — the same writer that has always handled ps_*.

The old sip_providers / sip_trunk_accounts tables are kept on disk for
one release as a migration source: bootstrap() reads them, collapses
each (provider, account) pair into the new flat shape, and inserts
into sip_trunks. After one deploy the old tables can be dropped.

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
import hashlib
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

# ── identifiers ──────────────────────────────────

# Provider ids are lowercase + digits + dash (mirrors how we slug them
# in UI URLs). 1-40 chars keeps room for things like 'innocalls-eu'.
_SAFE_PROVIDER_ID = re.compile(r"^[a-z0-9-]{1,40}$")

# Account ids mirror the existing PJSIP endpoint id rule because that's
# what they become at the realtime layer. Permissive on case + underscore
# so users can call them 'inno-calls-saudi', 'TwilioMain', etc.
_SAFE_ACCOUNT_ID = re.compile(r"^[a-zA-Z0-9_-]{1,60}$")

_VALID_TRANSPORTS  = {"udp", "tcp", "tls"}
_VALID_MEDIA       = {"none", "sdes"}
_VALID_MODES       = {"register", "ip-trunk"}  # legacy provider table

# Trunk ids slug from `name`. Permissive on case + underscore so users
# can paste a carrier-given name like "InnoCalls_Saudi" or "innov2"
# without trial-and-error renaming.
_SAFE_TRUNK_ID = re.compile(r"^[a-zA-Z0-9_-]{1,60}$")

# Tenant identifier any legacy / un-tagged row falls back to. velentsAgents
# (Laravel) issues real tenant ids via stancl/tenancy; this sentinel only
# exists so the NOT NULL migration is non-destructive on a single-tenant
# database. Backfill all 'default' rows to the real tenant id before
# onboarding a second tenant.
DEFAULT_TENANT_ID = "default"


def safe_tenant_prefix(tenant_id: str) -> str:
    """Compress an arbitrary tenant_id into 8 [a-z0-9] chars.

    Used to build PJSIP realtime row ids that are safe across every
    Asterisk-canonical column type (ps_*.id is VARCHAR(40) on stock
    schemas; padding the prefix keeps the total under that ceiling
    even with a 60-char trunk slug after _DDL_PJSIP_ID_WIDEN runs).
    Same input always yields the same prefix, so a tenant's endpoints
    are stable across pod restarts.
    """
    s = str(tenant_id or "").strip()
    if not s:
        return "default0"
    if re.fullmatch(r"[a-z0-9]{1,8}", s):
        return s
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:8]


def pjsip_trunk_endpoint_id(tenant_id: str, trunk_id: str) -> str:
    """PJSIP realtime row id (ps_endpoints.id etc.) for a trunk.

    Prefixing with the tenant prevents two tenants both naming a trunk
    'innov2' from clobbering each other's ps_endpoints row. The
    user-facing sip_trunks.id stays 'innov2'; only the realtime layer
    sees the namespaced form.
    """
    return f"t{safe_tenant_prefix(tenant_id)}_{trunk_id}"


def pjsip_agent_endpoint_id(tenant_id: str, agent_id) -> str:
    """PJSIP realtime row id for a per-agent WebRTC endpoint.

    Agent ids in velentsAgents are scoped per-tenant (tenant A's agent
    id 2 is a different person from tenant B's agent id 2), so the
    realtime row id has to encode both.
    """
    return f"staff_t{safe_tenant_prefix(tenant_id)}_{agent_id}"


class StoreError(RuntimeError):
    """Raised for validation, encryption, or persistence problems."""


class _NotFound(StoreError):
    pass


# ── schema ─────────────────────────────────────

# sip_trunks: canonical carrier-credential row. One row = one usable
# SIP. `address` carries the host (and optional :port) the carrier told
# us to talk to; `protocol` selects the PJSIP transport; `numbers` is
# the DID list the carrier assigned to this account. `register=true`
# means we authenticate with REGISTER; `register=false` means the
# carrier identifies us by IP (carrier_ip becomes the match for
# ps_identify). `media_encryption` defaults to 'none' because every
# carrier we've onboarded ships SRTP off by default; SDES is opt-in.
_DDL_TRUNKS = r"""
CREATE TABLE IF NOT EXISTS sip_trunks (
    tenant_id              TEXT NOT NULL DEFAULT 'default',
    id                     TEXT NOT NULL,
    name                   TEXT NOT NULL,
    address                TEXT NOT NULL,
    protocol               TEXT NOT NULL DEFAULT 'udp',
    media_encryption       TEXT NOT NULL DEFAULT 'none',
    auth_username          TEXT NOT NULL,
    auth_password_enc      TEXT NOT NULL DEFAULT '',
    numbers                TEXT[] NOT NULL DEFAULT '{}',
    from_user              TEXT,
    from_domain            TEXT,
    realm                  TEXT,
    register_enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    carrier_ip             INET,
    channel_limit          INTEGER NOT NULL DEFAULT 50,
    expiration_seconds     INTEGER NOT NULL DEFAULT 3600,
    description            TEXT,
    enabled                BOOLEAN NOT NULL DEFAULT TRUE,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- (tenant_id, id) lets tenants share a trunk slug; the PJSIP
    -- realtime layer disambiguates via pjsip_trunk_endpoint_id().
    PRIMARY KEY (tenant_id, id),
    CONSTRAINT sip_trunks_id_format
        CHECK (id ~ '^[a-zA-Z0-9_-]{1,60}$'),
    CONSTRAINT sip_trunks_protocol_valid
        CHECK (protocol IN ('udp','tcp','tls')),
    CONSTRAINT sip_trunks_media_valid
        CHECK (media_encryption IN ('none','sdes')),
    CONSTRAINT sip_trunks_channel_limit_range
        CHECK (channel_limit BETWEEN 1 AND 1000),
    CONSTRAINT sip_trunks_expiration_range
        CHECK (expiration_seconds BETWEEN 60 AND 86400),
    CONSTRAINT sip_trunks_ip_required_for_ip_trunk
        CHECK (register_enabled OR carrier_ip IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS sip_trunks_tenant_idx ON sip_trunks(tenant_id);
"""

# Migration patch for pre-existing single-tenant deployments. Idempotent:
# ADD COLUMN IF NOT EXISTS is a no-op once tenant_id is there; the
# constraint switch runs only if the single-column PK is still in place.
_DDL_TRUNKS_TENANT_MIGRATION = r"""
ALTER TABLE sip_trunks ADD COLUMN IF NOT EXISTS tenant_id TEXT;
UPDATE sip_trunks SET tenant_id = 'default' WHERE tenant_id IS NULL;
ALTER TABLE sip_trunks ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE sip_trunks ALTER COLUMN tenant_id SET DEFAULT 'default';
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'sip_trunks_pkey'
          AND conrelid = 'sip_trunks'::regclass
          AND array_length(conkey, 1) = 1
    ) THEN
        ALTER TABLE sip_trunks DROP CONSTRAINT sip_trunks_pkey;
        ALTER TABLE sip_trunks ADD PRIMARY KEY (tenant_id, id);
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS sip_trunks_tenant_idx ON sip_trunks(tenant_id);
"""

# Stock Asterisk realtime tables size *.id at VARCHAR(40), which is
# tight once we prefix with the tenant: `t<8>_<60>` = 70 chars. Widen
# to 190 (matching the rest of Asterisk's contrib alembic) so the
# namespaced ids fit. Idempotent — ALTER ... TYPE VARCHAR(190) is a
# no-op if the column is already wider, and the inner try/except in
# bootstrap() swallows the case where the column doesn't exist at all
# (stripped-down realtime schemas).
_DDL_PJSIP_ID_WIDEN = r"""
ALTER TABLE ps_endpoints     ALTER COLUMN id TYPE VARCHAR(190);
ALTER TABLE ps_aors          ALTER COLUMN id TYPE VARCHAR(190);
ALTER TABLE ps_auths         ALTER COLUMN id TYPE VARCHAR(190);
ALTER TABLE ps_registrations ALTER COLUMN id TYPE VARCHAR(190);
"""

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
# our two writers (control_api._pjsip_upsert for trunks, control_api.
# _provision_agent for per-agent WebRTC endpoints) populate.
# ADD COLUMN IF NOT EXISTS is idempotent on a fresh contrib schema or
# a hand-rolled subset. Asterisk's full ps_endpoints schema ships >40
# columns; the ones below cover both the trunk-side fields and the
# per-agent WebRTC fields. Without this patch, a stripped-schema deploy
# fails silently:
#
#   - Trunk side: outbound INVITEs go out anonymous / no PAI, carrier
#     returns 403 Forbidden.
#   - Agent side: _provision_agent throws "column XXX does not exist",
#     the /control/sip/agents/<id>/credentials endpoint 500s,
#     the browser softphone never registers, the dialpad does nothing.
#
# Setting ALL these columns to nullable VARCHAR(3) (for yes/no flags)
# or VARCHAR(40) (for short ids) is consistent with Asterisk's stock
# alembic migrations.
_DDL_PS_ENDPOINTS_PATCH = r"""
-- Trunk-side carrier-compat columns
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS from_user                VARCHAR(40);
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS from_domain              VARCHAR(190);
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS callerid                 VARCHAR(40);
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS trust_id_outbound        VARCHAR(3);
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS send_pai                 VARCHAR(3);
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS send_rpid                VARCHAR(3);
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS direct_media             VARCHAR(3);
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS rewrite_contact          VARCHAR(3);
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS rtp_symmetric            VARCHAR(3);
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS force_rport              VARCHAR(3);

-- Per-agent WebRTC columns (control_api._provision_agent writes these)
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS ice_support              VARCHAR(3);
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS use_avpf                 VARCHAR(3);
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS rtcp_mux                 VARCHAR(3);
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS media_encryption         VARCHAR(40);
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS dtls_verify              VARCHAR(40);
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS dtls_setup               VARCHAR(40);
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS dtls_auto_generate_cert  VARCHAR(3);
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS webrtc                   VARCHAR(3);
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS allow_subscribe          VARCHAR(3);

-- Custom column we tag agent endpoints with so the status feeder /
-- queue dispatcher can find them. Not a stock Asterisk column.
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS agent_id                 VARCHAR(60);

-- Updated_at is used by both writers' ON CONFLICT clauses. Asterisk
-- doesn't care about it but realtime won't error if it exists.
ALTER TABLE ps_endpoints ADD COLUMN IF NOT EXISTS updated_at               TIMESTAMPTZ;
"""

# ps_aors columns the per-agent WebRTC provisioning path writes.
# remove_existing makes a fresh REGISTER from a refreshed softphone
# replace the stale contact instead of leaving two on the same AOR
# (would otherwise cause click-to-dial to dial the wrong WebSocket).
# support_path advertises the Path header so the softphone can
# register through a proxy. Both are standard Asterisk 22 ps_aors
# columns; the stripped-schema deploys that lack the ps_endpoints
# carrier-compat columns generally also lack these.
_DDL_PS_AORS_PATCH = r"""
ALTER TABLE ps_aors ADD COLUMN IF NOT EXISTS remove_existing VARCHAR(3);
ALTER TABLE ps_aors ADD COLUMN IF NOT EXISTS support_path    VARCHAR(3);
"""

# Seed the one provider we actively need so the UI has a selectable
# carrier on first boot. ON CONFLICT keeps re-seeding idempotent and
# preserves any operator edits.
#
# Server URI / transport reflect the spec the carrier accepts in
# production (TCP:50760, register mode). Earlier seed used TLS:5061
# which the carrier 403s on outbound INVITE.
_SEED_INNOCALLS = r"""
INSERT INTO sip_providers
    (id, display_name, description, server_uri, transport, mode,
     carrier_ip, default_from_domain)
VALUES
    ('innocalls', 'innocalls',
     'innocalls wholesale termination',
     'sip:cu622.sip.innocalls.net:50760', 'tcp', 'register',
     NULL, 'cu622.sip.innocalls.net')
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

    Schema patch uses autocommit so each ALTER persists even if a
    later one fails — the earlier version wrapped them all in a
    single transaction and one missing column rolled the whole patch
    back.
    """
    if not HAS_PSYCOPG2:
        log.warning("sip_store.bootstrap: psycopg2 unavailable; skipping")
        return
    try:
        with db_conn_factory() as conn, conn.cursor() as cur:
            # Legacy provider+account tables are kept for one release as
            # a migration source. CREATE IF NOT EXISTS is idempotent;
            # they're read-only from here on.
            cur.execute(_DDL_PROVIDERS)
            cur.execute(_DDL_ACCOUNTS)
            # Canonical carrier-credential table.
            cur.execute(_DDL_TRUNKS)
            # Tenant migration for sip_trunks pre-existing the
            # tenant_id column. Safe on a fresh DB too — every step is
            # IF NOT EXISTS / IF the old PK is still in place.
            cur.execute(_DDL_TRUNKS_TENANT_MIGRATION)
            cur.execute(_SEED_INNOCALLS)
        log.info(
            "sip_store.bootstrap: sip_trunks ensured + tenant_id migration applied"
        )
    except Exception as exc:
        log.error("sip_store.bootstrap (trunks/providers/accounts) failed: %s", exc)

    # Widen ps_*.id from VARCHAR(40) to VARCHAR(190) so namespaced ids
    # (tXXXXXXXX_<60>) fit. Same autocommit/per-statement pattern as
    # _DDL_PS_ENDPOINTS_PATCH so a missing table doesn't roll back the
    # rest of bootstrap.
    try:
        conn = db_conn_factory()
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                for stmt in _DDL_PJSIP_ID_WIDEN.split(";"):
                    s = stmt.strip()
                    if not s or s.startswith("--"):
                        continue
                    try:
                        cur.execute(s)
                    except Exception as inner_exc:
                        log.warning(
                            "sip_store.bootstrap (pjsip id widen %r): %s",
                            s[:80], inner_exc,
                        )
        finally:
            conn.close()
        log.info(
            "sip_store.bootstrap: ps_*.id widened to VARCHAR(190) for tenant-namespaced ids"
        )
    except Exception as exc:
        log.warning(
            "sip_store.bootstrap (pjsip id widen) failed: %s — "
            "tenant-namespaced PJSIP endpoint ids may be truncated",
            exc,
        )

    # Copy any (provider, account) pairs that haven't been migrated yet
    # into sip_trunks. Idempotent: ON CONFLICT (id) DO NOTHING keeps
    # operator edits made via the new API from being clobbered on
    # restart. Logged separately so a migration error doesn't keep the
    # ps_endpoints patch from running.
    try:
        n = migrate_legacy_to_trunks(db_conn_factory)
        if n:
            log.info(
                "sip_store.bootstrap: migrated %d legacy account(s) into sip_trunks", n
            )
    except Exception as exc:
        log.warning("sip_store.bootstrap (legacy→trunks migration) failed: %s", exc)

    # Apply each ALTER independently with autocommit so a single
    # already-present column (or a permission glitch) can't roll back
    # the entire patch.
    try:
        conn = db_conn_factory()
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                for stmt in _DDL_PS_ENDPOINTS_PATCH.split(";"):
                    s = stmt.strip()
                    if not s or s.startswith("--"):
                        continue
                    try:
                        cur.execute(s)
                    except Exception as inner_exc:
                        log.warning(
                            "sip_store.bootstrap (ps_endpoints patch stmt %r): %s",
                            s[:80], inner_exc,
                        )
        finally:
            conn.close()
        log.info(
            "sip_store.bootstrap: ps_endpoints schema patched (carrier-compat + WebRTC columns)"
        )
    except Exception as exc:
        log.warning(
            "sip_store.bootstrap (ps_endpoints patch) failed: %s — "
            "outbound INVITEs may be 403'd by carriers AND agent softphones "
            "may fail to register if the WebRTC columns are missing",
            exc,
        )

    # Same idempotent / autocommit pattern, isolated so a ps_aors
    # permission issue can't break the rest of bootstrap. Without
    # remove_existing / support_path the agent-provisioning INSERT in
    # control_api._provision_agent fails with "column does not exist"
    # and the softphone never registers.
    try:
        conn = db_conn_factory()
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                for stmt in _DDL_PS_AORS_PATCH.split(";"):
                    s = stmt.strip()
                    if not s or s.startswith("--"):
                        continue
                    try:
                        cur.execute(s)
                    except Exception as inner_exc:
                        log.warning(
                            "sip_store.bootstrap (ps_aors patch stmt %r): %s",
                            s[:80], inner_exc,
                        )
        finally:
            conn.close()
        log.info(
            "sip_store.bootstrap: ps_aors schema patched (remove_existing + support_path)"
        )
    except Exception as exc:
        log.warning(
            "sip_store.bootstrap (ps_aors patch) failed: %s — "
            "agent softphone provisioning may 500 until DevOps adds "
            "remove_existing/support_path to ps_aors",
            exc,
        )


# ── crypto ────────────────────────────────────

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


# ── validation ──────────────────────────────────

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


# ── CRUD: providers ────────────────────────────────

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


# ── CRUD: trunk accounts ─────────────────────────────

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


# ── CRUD: sip_trunks (canonical) ─────────────────────

# The single normalize+validate pass over a carrier-JSON-shaped body.
# Accepts both the carrier-native keys (authUsername, authPassword,
# mediaEncryption) and the UI-friendly snake_case the older legacy
# routes used (auth_username, …) so a paste-from-JSON button can dump
# the raw object straight through.
def validate_trunk_input(body: dict, partial: bool = False) -> tuple[dict, Optional[str]]:
    """Return (column->value, plaintext_password_or_None).

    Same contract as validate_account_input: the password is returned
    separately so we encrypt at write-time. partial=True relaxes the
    'required' checks for PATCH-style updates.
    """
    def pick(*keys):
        for k in keys:
            if k in body:
                return body.get(k)
        return None

    out: dict = {}

    if not partial or pick("id", "name") is not None:
        raw_id = _str_or_none(body.get("id")) or _str_or_none(body.get("name"))
        if not raw_id:
            raise StoreError("id (or name) is required")
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw_id).strip("-")
        if not slug or not _SAFE_TRUNK_ID.match(slug):
            raise StoreError(
                "id slug must be 1-60 chars of a-z, A-Z, 0-9, '_' or '-'"
            )
        out["id"] = slug

    if not partial or "name" in body:
        name = _str_or_none(body.get("name")) or out.get("id")
        if not name:
            raise StoreError("name is required")
        out["name"] = name

    if not partial or "address" in body:
        addr = _str_or_none(body.get("address"))
        if not addr:
            raise StoreError("address is required (host or host:port)")
        # Strip a leading "sip:" / "sips:" if the operator pasted one —
        # the carrier JSON ships bare host[:port], but UI / legacy
        # surfaces may pass a full URI.
        if addr.startswith("sip:") or addr.startswith("sips:"):
            addr = addr.split(":", 1)[1]
        out["address"] = addr

    if not partial or "protocol" in body:
        proto = (pick("protocol", "transport") or "udp").lower()
        if proto not in _VALID_TRANSPORTS:
            raise StoreError(
                f"protocol must be one of: {sorted(_VALID_TRANSPORTS)}"
            )
        out["protocol"] = proto

    if "mediaEncryption" in body or "media_encryption" in body or not partial:
        me = (pick("mediaEncryption", "media_encryption") or "none").lower()
        # Asterisk accepts "no" as a synonym for "none" on the
        # media_encryption column; normalize here so DB stays canonical.
        if me in ("no", "off", "disabled"):
            me = "none"
        if me not in _VALID_MEDIA:
            raise StoreError(
                f"mediaEncryption must be one of: {sorted(_VALID_MEDIA)}"
            )
        out["media_encryption"] = me

    if not partial or pick("authUsername", "auth_username", "username") is not None:
        au = _str_or_none(pick("authUsername", "auth_username", "username"))
        if not au:
            raise StoreError("authUsername is required")
        out["auth_username"] = au

    if "numbers" in body:
        numbers = body.get("numbers")
        if numbers is None:
            numbers = []
        if not isinstance(numbers, list):
            raise StoreError("numbers must be an array of strings")
        norm = []
        for n in numbers:
            s = _str_or_none(n)
            if s:
                norm.append(s)
        out["numbers"] = norm

    if "fromUser" in body or "from_user" in body:
        out["from_user"] = _str_or_none(pick("fromUser", "from_user"))
    if "fromDomain" in body or "from_domain" in body:
        out["from_domain"] = _str_or_none(pick("fromDomain", "from_domain"))
    if "realm" in body:
        out["realm"] = _str_or_none(body.get("realm"))
    if "description" in body:
        out["description"] = _str_or_none(body.get("description"))

    if "registerEnabled" in body or "register_enabled" in body or not partial:
        v = pick("registerEnabled", "register_enabled")
        if v is None:
            v = True
        if not isinstance(v, bool):
            raise StoreError("registerEnabled must be a boolean")
        out["register_enabled"] = v

    if "carrierIp" in body or "carrier_ip" in body:
        out["carrier_ip"] = _str_or_none(pick("carrierIp", "carrier_ip"))

    if "channelLimit" in body or "channel_limit" in body or not partial:
        try:
            cl = int(pick("channelLimit", "channel_limit") or 50)
        except (TypeError, ValueError):
            raise StoreError("channelLimit must be an integer")
        if not 1 <= cl <= 1000:
            raise StoreError("channelLimit must be 1..1000")
        out["channel_limit"] = cl

    if "expirationSeconds" in body or "expiration_seconds" in body \
            or "expiration" in body or not partial:
        v = pick("expirationSeconds", "expiration_seconds", "expiration")
        if v is None:
            v = 3600
        try:
            es = int(v)
        except (TypeError, ValueError):
            raise StoreError("expirationSeconds must be an integer")
        if not 60 <= es <= 86400:
            raise StoreError("expirationSeconds must be 60..86400")
        out["expiration_seconds"] = es

    if "enabled" in body:
        out["enabled"] = _bool_or_default(body.get("enabled"), True)

    final_register = out.get("register_enabled", True)
    final_ip = out.get("carrier_ip")
    if not final_register and not final_ip and not partial:
        raise StoreError("carrierIp is required when registerEnabled is false")

    pw_field = pick("authPassword", "auth_password", "password")
    plaintext: Optional[str] = None
    if pw_field is not None:
        plaintext = str(pw_field)
        if not partial and not plaintext:
            raise StoreError("authPassword is required for a new trunk")

    return out, plaintext


_TRUNK_COLUMNS = (
    "tenant_id", "id", "name", "address", "protocol", "media_encryption",
    "auth_username", "auth_password_enc", "numbers",
    "from_user", "from_domain", "realm",
    "register_enabled", "carrier_ip",
    "channel_limit", "expiration_seconds",
    "description", "enabled", "created_at", "updated_at",
)


def _row_to_trunk(row) -> dict:
    """Cast a DictRow into the API shape (carrier-JSON keys, no ciphertext)."""
    carrier_ip = row["carrier_ip"]
    return {
        "tenantId":         row["tenant_id"],
        "id":               row["id"],
        "name":             row["name"],
        "address":          row["address"],
        "protocol":         row["protocol"],
        "mediaEncryption":  row["media_encryption"],
        "authUsername":     row["auth_username"],
        "hasPassword":      bool(row["auth_password_enc"]),
        "numbers":          list(row["numbers"] or []),
        "fromUser":         row["from_user"],
        "fromDomain":       row["from_domain"],
        "realm":            row["realm"],
        "registerEnabled":  bool(row["register_enabled"]),
        "carrierIp":        str(carrier_ip) if carrier_ip is not None else None,
        "channelLimit":     row["channel_limit"],
        "expirationSeconds": row["expiration_seconds"],
        "description":      row["description"],
        "enabled":          bool(row["enabled"]),
        "createdAt":        row["created_at"].isoformat() if row["created_at"] else None,
        "updatedAt":        row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def _require_tenant(tenant_id):
    """Reject empty / None tenant ids loudly.

    Trunk routes that forget to pass a tenant id are a security bug:
    silently defaulting to 'default' would let one tenant read or
    write another's trunks. The handler is responsible for translating
    a missing session-tenant into a 401 long before we get here.
    """
    if not tenant_id:
        raise StoreError("tenant_id is required")
    return str(tenant_id)


def list_trunks(db_conn_factory, tenant_id: str) -> list[dict]:
    tenant_id = _require_tenant(tenant_id)
    with db_conn_factory() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                f"SELECT {', '.join(_TRUNK_COLUMNS)} "
                "FROM sip_trunks WHERE tenant_id = %s ORDER BY name, id",
                (tenant_id,),
            )
            return [_row_to_trunk(r) for r in cur.fetchall()]


def get_trunk(db_conn_factory, tenant_id: str, trunk_id: str) -> dict:
    tenant_id = _require_tenant(tenant_id)
    with db_conn_factory() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                f"SELECT {', '.join(_TRUNK_COLUMNS)} "
                "FROM sip_trunks WHERE tenant_id = %s AND id = %s",
                (tenant_id, trunk_id),
            )
            row = cur.fetchone()
            if not row:
                raise _NotFound(f"trunk {trunk_id!r} not found")
            return _row_to_trunk(row)


def _trunk_to_pjsip_row(trunk: dict, plaintext_password: str) -> dict:
    """Project a canonical trunk into the flat row shape that
    control_api._pjsip_upsert consumes. Defaults that depend on the
    address (host, port, from_domain) are derived here so the writer
    stays carrier-agnostic.

    `trunk` is the camelCase API view returned by get_trunk / _row_to_trunk
    — that's what upsert_trunk hands us. Read the camelCase keys; the
    snake_case fields are only in the raw DB row.

    The `id` we hand to _pjsip_upsert is the tenant-namespaced PJSIP
    endpoint id, NOT the user-facing trunk slug. The realtime tables
    share one namespace across all tenants; without the prefix two
    tenants both naming a trunk 'innov2' would clobber each other's
    ps_endpoints row.
    """
    host = (trunk["address"] or "").split(":", 1)[0]
    # Build the server URI from address + transport so _pick_transport
    # in control_api falls through to the explicit transport we pass.
    server_uri = f"sip:{trunk['address']}"
    numbers = trunk.get("numbers") or []
    # callerid is built by control_api from `from_user`; the carrier
    # expects the DID. If the operator didn't override, the first
    # assigned number is the DID; absent any number, fall back to the
    # auth username so we at least don't go out "Anonymous".
    from_user = trunk.get("fromUser") or (numbers[0] if numbers else None) \
        or trunk["authUsername"]
    register_enabled = bool(trunk.get("registerEnabled", True))
    carrier_ip = trunk.get("carrierIp")
    tenant_id = trunk.get("tenantId") or DEFAULT_TENANT_ID
    pjsip_id = pjsip_trunk_endpoint_id(tenant_id, trunk["id"])
    return {
        "id":               pjsip_id,
        "display_name":     trunk["name"],
        "server_uri":       server_uri,
        "username":         trunk["authUsername"],
        "channel_limit":    trunk["channelLimit"],
        "enabled":          bool(trunk.get("enabled", True)),
        "transport":        trunk["protocol"],
        "context":          None,
        "client_uri":       None,
        "from_user":        from_user,
        "from_domain":      trunk.get("fromDomain") or host,
        "expiration":       trunk["expirationSeconds"],
        "allow":            None,
        "outbound_auth":    None,
        "identify_by":      None,
        "register_enabled": register_enabled,
        "carrier_ip":       carrier_ip if not register_enabled else None,
        # Carried through to ps_endpoints.media_encryption. 'none' means
        # plain RTP; 'sdes' means SDES-SRTP. _pjsip_upsert maps 'none'
        # to the Asterisk-native 'no' on write.
        "media_encryption": trunk.get("mediaEncryption", "none"),
    }


def upsert_trunk(
    db_conn_factory,
    tenant_id: str,
    body: dict,
    pjsip_upsert,
    url_id: Optional[str] = None,
) -> dict:
    """Validate + persist the trunk row, then push it through pjsip_upsert.

    pjsip_upsert is a callable matching control_api._pjsip_upsert's
    (flat_row_dict, plaintext_password) signature. Decoupled here so
    this module is unit-testable without importing control_api.

    tenant_id comes from the request handler — never from the request
    body, which a hostile client could spoof to write trunks into
    another tenant's namespace.
    """
    tenant_id = _require_tenant(tenant_id)
    if url_id and "id" not in body and "name" not in body:
        body = {**body, "id": url_id}
    cols, plaintext = validate_trunk_input(body, partial=False)

    # If no password was supplied, reuse the existing ciphertext (PATCH
    # semantics — the UI shouldn't have to re-enter the password to
    # rename a trunk).
    if plaintext is None:
        try:
            existing_view = get_trunk(db_conn_factory, tenant_id, cols["id"])
        except _NotFound:
            raise StoreError("authPassword is required for a new trunk")
        with db_conn_factory() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT auth_password_enc FROM sip_trunks "
                "WHERE tenant_id = %s AND id = %s",
                (tenant_id, cols["id"]),
            )
            r = cur.fetchone()
            ciphertext = r[0] if r else ""
        plaintext = decrypt_password(ciphertext)
        _ = existing_view

    ciphertext = encrypt_password(plaintext)

    with db_conn_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sip_trunks
                    (tenant_id, id, name, address, protocol, media_encryption,
                     auth_username, auth_password_enc, numbers,
                     from_user, from_domain, realm,
                     register_enabled, carrier_ip,
                     channel_limit, expiration_seconds,
                     description, enabled, updated_at)
                VALUES (%(tenant_id)s, %(id)s, %(name)s, %(address)s, %(protocol)s,
                        %(media_encryption)s, %(auth_username)s,
                        %(auth_password_enc)s, %(numbers)s,
                        %(from_user)s, %(from_domain)s, %(realm)s,
                        %(register_enabled)s, %(carrier_ip)s,
                        %(channel_limit)s, %(expiration_seconds)s,
                        %(description)s, %(enabled)s, NOW())
                ON CONFLICT (tenant_id, id) DO UPDATE SET
                    name               = EXCLUDED.name,
                    address            = EXCLUDED.address,
                    protocol           = EXCLUDED.protocol,
                    media_encryption   = EXCLUDED.media_encryption,
                    auth_username      = EXCLUDED.auth_username,
                    auth_password_enc  = EXCLUDED.auth_password_enc,
                    numbers            = EXCLUDED.numbers,
                    from_user          = EXCLUDED.from_user,
                    from_domain        = EXCLUDED.from_domain,
                    realm              = EXCLUDED.realm,
                    register_enabled   = EXCLUDED.register_enabled,
                    carrier_ip         = EXCLUDED.carrier_ip,
                    channel_limit      = EXCLUDED.channel_limit,
                    expiration_seconds = EXCLUDED.expiration_seconds,
                    description        = EXCLUDED.description,
                    enabled            = EXCLUDED.enabled,
                    updated_at         = NOW()
                """,
                {
                    "tenant_id":          tenant_id,
                    "id":                 cols["id"],
                    "name":               cols["name"],
                    "address":            cols["address"],
                    "protocol":           cols["protocol"],
                    "media_encryption":   cols["media_encryption"],
                    "auth_username":      cols["auth_username"],
                    "auth_password_enc":  ciphertext,
                    "numbers":            cols.get("numbers", []),
                    "from_user":          cols.get("from_user"),
                    "from_domain":        cols.get("from_domain"),
                    "realm":              cols.get("realm"),
                    "register_enabled":   cols.get("register_enabled", True),
                    "carrier_ip":         cols.get("carrier_ip"),
                    "channel_limit":      cols["channel_limit"],
                    "expiration_seconds": cols["expiration_seconds"],
                    "description":        cols.get("description"),
                    "enabled":            cols.get("enabled", True),
                },
            )

    view = get_trunk(db_conn_factory, tenant_id, cols["id"])
    pjsip_row = _trunk_to_pjsip_row(view, plaintext)
    pjsip_upsert(pjsip_row, plaintext)
    return view


def delete_trunk(
    db_conn_factory, tenant_id: str, trunk_id: str, pjsip_delete,
) -> None:
    tenant_id = _require_tenant(tenant_id)
    with db_conn_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM sip_trunks WHERE tenant_id = %s AND id = %s",
                (tenant_id, trunk_id),
            )
            removed = cur.rowcount > 0
    # pjsip_delete takes the realtime row id (namespaced), not the
    # user-facing slug. Compute it here so callers don't have to.
    pjsip_delete(pjsip_trunk_endpoint_id(tenant_id, trunk_id))
    if not removed:
        raise _NotFound(f"trunk {trunk_id!r} not found")


# ── one-shot migration: legacy provider+account → sip_trunks ─────

def migrate_legacy_to_trunks(db_conn_factory) -> int:
    """Copy any (provider, account) pairs not yet present in sip_trunks.

    Returns the number of rows inserted. Idempotent: rows already in
    sip_trunks are left untouched. Skips silently if the legacy tables
    don't exist (fresh deploy on the new schema).
    """
    if not HAS_PSYCOPG2:
        return 0
    inserted = 0
    with db_conn_factory() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            try:
                cur.execute("""
                    SELECT a.id                AS account_id,
                           a.display_name      AS display_name,
                           a.username          AS username,
                           a.password_enc      AS password_enc,
                           a.from_user         AS from_user,
                           a.from_domain       AS from_domain,
                           a.channel_limit     AS channel_limit,
                           a.expiration_seconds AS expiration_seconds,
                           a.description       AS description,
                           a.enabled           AS account_enabled,
                           p.server_uri        AS server_uri,
                           p.transport         AS transport,
                           p.mode              AS mode,
                           p.carrier_ip        AS carrier_ip,
                           p.default_from_domain AS default_from_domain,
                           p.default_realm     AS default_realm,
                           p.enabled           AS provider_enabled
                    FROM sip_trunk_accounts a
                    JOIN sip_providers p ON p.id = a.provider_id
                """)
                legacy = cur.fetchall()
            except psycopg2.Error:
                # Legacy tables don't exist — fresh deploy.
                return 0

            for r in legacy:
                # The legacy server_uri may be sip:host[:port] or just
                # host[:port]; strip the scheme for the new shape.
                addr = (r["server_uri"] or "").strip()
                if addr.startswith("sip:") or addr.startswith("sips:"):
                    addr = addr.split(":", 1)[1]
                if not addr:
                    log.warning(
                        "legacy account %s has no server_uri on its provider; skipping",
                        r["account_id"],
                    )
                    continue
                # Legacy rows have no tenant — fall through to the
                # 'default' column default so the NOT NULL constraint
                # is satisfied. Single-tenant deployments stay
                # functional; multi-tenant deployments need to
                # re-assign these rows post-migration.
                cur.execute(
                    """
                    INSERT INTO sip_trunks
                        (id, name, address, protocol, media_encryption,
                         auth_username, auth_password_enc, numbers,
                         from_user, from_domain, realm,
                         register_enabled, carrier_ip,
                         channel_limit, expiration_seconds,
                         description, enabled)
                    VALUES (%s, %s, %s, %s, 'none',
                            %s, %s, '{}',
                            %s, %s, %s,
                            %s, %s,
                            %s, %s,
                            %s, %s)
                    ON CONFLICT (tenant_id, id) DO NOTHING
                    """,
                    (
                        r["account_id"],
                        r["display_name"] or r["account_id"],
                        addr,
                        r["transport"] or "udp",
                        r["username"],
                        r["password_enc"] or "",
                        r["from_user"],
                        r["from_domain"] or r["default_from_domain"],
                        r["default_realm"],
                        (r["mode"] or "register") == "register",
                        str(r["carrier_ip"]) if r["carrier_ip"] is not None else None,
                        r["channel_limit"] or 50,
                        r["expiration_seconds"] or 3600,
                        r["description"],
                        bool(r["account_enabled"]) and bool(r["provider_enabled"]),
                    ),
                )
                inserted += cur.rowcount
    return inserted


__all__ = [
    "StoreError",
    "bootstrap",
    "encrypt_password",
    "decrypt_password",
    # tenant helpers
    "DEFAULT_TENANT_ID",
    "safe_tenant_prefix",
    "pjsip_trunk_endpoint_id",
    "pjsip_agent_endpoint_id",
    # canonical trunk API
    "list_trunks",
    "get_trunk",
    "upsert_trunk",
    "delete_trunk",
    "validate_trunk_input",
    "migrate_legacy_to_trunks",
    # legacy (read-only, kept for migration window)
    "list_providers",
    "get_provider",
    "upsert_provider",
    "delete_provider",
    "list_accounts",
    "get_account",
    "upsert_account",
    "delete_account",
]
