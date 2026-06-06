from django.apps import AppConfig


class CoreappConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'coreapp'

    def ready(self):
        # Boot-time schema audit — last line of defense against the
        # phantom-migration bug class.
        #
        # Django can sometimes mark a migration as applied without the
        # underlying DDL actually running (managed-Postgres quirks). The
        # symptom is a runtime "column does not exist" error on every
        # request that touches the affected model — which is almost
        # impossible to diagnose from frontend logs alone.
        #
        # This audit runs `heal_schema --dry-run` immediately after
        # Django boots. If ANY model has a field whose column is
        # missing from the database, it logs CRITICAL with the table +
        # column name so the next Railway deploy log surfaces the
        # problem in red instead of silently serving 500s.
        #
        # Skipped in test / migration / management contexts so we don't
        # log spurious warnings when the DB is intentionally being
        # rebuilt.
        try:
            from .startup_audit import audit_schema_on_boot
            audit_schema_on_boot()
        except Exception:
            # Never let an audit failure prevent Django from serving
            # traffic. The audit is a diagnostic, not a gate.
            import logging
            logging.getLogger(__name__).exception(
                "schema audit failed to run (non-fatal)"
            )
