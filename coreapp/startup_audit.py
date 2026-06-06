"""
Boot-time schema audit — runs immediately after Django boot.

If any model's declared field is missing its column in the database,
we log CRITICAL with the exact table + column. This is the canary that
catches the "phantom migration" bug class (migration marked applied
in `django_migrations` but the underlying ALTER TABLE never ran).

Skipped in management-command + test contexts so a deliberate fresh
database (during `migrate`, `test`, etc.) doesn't trigger noise.
"""
from __future__ import annotations
import logging
import os
import sys

log = logging.getLogger(__name__)

# These management commands intentionally touch the schema or run
# without a real DB. Skip the audit in those.
_SKIP_COMMANDS = {
    "migrate", "makemigrations", "test", "collectstatic", "shell",
    "createsuperuser", "dumpdata", "loaddata", "check", "showmigrations",
    "sqlmigrate", "diffsettings", "heal_schema",
}


def _should_audit() -> bool:
    if os.getenv("DROP_SIGMA_SKIP_SCHEMA_AUDIT", "").lower() in ("1", "true", "yes"):
        return False
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False

    argv = sys.argv
    if not argv:
        return True  # WSGI / gunicorn entry point
    cmd = (argv[1] if len(argv) > 1 else "").lower()
    if cmd in _SKIP_COMMANDS:
        return False
    return True


def audit_schema_on_boot() -> None:
    """Walk every model + log CRITICAL for missing columns. Non-blocking."""
    if not _should_audit():
        return

    try:
        from django.apps import apps
        from django.db import connection
        from django.db.models import Model
    except Exception:
        return  # Django not fully ready

    try:
        with connection.cursor() as cur:
            vendor = connection.vendor

            def _table_columns(table: str) -> set[str]:
                if vendor == "postgresql":
                    cur.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = %s", [table],
                    )
                    return {row[0] for row in cur.fetchall()}
                cur.execute(f"PRAGMA table_info('{table}')")
                return {row[1] for row in cur.fetchall()}

            problems: list[str] = []
            for model in apps.get_models(include_auto_created=True):
                if not issubclass(model, Model):
                    continue
                if model._meta.abstract or model._meta.managed is False:
                    continue
                table = model._meta.db_table
                try:
                    existing = _table_columns(table)
                except Exception:
                    continue
                if not existing:
                    continue  # table not created yet; `migrate` will handle it
                for field in model._meta.get_fields():
                    if not getattr(field, "column", None):
                        continue
                    if field.many_to_many:
                        continue
                    if field.column not in existing:
                        problems.append(f"{table}.{field.column}  "
                                        f"(model {model._meta.label}.{field.name})")

    except Exception as e:
        log.warning("schema audit could not inspect database: %s", e)
        return

    if not problems:
        log.info("schema audit: all model fields have matching columns ✓")
        return

    log.critical(
        "🚨 SCHEMA DRIFT DETECTED — %d column(s) declared on models but missing "
        "from the database. Run `python manage.py heal_schema` to fix:\n  - %s",
        len(problems), "\n  - ".join(problems),
    )
