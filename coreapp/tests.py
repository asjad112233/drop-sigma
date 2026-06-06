"""
Tests for the schema-drift safety net.

The "phantom migration" bug class (Django says column exists, DB
disagrees) is impossible to debug from frontend logs alone. We need
two guarantees baked into the codebase forever:

  1. `heal_schema` can find AND repair drift autonomously.
  2. The boot-time audit (coreapp.startup_audit) detects drift and
     logs CRITICAL so any future occurrence shows up in Railway logs
     immediately instead of being a silent 500 on every request.

If either of those breaks, this test suite fails.
"""
from __future__ import annotations

import logging
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase

from coreapp.startup_audit import audit_schema_on_boot


class HealSchemaSelfTest(TestCase):
    """`heal_schema --dry-run` on a healthy DB should report 'in sync'.
    Simplest possible smoke test — if it fails, auto-discovery is broken."""

    def test_clean_db_is_in_sync(self):
        out = StringIO()
        call_command("heal_schema", "--dry-run", stdout=out)
        output = out.getvalue()
        self.assertIn("in sync", output,
                      f"heal_schema reported drift on a fresh test DB:\n{output}")
        self.assertNotIn("would add", output)


class StartupAuditTest(TestCase):
    """The boot-time audit MUST detect drift and log CRITICAL.
    Without that signal, schema drift becomes invisible in production
    and merchants see silent 500s on every dashboard request."""

    def test_no_log_on_healthy_db(self):
        """Clean DB → no CRITICAL log. False positives on every deploy
        would make the signal useless noise."""
        with patch("coreapp.startup_audit._should_audit", return_value=True):
            logger = logging.getLogger("coreapp.startup_audit")
            captured = []

            class _Capture(logging.Handler):
                def emit(self, record):
                    captured.append(record)

            handler = _Capture()
            logger.addHandler(handler)
            try:
                audit_schema_on_boot()
            finally:
                logger.removeHandler(handler)

            critical_records = [r for r in captured if r.levelno >= logging.CRITICAL]
            self.assertEqual(
                critical_records, [],
                f"Audit logged CRITICAL on a healthy DB: "
                f"{[r.getMessage() for r in critical_records]}"
            )

    def test_critical_logged_on_drift(self):
        """Drop a column behind Django's back → audit MUST log CRITICAL
        naming the missing column."""
        if connection.vendor == "postgresql":
            sql = "ALTER TABLE auth_user DROP COLUMN IF EXISTS last_login"
        else:
            sql = "ALTER TABLE auth_user DROP COLUMN last_login"
        try:
            with connection.cursor() as cur:
                cur.execute(sql)
        except Exception:
            # SQLite versions without DROP COLUMN support — Postgres
            # path is the production one that matters.
            self.skipTest("Database can't DROP COLUMN in this environment")

        with patch("coreapp.startup_audit._should_audit", return_value=True), \
             self.assertLogs("coreapp.startup_audit", level="CRITICAL") as captured:
            audit_schema_on_boot()
        joined = "\n".join(captured.output)
        self.assertIn("SCHEMA DRIFT DETECTED", joined)
        self.assertIn("last_login", joined)
