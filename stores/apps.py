import os
import threading
from django.apps import AppConfig


class StoresConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'stores'

    def ready(self):
        import stores.signals  # noqa: F401

        # Auto-start cloudflared tunnel in development.
        # RUN_MAIN=true means we're in the child reloader process (actual app).
        # Without this guard, ready() fires twice and starts two tunnels.
        if os.environ.get('RUN_MAIN') == 'true':
            from stores.tunnel import start
            threading.Thread(target=start, daemon=True).start()
