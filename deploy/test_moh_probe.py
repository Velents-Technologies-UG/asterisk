#!/usr/bin/env python3
"""Tests for the MOH class resolution probe in control_api.py.

Stdlib unittest on purpose: the sidecar ships with no test runner and the
image installs no pip packages for testing, so a test that needs pytest is a
test nobody in this deployment can run.

    python3 -m unittest discover -s deploy -p 'test_*.py'

WHAT IS AND IS NOT COVERED. These exercise the pure parts: the `odbc show`
parser, the evidence -> verdict table in _moh_fault, the database-identity
redaction, and the directory stat. The live halves - `asterisk -rx` against a
running Asterisk and a real psycopg2 SELECT - are not covered here and cannot
be without a pod.

The properties worth pinning are the ones this module keeps being asked to
get wrong, so most of these assert that something did NOT become a false
negative: an unreadable CLI, an unrunnable SELECT and an unreadable directory
each have to stay undetermined rather than collapse into "not there".
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import control_api as ca  # noqa: E402


ODBC_SHOW_OK = """
ODBC DSN Settings
-----------------

  Name:   asterisk
  DSN:    asterisk-pgsql
    Number of active connections: 3 (out of 20)
    Cache Type: stack (last release, first re-use)
    Cache Usage: 1 cached out of 20
    Logging: Disabled

"""

ODBC_SHOW_IDLE_WITH_OLD_FAILURE = """
ODBC DSN Settings
-----------------

  Name:   asterisk
  DSN:    asterisk-pgsql
    Last fail connection attempt: 2026-08-27 09:14:02
    Number of active connections: 0 (out of 20)
    Logging: Disabled

"""

ODBC_SHOW_OTHER_SECTION_ONLY = """
ODBC DSN Settings
-----------------

  Name:   somethingelse
  DSN:    other-dsn
    Number of active connections: 2 (out of 5)

