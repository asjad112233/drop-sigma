"""Daily cron: renew Gmail push watches before they expire.

Gmail caps users.watch() at 7 days. If a watch expires, Gmail stops pushing
notifications and we silently fall back to whatever the user's other sync
mechanism is. Run this once a day (e.g. via Railway cron) to keep watches
alive for every OAuth-connected mailbox.

Usage:
    python manage.py renew_gmail_watches
    python manage.py renew_gmail_watches --all   # ignore expiration window, refresh every account
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from emails.models import EmailAccount
from emails.services import start_gmail_watch


class Command(BaseCommand):
    help = "Renew Gmail Pub/Sub watches for OAuth-connected mailboxes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all", action="store_true",
            help="Renew every OAuth account, not just ones nearing expiration.",
        )
        parser.add_argument(
            "--hours", type=int, default=48,
            help="Renew watches expiring within this many hours (default: 48).",
        )

    def handle(self, *args, **opts):
        renew_all = opts.get("all", False)
        threshold_hours = opts.get("hours", 48)
        now = timezone.now()
        threshold = now + timedelta(hours=threshold_hours)

        qs = EmailAccount.objects.filter(
            auth_type="oauth",
            is_active=True,
        ).exclude(oauth_refresh_token="")

        if not renew_all:
            # Include accounts whose watch never started yet (expiration is null)
            # AND accounts expiring within the threshold window.
            from django.db.models import Q
            qs = qs.filter(Q(gmail_watch_expiration__isnull=True)
                           | Q(gmail_watch_expiration__lte=threshold))

        total = qs.count()
        renewed, failed = 0, 0
        for account in qs:
            try:
                ok, info = start_gmail_watch(account)
                if ok:
                    renewed += 1
                    self.stdout.write(self.style.SUCCESS(
                        f"  ✓ {account.email} → exp {info.get('expiration') if isinstance(info, dict) else info}"
                    ))
                else:
                    failed += 1
                    self.stdout.write(self.style.WARNING(f"  ✗ {account.email}: {info}"))
            except Exception as e:
                failed += 1
                self.stdout.write(self.style.ERROR(f"  ✗ {account.email}: {e}"))

        self.stdout.write(self.style.SUCCESS(
            f"Done. {renewed}/{total} renewed, {failed} failed."
        ))
