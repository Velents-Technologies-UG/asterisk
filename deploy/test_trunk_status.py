#!/usr/bin/env python3
"""Tests for the trunk badge the status feeder writes.

    python3 -m unittest discover -s deploy -p 'test_*.py'

Covers the pure parts: the `pjsip show registrations` parser, the one rule
that turns endpoint state + identify + registration into a badge, and the
slug a call-engine (trunks_meta) endpoint id maps back to. The live halves
(`asterisk -rx`, the trunks_meta SELECT) need a pod.

The case that motivated this (prod, 2026-10-05): a Voylo line created from
the Telephony page was Registered and carrying calls while its fixed AOR
contact sat at NonQual, so the endpoint read Unavailable and the page said
"not confirmed".
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import control_api as ca  # noqa: E402


REGISTRATIONS = """
 <Registration/ServerURI..............................>  <Auth....................>  <Status.......>
==========================================================================================

 ta985cf9d_voylo-outbound/sip:sip.uae.voylo.ai           ta985cf9d_voylo-outbound-auth  Registered        (exp. 2852s)
 t062ac5eb_innov2/sip:cu622.sip.innocalls.net:50760      t062ac5eb_innov2-auth          Rejected
 t062ac5eb_other/sip:sip.carrier.net                     t062ac5eb_other-auth           Unregistered
 t062ac5eb_pending/sip:sip.carrier.net                   t062ac5eb_pending-auth         Unknown

Objects found: 4
"""


class RegistrationParserTest(unittest.TestCase):
    def test_reads_each_row_by_endpoint_id(self):
        got = dict(ca._parse_pjsip_registrations(REGISTRATIONS))
        self.assertEqual(got["ta985cf9d_voylo-outbound"], "online")
        self.assertEqual(got["t062ac5eb_innov2"], "offline")

    def test_only_rejected_is_evidence_of_failure(self):
        # Unregistered is what every registration reads right after an
        # Asterisk restart; reading it as offline fires a false drop alert
        # (and a restore one) on every restart. Unknown says nothing either.
        got = dict(ca._parse_pjsip_registrations(REGISTRATIONS))
        self.assertNotIn("t062ac5eb_other", got)
        self.assertNotIn("t062ac5eb_pending", got)

    def test_stopped_says_nothing(self):
        out = " t1_x/sip:h  t1_x-auth  Stopped\n"
        self.assertEqual(list(ca._parse_pjsip_registrations(out)), [])

    def test_header_and_footer_are_not_rows(self):
        got = dict(ca._parse_pjsip_registrations(REGISTRATIONS))
        self.assertEqual(len(got), 2)

    def test_no_registrations(self):
        self.assertEqual(list(ca._parse_pjsip_registrations("No objects found.\n")), [])


class TrunkLiveStatusTest(unittest.TestCase):
    def test_a_registered_line_is_online_whatever_the_endpoint_says(self):
        self.assertEqual(
            ca._trunk_live_status("ep", "Unavailable", set(), {"ep": "online"}),
            "online",
        )

    def test_a_rejected_registration_is_offline(self):
        self.assertEqual(
            ca._trunk_live_status("ep", "Not in use", set(), {"ep": "offline"}),
            "offline",
        )

    def test_an_ip_trunk_is_online_as_before(self):
        self.assertEqual(
            ca._trunk_live_status("ep", "Unavailable", {"ep"}, {}), "online",
        )

    def test_no_registration_falls_back_to_the_endpoint_state(self):
        self.assertEqual(ca._trunk_live_status("ep", "Not in use", set(), {}), "online")
        self.assertEqual(ca._trunk_live_status("ep", "Unavailable", set(), {}), "offline")
        self.assertEqual(ca._trunk_live_status("ep", "Invalid", set(), {}), "unknown")


AORS = """
      Aor:  <Aor..............................................>  <MaxContact>
    Contact:  <Aor/ContactUri............................> <Hash....> <Status> <RTT(ms)..>
==========================================================================================

      Aor:  tinfath_trunk_magict_out                           1
    Contact:  tinfath_trunk_magict_out/sip:85.194.93.23:5060 5a1b2c3d4e NonQual         nan

      Aor:  t062ac5eb_dead                                      1
    Contact:  t062ac5eb_dead/sip:203.0.113.9:5060            1a2b3c4d5e Unavail         nan

      Aor:  t062ac5eb_empty                                     1

      Aor:  t062ac5eb_fresh                                     1
    Contact:  t062ac5eb_fresh/sip:203.0.113.7:5060           9a8b7c6d5e Unknown         nan

      Aor:  t062ac5eb_mixed                                     1
    Contact:  t062ac5eb_mixed/sip:203.0.113.5:5060           2b3c4d5e6f NonQual         nan
    Contact:  t062ac5eb_mixed/sip:203.0.113.6:5060           3c4d5e6f7a Unavail         nan
