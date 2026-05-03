#!/bin/bash
echo "=== App starting ==="
echo "Python: $(python --version)"
echo "PORT: ${PORT}"

echo "=== Running migrations ==="
python manage.py migrate --noinput
echo "=== Migrations done ==="

echo "=== Running ensure_superuser ==="
python manage.py ensure_superuser || echo "WARNING: ensure_superuser failed, continuing..."
echo "=== ensure_superuser done ==="

echo "=== Starting gunicorn on ${PORT:-8080} ==="
exec gunicorn core.wsgi \
    --bind "0.0.0.0:${PORT:-8080}" \
    --workers 1 \
    --timeout 120 \
    --log-level info \
    --access-logfile - \
    --error-logfile -
