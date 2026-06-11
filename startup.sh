#!/bin/sh
echo "=== App starting ==="
echo "Python: $(python --version 2>&1)"
echo "PORT: ${PORT}"

# ── Migrations ─────────────────────────────────────────────────────
# Old script used `timeout 30` — too short. A single migration adding
# many columns + foreign-key indexes (e.g. orders.0012 with 13 fields
# + 2 FK indexes) can easily exceed 30 s on a busy production DB,
# silently dropped the schema change, and left every endpoint that
# touched the affected model returning HTTP 500 until manually
# repaired. 300 s gives every realistic migration the room it needs;
# anything longer than that is genuinely stuck and a manual heal
# is the right tool.
echo "=== Running migrations ==="
timeout 300 python manage.py migrate --noinput
MIGRATE_RC=$?
if [ "$MIGRATE_RC" -ne 0 ]; then
    echo "WARNING: migrate exited with code $MIGRATE_RC, attempting heal_schema fallback…"
fi
echo "=== Migrations done ==="

# ── Schema safety net ──────────────────────────────────────────────
# heal_schema scans every Django model field and adds any column that
# the DB is missing. Defends against:
#   1. A migration timing out mid-flight on a previous deploy.
#   2. A migration getting dropped during a revert without unmarking
#      its django_migrations entry.
#   3. A manual schema-drift fix landing on prod but not in the
#      migration history.
# It's idempotent + safe to run every boot; existing columns are
# left untouched.
echo "=== Running heal_schema ==="
timeout 60 python manage.py heal_schema || echo "WARNING: heal_schema failed, continuing…"
echo "=== heal_schema done ==="

echo "=== Running ensure_superuser ==="
timeout 15 python manage.py ensure_superuser || echo "WARNING: ensure_superuser timed out or failed, continuing..."
echo "=== ensure_superuser done ==="

echo "=== Starting gunicorn on port ${PORT:-8080} ==="
exec gunicorn core.wsgi \
    --bind "0.0.0.0:${PORT:-8080}" \
    --workers 1 \
    --timeout 120 \
    --log-level info \
    --access-logfile - \
    --error-logfile -
