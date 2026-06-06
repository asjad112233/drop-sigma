"""
Defensive schema healer for the stores_store table.

Background: migration 0003_add_shopify_refresh_token introduced three
new columns on stores_store. On some deploys (Railway in particular)
the migration framework reports the migration as applied but the
columns are not physically present in the database — which causes
every authenticated /stores/api/ request to crash with
"column refresh_token does not exist".

This management command makes the schema match the model by issuing
idempotent ADD COLUMN IF NOT EXISTS statements. Safe to run on every
deploy: if the columns are already there, the command is a no-op.

Usage:
    python manage.py heal_store_schema
"""
from django.core.management.base import BaseCommand
from django.db import connection


REQUIRED_COLUMNS = [
    # (column_name, postgres_type, fallback_default_sql)
    ("refresh_token",             "VARCHAR(500)", "''"),
    ("token_expires_at",          "TIMESTAMP WITH TIME ZONE", "NULL"),
    ("refresh_token_expires_at",  "TIMESTAMP WITH TIME ZONE", "NULL"),
]


class Command(BaseCommand):
    help = "Idempotently ensure stores_store has the Shopify refresh_token columns."

    def handle(self, *args, **kwargs):
        with connection.cursor() as cur:
            vendor = connection.vendor  # 'postgresql' or 'sqlite'

            for col, col_type, default in REQUIRED_COLUMNS:
                if vendor == "postgresql":
                    # Postgres supports the cleanest idempotent path.
                    sql = (
                        f"ALTER TABLE stores_store "
                        f"ADD COLUMN IF NOT EXISTS {col} {col_type} "
                        f"DEFAULT {default}"
                    )
                    try:
                        cur.execute(sql)
                        self.stdout.write(self.style.SUCCESS(
                            f"  ✓ ensured column: {col}"
                        ))
                    except Exception as e:
                        self.stdout.write(self.style.WARNING(
                            f"  ⚠ {col}: {e}"
                        ))
                else:
                    # SQLite — check first, then add (no IF NOT EXISTS on ADD COLUMN).
                    cur.execute("PRAGMA table_info('stores_store')")
                    existing = {row[1] for row in cur.fetchall()}
                    if col in existing:
                        self.stdout.write(f"  • column already present: {col}")
                        continue
                    sqlite_type = "TEXT" if "VARCHAR" in col_type else "TEXT"
                    cur.execute(
                        f"ALTER TABLE stores_store ADD COLUMN {col} {sqlite_type} DEFAULT {default}"
                    )
                    self.stdout.write(self.style.SUCCESS(
                        f"  ✓ added column: {col}"
                    ))

            # Mark the migration as applied so future `migrate` runs don't
            # try to re-add the columns (which would fail on a fresh DB
            # without the migration record).
            try:
                cur.execute(
                    "INSERT INTO django_migrations (app, name, applied) "
                    "VALUES ('stores', '0003_add_shopify_refresh_token', NOW()) "
                    "ON CONFLICT DO NOTHING"
                    if vendor == "postgresql" else
                    "INSERT OR IGNORE INTO django_migrations (app, name, applied) "
                    "VALUES ('stores', '0003_add_shopify_refresh_token', datetime('now'))"
                )
            except Exception:
                pass

        self.stdout.write(self.style.SUCCESS("stores_store schema healed."))
