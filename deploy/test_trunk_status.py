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
        self.assertEqual(got["t062ac5eb_other"], "offline")

    def test_an_unknown_status_says_nothing(self):
        # Unknown must not become "offline": it would override an endpoint
        # state that may well be healthy.
        got = dict(ca._parse_pjsip_registrations(REGISTRATIONS))
        self.assertNotIn("t062ac5eb_pending", got)

    def test_header_and_footer_are_not_rows(self):
        got = dict(ca._parse_pjsip_registrations(REGISTRATIONS))
        self.assertEqual(len(got), 3)

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


if __name__ == "__main__":
    unittest.main()