"""


class NonQualTrunkTest(unittest.TestCase):
    """A line nothing measures must read unknown, never offline."""

    def setUp(self):
        self.contacts = ca._aor_contact_states(AORS)

    def test_contact_states_are_read_per_aor(self):
        self.assertEqual(self.contacts["tinfath_trunk_magict_out"], ["NonQual"])
        self.assertEqual(self.contacts["t062ac5eb_dead"], ["Unavail"])
        self.assertEqual(self.contacts["t062ac5eb_empty"], [])

    def test_unavailable_over_nonqual_contacts_is_unknown(self):
        # MAG-ICT's outbound endpoint: qualify off on purpose, no
        # registration, no identify row. Red here would be a false fault on
        # the line carrying the tenant's calls.
        self.assertEqual(
            ca._trunk_live_status("tinfath_trunk_magict_out", "Unavailable",
                                  set(), {}, self.contacts),
            "unknown",
        )

    def test_a_contact_not_yet_qualified_after_a_restart_is_unknown(self):
        # Qualify on, first OPTIONS not answered yet: offline here would be
        # an online -> offline edge, i.e. a false drop alert, on every
        # Asterisk restart.
        self.assertEqual(
            ca._trunk_live_status("t062ac5eb_fresh", "Unavailable",
                                  set(), {}, self.contacts),
            "unknown",
        )

    def test_one_measured_unreachable_contact_keeps_it_offline(self):
        self.assertEqual(
            ca._trunk_live_status("t062ac5eb_mixed", "Unavailable",
                                  set(), {}, self.contacts),
            "offline",
        )

    def test_a_measured_unreachable_contact_is_still_offline(self):
        self.assertEqual(
            ca._trunk_live_status("t062ac5eb_dead", "Unavailable",
                                  set(), {}, self.contacts),
            "offline",
        )

    def test_no_contact_at_all_is_still_offline(self):
        self.assertEqual(
            ca._trunk_live_status("t062ac5eb_empty", "Unavailable",
                                  set(), {}, self.contacts),
            "offline",
        )

    def test_the_old_aor_verdict_is_unchanged(self):
        got = dict(ca._parse_pjsip_aors(AORS))
        self.assertEqual(got["tinfath_trunk_magict_out"], "online")
        self.assertEqual(got["t062ac5eb_dead"], "offline")
        self.assertEqual(got["t062ac5eb_empty"], "offline")


class ReverseMapTest(unittest.TestCase):
    """trunks_meta rows join the reverse map; sip_trunks wins on a clash."""

    def setUp(self):
        self._orig = (ca._trunks_meta_reverse_rows, ca.HAS_SIP_STORE,
                      ca._db_enabled, ca._db_conn)

    def tearDown(self):
        (ca._trunks_meta_reverse_rows, ca.HAS_SIP_STORE,
         ca._db_enabled, ca._db_conn) = self._orig
        ca._SLUG_CLASH_WARNED.clear()

    def _with(self, sip_rows, meta_rows):
        class Cur:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def execute(s, *a): pass
            def fetchall(s): return sip_rows

        class Conn:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def cursor(s): return Cur()

        ca.HAS_SIP_STORE = True
        ca._db_enabled = lambda: True
        ca._db_conn = lambda: Conn()
        ca._trunks_meta_reverse_rows = lambda: meta_rows
        return ca._build_trunk_id_reverse_map()

    def test_a_trunks_meta_line_is_placed_under_its_tenant(self):
        got = self._with([], [
            ("ta985cf9d_voylo-outbound",
             ("livedemos", "voylo-outbound", "Voylo Outbound")),
        ])
        self.assertEqual(got["ta985cf9d_voylo-outbound"],
                         ("livedemos", "voylo-outbound", "Voylo Outbound"))

    def test_sip_trunks_wins_a_slug_clash(self):
        sip_id = ca.sip_store.pjsip_trunk_endpoint_id("livedemos", "voylo")
        with self.assertLogs(ca.log, level="WARNING"):
            got = self._with([("livedemos", "voylo", "From sidecar")], [
                ("voylo", ("livedemos", "voylo", "From call-engine")),
            ])
        self.assertEqual(got[sip_id][2], "From sidecar")
        self.assertNotIn("voylo", got)


class TrunkSlugTest(unittest.TestCase):
    def test_strips_this_tenants_prefix(self):
        # livedemos is 9 characters, so its prefix is hashed: ta985cf9d_
        self.assertEqual(
            ca._trunk_slug("livedemos", "ta985cf9d_voylo-outbound"), "voylo-outbound",
        )
        self.assertEqual(ca._trunk_slug("infath", "tinfath_trunk_magict_out"),
                         "trunk_magict_out")

    def test_leaves_another_tenants_prefix_alone(self):
        self.assertEqual(
            ca._trunk_slug("infath", "ta985cf9d_voylo-outbound"),
            "ta985cf9d_voylo-outbound",
        )

    def test_an_un_namespaced_id_is_its_own_slug(self):
        self.assertEqual(ca._trunk_slug("livedemos", "trunk_voylo_out"), "trunk_voylo_out")


class FakeRedis:
    """The few hash commands the stale sweep uses, over plain dicts."""

    def __init__(self, data=None):
        self.data = {k: dict(v) for k, v in (data or {}).items()}

    def hkeys(self, key):
        return list(self.data.get(key, {}))

    def hdel(self, key, *fields):
        h = self.data.get(key, {})
        for f in fields:
            h.pop(f, None)

    def scan_iter(self, match):
        raise AssertionError("the sweep must not scan other instances' keys")


def _roster(*rows):
    """(tenant, slug) rows -> the reverse-map shape the feeder builds."""
    return {f"t_{t}_{s}": (t, s, s.title()) for t, s in rows}


class SharedRedisSweepTest(unittest.TestCase):
    """Two instances, one Redis: a sweep deletes only what it owns.

    Azure prod (2026-10): "primary" holds infath, "shared" every other
    tenant, each with its own sip_trunks. Before this, each sweep emptied
    every cx:trunks:status:<tenant> hash missing from its own table.
    """

    KEY = ca.STATUS_FEEDER_KEY

    def setUp(self):
        self._legacy = ca.STATUS_FEEDER_LEGACY_HASH
        ca.STATUS_FEEDER_LEGACY_HASH = True
        self.redis = FakeRedis({
            f"{self.KEY}:infath": {"magict": "online"},
            f"{self.KEY}:livedemos": {"voylo": "online", "gone": "online"},
            self.KEY: {"magict": "online", "voylo": "online",
                       "gone": "online"},
        })

    def tearDown(self):
        ca.STATUS_FEEDER_LEGACY_HASH = self._legacy

    def test_another_instances_tenant_is_never_touched(self):
        owned = ca._FeederOwnership()
        prev = {("infath", "magict"): "online"}
        ca._sweep_stale_trunk_status(
            self.redis, _roster(("livedemos", "voylo")), {"voylo": "online"},
            prev, owned,
        )
        self.assertEqual(self.redis.data[f"{self.KEY}:infath"],
                         {"magict": "online"})
        self.assertIn(("infath", "magict"), prev)

    def test_an_owned_tenants_deleted_trunk_is_still_removed(self):
        owned = ca._FeederOwnership()
        prev = {("livedemos", "gone"): "online"}
        ca._sweep_stale_trunk_status(
            self.redis, _roster(("livedemos", "voylo")), {"voylo": "online"},
            prev, owned,
        )
        self.assertEqual(self.redis.data[f"{self.KEY}:livedemos"],
                         {"voylo": "online"})
        self.assertNotIn(("livedemos", "gone"), prev)

    def test_legacy_fields_this_instance_never_wrote_survive(self):
        owned = ca._FeederOwnership()
        owned.legacy_written = {"voylo", "gone"}
        ca._sweep_stale_trunk_status(
            self.redis, _roster(("livedemos", "voylo")), {"voylo": "online"},
            {}, owned,
        )
        # "magict" is the other instance's; "gone" was ours and is deleted.
        self.assertEqual(self.redis.data[self.KEY],
                         {"magict": "online", "voylo": "online"})
        self.assertEqual(owned.legacy_written, {"voylo"})

    def test_a_tenants_last_trunk_deleted_clears_only_known_slugs(self):
        owned = ca._FeederOwnership()
        owned.tenants = {"livedemos"}
        self.redis.data[f"{self.KEY}:livedemos"]["foreign"] = "online"
        prev = {("livedemos", "voylo"): "online",
                ("livedemos", "gone"): "online"}
        ca._sweep_stale_trunk_status(self.redis, {}, {}, prev, owned)
        self.assertEqual(self.redis.data[f"{self.KEY}:livedemos"],
                         {"foreign": "online"})
        self.assertEqual(owned.tenants, set())
        self.assertEqual(prev, {})

    def test_legacy_hash_off_leaves_it_alone(self):
        ca.STATUS_FEEDER_LEGACY_HASH = False
        owned = ca._FeederOwnership()
        owned.legacy_written = {"gone"}
        ca._sweep_stale_trunk_status(
            self.redis, _roster(("livedemos", "voylo")), {}, {}, owned,
        )
        self.assertIn("gone", self.redis.data[self.KEY])

    def test_strays_written_this_sweep_are_spared(self):
        owned = ca._FeederOwnership()
        owned.legacy_written = {"staff_2"}
        self.redis.data[self.KEY]["staff_2"] = "online"
        ca._sweep_stale_trunk_status(
            self.redis, _roster(("livedemos", "voylo")),
            {"staff_2": "online"}, {}, owned,
        )
        self.assertIn("staff_2", self.redis.data[self.KEY])


if __name__ == "__main__":
    unittest.main()
