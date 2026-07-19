-- Migration: register a test inbound flow for tenant `testCallCenter` that
-- forwards the call to a personal number, for end-to-end inbound-call testing.
--
-- Target: the NONPROD/test Postgres instance (NOT the prod
-- velents-prod-postgres instance used elsewhere in this repo).
--   Host:     velents-nonprod-postgres.cr806yc6wdhh.eu-central-1.rds.amazonaws.com
--   Port:     5432 (reach via SSM port-forward to localhost:15432)
--   Central DB (statement 2): velentsagents
--   Tenant DB (statement 1):  tenant_testCallCenter_boddytest
--
-- Confirmed present on this instance (not on prod): `flows` (tenant-scoped),
-- `did_registry` + `tenants` (central, in `velentsagents`).
--
-- Run statement 1 against tenant_testCallCenter_boddytest.
-- Run statement 2 against velentsagents (central).
--
-- Rollback: see the DELETE statements at the bottom of this file.

-- =====================================================================
-- 1. Flow — tenant_testCallCenter_boddytest.flows
--    entry -> transfer (e164 -> +201112235924, 30s ring timeout)
-- =====================================================================
INSERT INTO flows (public_id, name, definition, entry_node_id, status, version, tags, created_at, updated_at)
VALUES (
  'forward-to-personal-test',
  'Forward to personal number (test)',
  '{
    "entryNodeId": "n_start",
    "nodes": [
      {"id": "n_start",    "type": "start"},
      {"id": "n_transfer", "type": "transfer", "data": {"target_type": "e164", "target": "+201112235924", "timeout_seconds": 30}}
    ],
    "edges": [
      {"id": "e1", "from": "n_start", "to": "n_transfer"}
    ]
  }'::jsonb,
  'n_start',
  'published',
  1,
  '[]'::jsonb,
  now(), now()
);

-- =====================================================================
-- 2. DID registry — velentsagents.did_registry (central)
--    EXECUTED 2026-07-19. Confirmed no prior row existed for this DID
--    (an earlier assumption that it was already mapped to `infath` was
--    wrong — that was a different number, +20248825564. This DID had no
--    did_registry row at all before this migration, so no tenant lost
--    it — this was a clean INSERT, not a repoint.)
--    Result row: id=5, did='+966115030505', tenant_id='testCallCenter',
--    flow_public_id='forward-to-personal-test', enabled=true.
--    Verified live via GET /ML/Did/Resolve/+966115030505 -> ok:true.
-- =====================================================================
INSERT INTO did_registry (did, tenant_id, flow_public_id, description, enabled, created_at, updated_at)
VALUES (
  '+966115030505',
  'testCallCenter',
  'forward-to-personal-test',
  'Test DID: forwards inbound calls to +201112235924 for end-to-end inbound testing',
  true,
  now(), now()
);

-- =====================================================================
-- Rollback
-- =====================================================================
-- DELETE FROM did_registry WHERE did = '+966115030505' AND tenant_id = 'testCallCenter';
-- DELETE FROM flows WHERE public_id = 'forward-to-personal-test';
