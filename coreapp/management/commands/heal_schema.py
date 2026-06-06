"""
Universal database schema healer.

Walks every Django model in the project, compares the Python field
definitions against the actual columns in the database, and adds any
missing columns via idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.

Why this exists
───────────────
Django's `migrate` command sometimes marks a migration as applied
(records it in `django_migrations`) without actually running the
underlying DDL — typically on managed Postgres providers where
permission scoping, connection pooling, or transaction quirks
silently swallow the ALTER TABLE. The result is a
"phantom migration": Python thinks the column exists, the database
disagrees, and every query against that table throws
`column does not exist` at runtime.

We hit this on Railway with the Shopify refresh-token migration —
every authenticated `/stores/api/` request 500'd, the dashboard
showed "Unable to load projects", and nothing in the deploy logs
indicated a problem.

This command is the permanent fix:
  * Discovers drift on its own (no hand-maintained column list).
  * Idempotent — safe to run on every deploy.
  * Reports exactly what it added.

Wire into your release pipeline:

    web: python manage.py migrate --noinput \
         && python manage.py heal_schema \
         && python manage.py ensure_superuser \
         && gunicorn ...

Usage:
    python manage.py heal_schema             # heal + report
    python manage.py heal_schema --dry-run   # report only, no changes
    python manage.py heal_schema --app stores  # restrict to one app

The `--dry-run` flag is what the boot-time audit uses to log CRITICAL
when drift exists without actually mutating anything (so the audit
can't corrupt state on a misconfigured deploy).
"""
from __future__ import annotations

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.models import Model


# ─── Field → SQL column type mapping ─────────────────────────────────────────
# Maps Django internal_type() values to (postgres, sqlite) DDL fragments.
# Only fields we might add post-deploy need to be here. Defaults to TEXT for
# unknown types so we never crash — worst case is a too-permissive column.
_PG_TYPES = {
    "AutoField":              "INTEGER",
    "BigAutoField":           "BIGINT",
    "BigIntegerField":        "BIGINT",
    "BooleanField":           "BOOLEAN",
    "CharField":              "VARCHAR({max_length})",
    "DateField":              "DATE",
    "DateTimeField":          "TIMESTAMP WITH TIME ZONE",
    "DecimalField":           "NUMERIC({max_digits},{decimal_places})",
    "DurationField":          "INTERVAL",
    "EmailField":             "VARCHAR({max_length})",
    "FileField":              "VARCHAR({max_length})",
    "FloatField":             "DOUBLE PRECISION",
    "ImageField":             "VARCHAR({max_length})",
    "IntegerField":           "INTEGER",
    "JSONField":              "JSONB",
    "PositiveBigIntegerField":"BIGINT",
    "PositiveIntegerField":   "INTEGER",
    "PositiveSmallIntegerField":"SMALLINT",
    "SlugField":              "VARCHAR({max_length})",
    "SmallIntegerField":      "SMALLINT",
    "TextField":              "TEXT",
    "TimeField":              "TIME",
    "URLField":               "VARCHAR({max_length})",
    "UUIDField":              "UUID",
    # Relational
    "ForeignKey":             "BIGINT",         # stored as FK id
    "OneToOneField":          "BIGINT",
}

_SQLITE_TYPES = {
    "BooleanField":           "BOOL",
    "DateField":              "DATE",
    "DateTimeField":          "DATETIME",
    "DecimalField":           "DECIMAL",
    "FloatField":             "REAL",
    "IntegerField":           "INTEGER",
    "JSONField":              "TEXT",
    "PositiveIntegerField":   "INTEGER",
    "PositiveSmallIntegerField":"SMALLINT",
    "SmallIntegerField":      "SMALLINT",
    "TimeField":              "TIME",
}


def _column_ddl_type(field) -> str:
    """Return a SQL type fragment for `field`, picking by db vendor."""
    vendor = connection.vendor
    internal = field.get_internal_type()

    if vendor == "postgresql":
        tmpl = _PG_TYPES.get(internal, "TEXT")
    else:
        tmpl = _SQLITE_TYPES.get(internal, "TEXT")

    # Substitute attributes if the template uses them.
    fmt = {}
    for attr in ("max_length", "max_digits", "decimal_places"):
        fmt[attr] = getattr(field, attr, None) or ("255" if attr == "max_length" else "0")
    try:
        return tmpl.format(**fmt)
    except (KeyError, ValueError):
        return "TEXT"


