"""Daily cron: send vendor 7-day quote reminders + tenant 14-day escalations.

Usage:
    python manage.py send_quote_reminders

Scheduling: hook this up via Railway cron daily. The job is idempotent — each
(assignment, reminder kind) pair is logged in QuoteReminderLog and only fires
once.
"""
from django.core.management.base import BaseCommand

from vendors.services import send_quote_reminders


class Command(BaseCommand):
    help = "Send vendor pricing SLA reminders (7-day vendor + 14-day tenant)."

    def handle(self, *args, **options):
        result = send_quote_reminders()
        self.stdout.write(self.style.SUCCESS(
            f"Done. vendor_reminders_sent={result['vendor_reminders_sent']} "
            f"tenant_escalations_sent={result['tenant_escalations_sent']} "
            f"skipped={result['skipped']} failed={result['failed']}"
        ))