"""

# res_odbc unloaded: the CLI answers this on a ZERO exit code.
ODBC_SHOW_NO_SUCH_COMMAND = "No such command 'odbc show' (type 'core show help odbc' for other possible commands)\n"


class ParseOdbcShow(unittest.TestCase):
    def test_reads_the_section_it_was_asked_for(self):
        rec, recognised = ca._parse_odbc_show(ODBC_SHOW_OK, "asterisk")
        self.assertTrue(recognised)
        self.assertEqual(rec["section"], "asterisk")
        self.assertEqual(rec["dsn"], "asterisk-pgsql")
        self.assertEqual(rec["activeConnections"], 3)
        self.assertEqual(rec["maxConnections"], 20)
        self.assertIsNone(rec["lastFailedConnect"])

    def test_section_match_is_case_insensitive(self):
        rec, _ = ca._parse_odbc_show(ODBC_SHOW_OK, "ASTERISK")
        self.assertIsNotNone(rec)

    def test_absent_section_is_recognised_output_with_no_record(self):
        rec, recognised = ca._parse_odbc_show(
            ODBC_SHOW_OTHER_SECTION_ONLY, "asterisk")
        self.assertTrue(recognised)
        self.assertIsNone(rec)

    def test_fields_do_not_bleed_between_records(self):
        rec, _ = ca._parse_odbc_show(
            ODBC_SHOW_OTHER_SECTION_ONLY + ODBC_SHOW_OK, "somethingelse")
        self.assertEqual(rec["dsn"], "other-dsn")
        self.assertEqual(rec["activeConnections"], 2)

    def test_no_such_command_is_not_recognised_output(self):
        # The whole point: a zero-exit "No such command" must not read as
        # "the section is not configured".
        rec, recognised = ca._parse_odbc_show(
            ODBC_SHOW_NO_SUCH_COMMAND, "asterisk")
        self.assertFalse(recognised)
        self.assertIsNone(rec)


class OdbcConnectionTriState(unittest.TestCase):
    def setUp(self):
        self._real = ca._asterisk_cli_read

    def tearDown(self):
        ca._asterisk_cli_read = self._real

    def _cli(self, stdout, err=None):
        ca._asterisk_cli_read = lambda cli: (stdout, err)

    def test_live_connections_are_up(self):
        self._cli(ODBC_SHOW_OK)
        rec, up, detail = ca._moh_odbc_connection("asterisk")
        self.assertIs(up, True)
        self.assertIsNone(detail)

    def test_zero_connections_is_undetermined_not_down(self):
        # Idle and dead are indistinguishable from a pool count, and calling
        # a quiet queue's healthy pool "down" is the false negative.
        self._cli(ODBC_SHOW_IDLE_WITH_OLD_FAILURE)
        rec, up, detail = ca._moh_odbc_connection("asterisk")
        self.assertIsNone(up)
        self.assertIn("idle and dead", detail)

    def test_old_failure_timestamp_is_reported_but_not_a_verdict(self):
        # last_negative_connect is never cleared on a later success
        # (res_odbc.c), so it must not make `up` False.
        self._cli(ODBC_SHOW_IDLE_WITH_OLD_FAILURE)
        rec, up, _ = ca._moh_odbc_connection("asterisk")
        self.assertEqual(rec["lastFailedConnect"], "2026-08-27 09:14:02")
        self.assertIsNot(up, False)

    def test_missing_section_is_a_positive_down(self):
        self._cli(ODBC_SHOW_OTHER_SECTION_ONLY)
        rec, up, detail = ca._moh_odbc_connection("asterisk")
        self.assertIs(up, False)
        self.assertIn("no `asterisk` section", detail)

    def test_unreadable_cli_is_undetermined(self):
        self._cli(None, err="`odbc show` timed out after 5s")
        rec, up, detail = ca._moh_odbc_connection("asterisk")
        self.assertIsNone(up)
        self.assertIsNone(rec)
        self.assertIn("timed out", detail)

    def test_module_not_loaded_is_undetermined(self):
        self._cli(ODBC_SHOW_NO_SUCH_COMMAND)
        rec, up, detail = ca._moh_odbc_connection("asterisk")
        self.assertIsNone(up)
        self.assertIn("recognisably", detail)


class SidecarDatabaseIdentity(unittest.TestCase):
    def setUp(self):
        self._real = ca.DATABASE_URL

    def tearDown(self):
        ca.DATABASE_URL = self._real

    def test_never_reports_a_credential(self):
        ca.DATABASE_URL = (
            "postgresql://ast_user:sup3r-s3cret@db.internal:5433/asterisk"
        )
        db = ca._moh_sidecar_database()
        self.assertEqual(
            db, {"host": "db.internal", "port": 5433, "name": "asterisk"})
        blob = repr(db) + (ca._moh_database_identity_str(db) or "")
        self.assertNotIn("sup3r-s3cret", blob)
        self.assertNotIn("ast_user", blob)

    def test_identity_string_is_comparable_one_liner(self):
        ca.DATABASE_URL = "postgres://u:p@h:5432/velents"
        self.assertEqual(
            ca._moh_database_identity_str(ca._moh_sidecar_database()),
            "h:5432/velents",
        )

    def test_defaults_mirror_render_odbc(self):
        # render_odbc.py defaults host->localhost and port->5432; both sides
        # of the operator's comparison have to render the same way.
        ca.DATABASE_URL = "postgresql:///asterisk"
        self.assertEqual(
            ca._moh_sidecar_database(),
            {"host": "localhost", "port": 5432, "name": "asterisk"},
        )

    def test_unset_or_unusable_url_is_none(self):
        for url in ("", "mysql://u:p@h/db", "postgresql://u:p@h:5432/"):
            ca.DATABASE_URL = url
            self.assertIsNone(ca._moh_sidecar_database(), url)


class AudioDirectory(unittest.TestCase):
    def test_missing_directory_is_a_positive_absence(self):
        out = ca._moh_audio_directory("/nonexistent/moh/whatever")
        self.assertIs(out["exists"], False)
        self.assertEqual(out["fileCount"], 0)

    def test_populated_directory_counts_files(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "source.wav"), "wb").close()
            open(os.path.join(d, ".hidden"), "wb").close()
            out = ca._moh_audio_directory(d)
        self.assertIs(out["exists"], True)
        self.assertEqual(out["fileCount"], 1)

    def test_empty_directory_is_zero(self):
        with tempfile.TemporaryDirectory() as d:
            out = ca._moh_audio_directory(d)
        self.assertIs(out["exists"], True)
        self.assertEqual(out["fileCount"], 0)

    def test_relative_directory_resolves_against_ast_data_dir(self):
        out = ca._moh_audio_directory("moh/greeting")
        self.assertEqual(
            out["scannedPath"], os.path.join(ca.AST_DATA_DIR, "moh/greeting"))

    def test_no_directory_reports_nothing(self):
        self.assertIsNone(ca._moh_audio_directory(None))
        self.assertIsNone(ca._moh_audio_directory(""))


def _ev(**kw):
    """An evidence dict with everything undetermined unless overridden."""
    base = {
        "mohClass": "moh-greeting", "resolved": None, "familyMapped": None,
        "rowVisible": None, "rowInSidecarDb": None, "tableInSidecarDb": None,
        "odbcDsnMatchesRendered": None, "odbcConnectionUp": None,
        "odbcSection": "asterisk", "odbc": {"dsn": "asterisk-pgsql"},
        "sidecarDatabaseIdentity": "db.internal:5432/asterisk",
    }
    base.update(kw)
    return base


class FaultMatrix(unittest.TestCase):
    """The evidence -> verdict table, including every undetermined path."""

    def test_resolved_with_audio_is_no_fault(self):
        fault, detail = ca._moh_fault(_ev(resolved=True))
        self.assertEqual(fault, ca.MOH_FAULT_NONE)
        self.assertIsNone(detail)

    def test_family_not_mapped(self):
        fault, detail = ca._moh_fault(
            _ev(resolved=False, familyMapped=False, rowVisible=True))
        self.assertEqual(fault, ca.MOH_FAULT_FAMILY_NOT_MAPPED)
        self.assertIn("extconfig.conf", detail)

    def test_fault_A_row_is_not_in_the_database_this_pod_reads(self):
        fault, detail = ca._moh_fault(_ev(
            resolved=False, familyMapped=True, rowVisible=False,
            rowInSidecarDb=False, tableInSidecarDb=True,
            odbcDsnMatchesRendered=True, odbcConnectionUp=True,
        ))
        self.assertEqual(fault, ca.MOH_FAULT_ROW_ABSENT_HERE)
        self.assertIn("DATABASE_URL", detail)
        self.assertIn("db.internal:5432/asterisk", detail)

    def test_fault_A_missing_table_says_so_specifically(self):
        fault, detail = ca._moh_fault(_ev(
            resolved=False, familyMapped=True, rowVisible=False,
            rowInSidecarDb=False, tableInSidecarDb=False,
            odbcDsnMatchesRendered=True, odbcConnectionUp=True,
        ))
        self.assertEqual(fault, ca.MOH_FAULT_ROW_ABSENT_HERE)
        self.assertIn("does not exist", detail)
        self.assertIn("bootstrap", detail)

    def test_fault_B_row_is_here_and_asterisk_still_cannot_read_it(self):
        fault, detail = ca._moh_fault(_ev(
            resolved=False, familyMapped=True, rowVisible=False,
            rowInSidecarDb=True, tableInSidecarDb=True,
            odbcDsnMatchesRendered=True, odbcConnectionUp=True,
        ))
        self.assertEqual(fault, ca.MOH_FAULT_ROW_HERE_UNREADABLE)
        self.assertIn("NOT a mismatched", detail)
        self.assertIn("search_path", detail)

    def test_undetermined_select_does_not_become_fault_A(self):
        # The single most important case. Asterisk says "no row"; our own
        # SELECT could not run. That must NOT be reported as "the row is in
        # another database" - it is the two-fault ambiguity, unresolved.
        fault, detail = ca._moh_fault(_ev(
            resolved=False, familyMapped=True, rowVisible=False,
            rowInSidecarDb=None, tableInSidecarDb=None,
            odbcDsnMatchesRendered=True, odbcConnectionUp=True,
            sidecarDbDetail="sidecar database connect failed: timeout",
        ))
        self.assertEqual(fault, ca.MOH_FAULT_ROW_ORIGIN_UNKNOWN)
        self.assertIn("NOT", detail)
        self.assertIn("timeout", detail)

    def test_dsn_mismatch_outranks_the_select(self):
        # A masked odbc.ini makes the sidecar's own database irrelevant, so
        # even a definite "no row here" must not be reported as fault (A).
        fault, detail = ca._moh_fault(_ev(
            resolved=False, familyMapped=True, rowVisible=False,
            rowInSidecarDb=False, tableInSidecarDb=True,
            odbcDsnMatchesRendered=False, odbcConnectionUp=True,
        ))
        self.assertEqual(fault, ca.MOH_FAULT_OTHER_DSN)
        self.assertIn("masking odbc.ini", detail)

    def test_missing_odbc_section_outranks_the_generic_B(self):
        fault, detail = ca._moh_fault(_ev(
            resolved=False, familyMapped=True, rowVisible=False,
            rowInSidecarDb=True, tableInSidecarDb=True,
            odbcDsnMatchesRendered=None, odbcConnectionUp=False,
        ))
        self.assertEqual(fault, ca.MOH_FAULT_ODBC_SECTION_MISSING)
        self.assertIn("res_odbc.conf", detail)

    def test_undetermined_resolution_is_never_a_named_fault(self):
        fault, detail = ca._moh_fault(_ev(
            resolved=None, familyMapped=True, rowVisible=None,
            rowInSidecarDb=True,
        ))
        self.assertEqual(fault, ca.MOH_FAULT_UNDETERMINED)

    def test_resolved_but_audio_directory_missing_is_the_silence_fault(self):
        fault, detail = ca._moh_fault(_ev(
            resolved=True,
            audioDirectory={"exists": False, "fileCount": 0,
                            "scannedPath": "/var/lib/asterisk/moh/x"},
        ))
        self.assertEqual(fault, ca.MOH_FAULT_AUDIO_DIR_MISSING)
        self.assertIn("SILENCE", detail)

    def test_resolved_but_audio_directory_empty(self):
        fault, _ = ca._moh_fault(_ev(
            resolved=True,
            audioDirectory={"exists": True, "fileCount": 0,
                            "scannedPath": "/var/lib/asterisk/moh/x"},
        ))
        self.assertEqual(fault, ca.MOH_FAULT_AUDIO_DIR_EMPTY)

    def test_unreadable_audio_directory_is_not_a_fault(self):
        # "we could not look" is not "there is nothing there".
        fault, detail = ca._moh_fault(_ev(
            resolved=True,
            audioDirectory={"exists": None, "fileCount": None,
                            "scannedPath": "/var/lib/asterisk/moh/x"},
        ))
        self.assertEqual(fault, ca.MOH_FAULT_NONE)

    def test_populated_directory_is_not_promoted_to_an_audibility_claim(self):
        # fileCount > 0 says nothing about playable FORMAT, so it may only
        # ever fail to raise a fault - never assert success beyond `resolved`.
        fault, _ = ca._moh_fault(_ev(
            resolved=True,
            audioDirectory={"exists": True, "fileCount": 4,
                            "scannedPath": "/var/lib/asterisk/moh/x"},
        ))
        self.assertEqual(fault, ca.MOH_FAULT_NONE)


class ResolvedIsUnaffectedByTheNewEvidence(unittest.TestCase):
    """`resolved` is call-engine's contract; only the two CLI reads feed it."""

    def setUp(self):
        self._cli = ca._asterisk_cli_read
        self._row = ca._moh_row_in_sidecar_db
        self._odbc = ca._moh_odbc_connection

    def tearDown(self):
        ca._asterisk_cli_read = self._cli
        ca._moh_row_in_sidecar_db = self._row
        ca._moh_odbc_connection = self._odbc

    def _asterisk_says_resolved(self):
        def fake(cli):
            if cli == "core show config mappings":
                return (
                    "Config Engine: odbc\n"
                    "===> musiconhold (db=asterisk, table=musiconhold)\n"
                ), None
            if cli.startswith("realtime load"):
                return "Column Name          Column Value\nname   moh-x\n", None
            return "", None
        ca._asterisk_cli_read = fake

    def test_unreachable_sidecar_database_cannot_unresolve_a_working_class(self):
        self._asterisk_says_resolved()
        ca._moh_row_in_sidecar_db = lambda c: (None, None, None, "db down")
        ca._moh_odbc_connection = lambda s: (None, None, "cli unreadable")
        out = ca._probe_moh_class("moh-x")
        self.assertIs(out["resolved"], True)
        self.assertIs(out["familyMapped"], True)
        self.assertIs(out["rowVisible"], True)
        self.assertEqual(out["fault"], ca.MOH_FAULT_NONE)
        self.assertIsNone(out["rowInSidecarDb"])

    def test_probe_reports_the_three_original_tristates_always(self):
        self._asterisk_says_resolved()
        ca._moh_row_in_sidecar_db = lambda c: (True, True, None, None)
        ca._moh_odbc_connection = lambda s: (
            {"dsn": ca.ODBC_DSN_NAME}, True, None)
        out = ca._probe_moh_class("moh-x")
        for k in ("resolved", "familyMapped", "rowVisible"):
            self.assertIn(k, out)
        self.assertIs(out["odbcDsnMatchesRendered"], True)

    def test_unreadable_asterisk_reports_undetermined_with_the_cli_reason(self):
        ca._asterisk_cli_read = lambda cli: (None, "asterisk binary not on PATH")
        ca._moh_row_in_sidecar_db = lambda c: (False, True, None, None)
        ca._moh_odbc_connection = lambda s: (None, None, "no cli")
        out = ca._probe_moh_class("moh-x")
        self.assertIsNone(out["resolved"])
        self.assertEqual(out["fault"], ca.MOH_FAULT_UNDETERMINED)
        self.assertIn("not on PATH", out["detail"])


if __name__ == "__main__":
    unittest.main()
