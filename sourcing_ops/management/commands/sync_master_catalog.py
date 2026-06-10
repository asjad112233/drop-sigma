"""Sync every active tenant store's products into TenantProductCache,
then recompute the CrossTenantSKU aggregates.

Run manually:
    python manage.py sync_master_catalog
    python manage.py sync_master_catalog --store-id 12
    python manage.py sync_master_catalog --demo

Designed to be cron-safe (idempotent — same input → same output) and
to skip stores without API credentials unless --demo is passed.
"""
from django.core.management.base import BaseCommand

from sourcing_ops.views_catalog import run_sync


class Command(BaseCommand):
    help = "Sync all tenant stores' products into TenantProductCache."

    def add_arguments(self, parser):
        parser.add_argument(
            "--store-id", type=int, default=None,
            help="Only sync this specific store (skips all others).",
        )
        parser.add_argument(
            "--demo", action="store_true",
            help="Use demo catalog data for stores with no credentials.",
        )

    def handle(self, *args, **options):
        store_id   = options.get("store_id")
        allow_demo = bool(options.get("demo"))

        self.stdout.write(self.style.NOTICE(
            f"Starting master catalog sync "
            f"(store_id={store_id or 'all'}, demo={allow_demo})..."
        ))

        result = run_sync(store_id=store_id, allow_demo=allow_demo)

        self.stdout.write(self.style.SUCCESS(
            f"  Stores synced:  {result['stores_synced']} / "
            f"{result['stores_total']}"
        ))
        if result["stores_failed"]:
            self.stdout.write(self.style.ERROR(
                f"  Stores failed:  {result['stores_failed']}"
            ))
        if result["stores_skipped"]:
            self.stdout.write(self.style.WARNING(
                f"  Stores skipped: {result['stores_skipped']} "
                f"(no credentials; pass --demo to test)"
            ))
        self.stdout.write(
            f"  Products cached: {result['products_saved']}"
        )
        self.stdout.write(
            f"  Unique SKUs:     {result['unique_skus']}"
        )
        for err in result["errors"]:
            self.stdout.write(self.style.ERROR(
                f"    • store #{err['store_id']} {err['store_name']}: "
                f"{err['error']}"
            ))
        self.stdout.write(self.style.SUCCESS(
            f"Done — synced {result['stores_synced']} stores, "
            f"{result['products_saved']} products, "
            f"{result['unique_skus']} unique SKUs."
        ))
