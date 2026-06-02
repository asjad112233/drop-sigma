from django.apps import AppConfig


class OrdersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'orders'

    def ready(self):
        # Boot the webhook sentinel — the daemon that guarantees every
        # connected store keeps its real-time order webhook registered
        # and pointing at our current public URL.
        #
        # Idempotent + skipped automatically in management-command and
        # test contexts (see _should_run_sentinel), so calling this
        # unconditionally from ready() is safe.
        try:
            from . import webhook_sentinel
            webhook_sentinel.start_sentinel()
        except Exception:
            # Never let a sentinel boot failure prevent Django from
            # serving traffic. Errors get logged inside the module.
            import logging
            logging.getLogger(__name__).exception(
                "webhook sentinel failed to start (non-fatal)"
            )
