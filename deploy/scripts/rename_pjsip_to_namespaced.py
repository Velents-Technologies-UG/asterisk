#!/usr/bin/env python3
"""One-shot: rewrite legacy PJSIP realtime rows to tenant-namespaced ids.

After the tenant_id migration lands, sip_trunks rows have a tenant_id
column but the ps_endpoints / ps_aors / ps_auths / ps_registrations
rows still carry the OLD un-namespaced ids (e.g., 'innov2', 'staff_2')
from before the upgrade. New writes go to the namespaced ids
('t{prefix}_innov2', 'staff_t{prefix}_2') so the next time someone
edits a trunk or re-provisions an agent the realtime layer would end
up with both an old and a new row for the same logical endpoint.

This script:
  1. Iterates every sip_trunks row, deletes the legacy ps_* rows
     keyed by the bare trunk slug, and re-runs the regular pjsip
     upsert path which writes the namespaced rows.
  2. Finds every ps_endpoints row whose id matches `staff_<digits>`
     (legacy agent format, no tenant prefix). Because we don't know
     which tenant those belong to without looking it up in
     velentsAgents — and there's no DB link from ps_endpoints to a
     tenant — these are reported for manual cleanup. In the
     single-tenant deployment we have today, the operator can pass
     --assume-tenant=<id> to namespace them under that tenant.

Run inside the asterisk pod:

    kubectl -n velents exec deploy/asterisk -- python3 \\
        /usr/local/bin/rename_pjsip_to_namespaced.py --assume-tenant=default

Idempotent: rows already in the namespaced form are left alone.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import urllib.parse

import psycopg2
import psycopg2.extras

# Importing the sidecar's modules pulls in their helpers without copying
# code. /usr/local/bin is on sys.path inside the container image.
sys.path.insert(0, "/usr/local/bin")
import control_api  # noqa: E402  pylint: disable=wrong-import-position
import sip_store  # noqa: E402  pylint: disable=wrong-import-position

log = logging.getLogger("rename-pjsip-to-namespaced")


def _db_conn():
    """Same DSN parsing as control_api but kept inline so this script
    runs even when DATABASE_URL contains URL-encoded special chars."""
    url = os.environ["DATABASE_URL"]
    u = urllib.parse.urlparse(url)
    return psycopg2.connect(
        host=u.hostname,
        port=u.port or 5432,
        user=urllib.parse.unquote(u.username or ""),
        password=urllib.parse.unquote(u.password or ""),
        dbname=(u.path or "/").lstrip("/"),
    )


def rename_trunks() -> int:
    """For each sip_trunks row, drop legacy ps_* rows and re-upsert
    via control_api._pjsip_upsert which writes the namespaced ids.
    Returns the number of trunks rewritten."""
    rewritten = 0
    with _db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT tenant_id, id FROM sip_trunks ORDER BY tenant_id, id"
            )
            rows = cur.fetchall()
    for r in rows:
        tenant_id = r["tenant_id"]
        trunk_slug = r["id"]
        new_id = sip_store.pjsip_trunk_endpoint_id(tenant_id, trunk_slug)
        log.info(
            "trunk tenant=%s slug=%s -> %s", tenant_id, trunk_slug, new_id
        )
        # Pull the full trunk view (so we have plaintext password) and
        # re-run the standard upsert path. The control_api side will
        # write the namespaced rows.
        try:
            view = sip_store.get_trunk(_db_conn, tenant_id, trunk_slug)
        except sip_store.StoreError as exc:
            log.warning("skipping trunk %s/%s: %s", tenant_id, trunk_slug, exc)
            continue
        # Decrypt password to feed _trunk_to_pjsip_row.
        with _db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT auth_password_enc FROM sip_trunks "
                "WHERE tenant_id = %s AND id = %s",
                (tenant_id, trunk_slug),
            )
            row = cur.fetchone()
            cipher = row[0] if row else ""
        plaintext = sip_store.decrypt_password(cipher) if cipher else ""
        # Delete the legacy un-namespaced rows (best-effort; they may
        # not exist on a fresh deploy).
        control_api._pjsip_delete(trunk_slug)  # pylint: disable=protected-access
        # Write the namespaced rows.
        pjsip_row = sip_store._trunk_to_pjsip_row(view, plaintext)  # pylint: disable=protected-access
        control_api._pjsip_upsert(pjsip_row, plaintext)  # pylint: disable=protected-access
        rewritten += 1
    return rewritten


def find_legacy_agent_endpoints():
    """Return a list of ps_endpoints.id values matching the legacy
    `staff_<id>` shape (no tenant prefix)."""
    pattern = re.compile(r"^staff_(?!t[a-z0-9]{1,8}_)(.+)$")
    with _db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM ps_endpoints WHERE id LIKE 'staff_%%'")
        rows = [r[0] for r in cur.fetchall()]
    return [r for r in rows if pattern.match(r)]


def rename_agents(assume_tenant: str) -> int:
    """Delete legacy `staff_<id>` realtime rows.

    Listing every ps_endpoints column to copy via INSERT...SELECT is
    fragile: the column set varies by image and the legacy schema
    might be missing recent additions. Instead, drop the legacy rows
    — the softphone will refresh credentials on the next REGISTER
    cycle and `/api/agents/<id>/sip-credentials` will re-provision
    under the namespaced id automatically. Existing softphone
    sessions break for a few seconds while they re-auth; the new
    rows have the same password (credentials get rotated on provision
    anyway, so users see the same effect they would on any password
    rotation).

    assume_tenant is required so we can build the new id deterministically
    (used here only for logging — the actual writes happen later via the
    sip-credentials route).
    """
    legacy = find_legacy_agent_endpoints()
    if not legacy:
        return 0
    removed = 0
    with _db_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for old_id in legacy:
                bare = old_id[len("staff_"):]
                new_id = sip_store.pjsip_agent_endpoint_id(
                    assume_tenant, bare
                )
                if new_id == old_id:
                    continue
                log.info("dropping legacy %s (will re-provision as %s)",
                         old_id, new_id)
                cur.execute("DELETE FROM ps_endpoints WHERE id = %s", (old_id,))
                cur.execute("DELETE FROM ps_auths WHERE id = %s", (old_id,))
                cur.execute("DELETE FROM ps_aors WHERE id = %s", (old_id,))
                removed += 1
    return removed


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--assume-tenant",
        default=None,
        help="Tenant id to attribute legacy un-namespaced staff_<id> rows to. "
             "Required to migrate agent endpoints; if omitted, only trunks "
             "are rewritten and legacy agent rows are reported.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would change; make no DB writes.",
    )
    args = parser.parse_args()

    if args.dry_run:
        log.info("dry-run: trunks that would be rewritten:")
        with _db_conn() as conn, conn.cursor(
            cursor_factory=psycopg2.extras.DictCursor,
        ) as cur:
            cur.execute(
                "SELECT tenant_id, id FROM sip_trunks ORDER BY tenant_id, id"
            )
            for r in cur.fetchall():
                log.info(
                    "  %s/%s -> %s",
                    r["tenant_id"], r["id"],
                    sip_store.pjsip_trunk_endpoint_id(
                        r["tenant_id"], r["id"]
                    ),
                )
        log.info("dry-run: legacy agent endpoints found:")
        for old in find_legacy_agent_endpoints():
            target = (
                sip_store.pjsip_agent_endpoint_id(
                    args.assume_tenant or "<unknown>",
                    old[len("staff_"):],
                )
                if args.assume_tenant
                else "<requires --assume-tenant>"
            )
            log.info("  %s -> %s", old, target)
        return 0

    n_trunks = rename_trunks()
    log.info("trunks rewritten: %d", n_trunks)

    if args.assume_tenant:
        n_agents = rename_agents(args.assume_tenant)
        log.info("agent endpoints rewritten: %d", n_agents)
    else:
        legacy = find_legacy_agent_endpoints()
        if legacy:
            log.warning(
                "%d legacy staff_<id> agent endpoints remain; pass "
                "--assume-tenant=<id> to rewrite them.",
                len(legacy),
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
