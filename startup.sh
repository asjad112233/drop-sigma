#!/bin/sh
echo "=== App starting ==="
echo "Python: $(python --version 2>&1)"
echo "PORT: ${PORT}"

echo "=== Running migrations ==="
timeout 30 python manage.py migrate --noinput || echo "WARNING: migrate failed, continuing..."
echo "=== Migrations done ==="

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
