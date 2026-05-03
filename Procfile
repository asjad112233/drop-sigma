web: python manage.py migrate --noinput && python manage.py ensure_superuser && gunicorn core.wsgi --bind 0.0.0.0:$PORT --workers 2 --timeout 120