def _column_default_clause(field) -> str:
    """Best-effort DEFAULT clause so existing rows get a sensible value."""
    if field.has_default():
        try:
            default = field.get_default()
        except Exception:
            return ""
        if default is None:
            return "DEFAULT NULL"
        if isinstance(default, bool):
            return f"DEFAULT {'TRUE' if default else 'FALSE'}"
        if isinstance(default, (int, float)):
            return f"DEFAULT {default}"
        if isinstance(default, str):
            safe = default.replace("'", "''")
            return f"DEFAULT '{safe}'"
        # Callables / objects → fall through to NULL so the ALTER succeeds
        return "DEFAULT NULL" if field.null else ""

    return "DEFAULT NULL" if field.null else ""


def _db_columns(table: str) -> set[str]:
    """Return the set of column names that physically exist in `table`."""
    vendor = connection.vendor
    with connection.cursor() as cur:
        if vendor == "postgresql":
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s", [table],
            )
            return {row[0] for row in cur.fetchall()}
        else:
            # SQLite
            cur.execute(f"PRAGMA table_info('{table}')")
            return {row[1] for row in cur.fetchall()}


def _add_column(table: str, column: str, sql_type: str, default: str, *, dry_run: bool) -> str | None:
    """Add a column if missing. Returns the SQL it ran, or None on no-op."""
    vendor = connection.vendor

    # Build the DDL. ADD COLUMN IF NOT EXISTS is supported on Postgres
    # but NOT on every SQLite version, so we check existence ourselves
    # for SQLite to stay portable.
    if vendor == "postgresql":
        sql = (
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
            f"{column} {sql_type} {default}".strip()
        )
    else:
        existing = _db_columns(table)
        if column in existing:
            return None
        sql = f'ALTER TABLE "{table}" ADD COLUMN "{column}" {sql_type} {default}'.strip()

    if dry_run:
        return sql

    with connection.cursor() as cur:
        cur.execute(sql)
    return sql


class Command(BaseCommand):
    help = "Heal database schema drift — add any missing columns."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report drift without altering the database.",
        )
        parser.add_argument(
            "--app", default="",
            help="Restrict to a single app label (e.g. 'stores').",
        )

    def handle(self, *args, **opts):
        dry_run: bool = opts["dry_run"]
        target_app: str = opts["app"]

        problems_found = 0
        problems_fixed = 0
        scanned_tables = 0

        for model in apps.get_models(include_auto_created=True):
            if not issubclass(model, Model):
                continue
            if model._meta.abstract or model._meta.managed is False:
                continue
            if target_app and model._meta.app_label != target_app:
                continue

            table = model._meta.db_table
            try:
                existing_cols = _db_columns(table)
            except Exception as e:
                self.stdout.write(self.style.WARNING(
                    f"  ⚠ {model._meta.label}: could not inspect table {table!r}: {e}"
                ))
                continue
            if not existing_cols:
                # Table itself doesn't exist yet — that's a `migrate`
                # job, not ours. Skip.
                continue
            scanned_tables += 1

            for field in model._meta.get_fields():
                # Skip reverse relations + many-to-many (separate tables) +
                # generic FKs (no real column).
                if not getattr(field, "column", None):
                    continue
                if field.many_to_many:
                    continue
                column = field.column
                if column in existing_cols:
                    continue

                problems_found += 1
                sql_type = _column_ddl_type(field)
                default = _column_default_clause(field)

                try:
                    sql = _add_column(table, column, sql_type, default, dry_run=dry_run)
                except Exception as e:
                    self.stdout.write(self.style.ERROR(
                        f"  ✗ {model._meta.label}.{field.name} ({table}.{column}): {e}"
                    ))
                    continue

                if dry_run:
                    self.stdout.write(self.style.WARNING(
                        f"  ⚠ would add {table}.{column}  ({sql})"
                    ))
                else:
                    problems_fixed += 1
                    self.stdout.write(self.style.SUCCESS(
                        f"  ✓ added {table}.{column}  ({sql_type})"
                    ))

        # ─── Summary ────────────────────────────────────────────────────
        if problems_found == 0:
            self.stdout.write(self.style.SUCCESS(
                f"heal_schema: all {scanned_tables} tables in sync ✓"
            ))
            return

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"heal_schema (dry-run): {problems_found} column(s) missing "
                f"across {scanned_tables} tables. Re-run without --dry-run to apply."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"heal_schema: healed {problems_fixed}/{problems_found} missing "
                f"column(s) across {scanned_tables} tables."
            ))
