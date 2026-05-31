"""
Seed realistic demo notifications for testing the notification bar UI.

Usage:
  python manage.py seed_notifications                    # all users get demo data
  python manage.py seed_notifications --user asjad       # specific user (username/email)
  python manage.py seed_notifications --clear            # wipe existing first
  python manage.py seed_notifications --portal admin     # only admin-audience notifs
"""

import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone

from notifications.models import Notification


ADMIN_TEMPLATES = [
    ("order",    "high",   "fa-solid fa-cart-shopping",
     "New order #{n} from Kayali Store",
     "Tyrone Sallette ordered 3 items totalling $418.00. Awaiting vendor assignment."),
    ("tracking", "medium", "fa-solid fa-clipboard-check",
     "Vendor submitted tracking for #{n}",
     "Maria Foo submitted tracking number LX9384721. Awaiting your approval."),
    ("email",    "medium", "fa-solid fa-envelope",
     "New customer email — Refund request",
     "Sarah K. wrote: \"Hi, I would like a refund for order #{n} because the item arrived damaged.\""),
    ("ai",       "medium", "fa-solid fa-wand-magic-sparkles",
     "AI draft ready for ticket #{n}",
     "Drop Sigma AI has drafted a reply for the dispute ticket. Review before sending."),
    ("store",    "urgent", "fa-solid fa-triangle-exclamation",
     "Store offline — Inglock We Trust",
     "Health check failed 3 times in a row. API key may have expired or store may be down."),
    ("payment",  "high",   "fa-solid fa-credit-card",
     "Payment failed for order #{n}",
     "Stripe declined charge $128.50 (insufficient funds). Customer has been notified automatically."),
    ("vendor",   "low",    "fa-solid fa-handshake",
     "Vendor invitation accepted",
     "David Lin joined as a vendor for Kayali store and can now access the vendor portal."),
    ("chat",     "low",    "fa-solid fa-comments",
     "New message in #operations",
     "Maria: \"USPS customs hold at LAX today — ETA pushed by 24h on 4 shipments.\""),
    ("tracking", "low",    "fa-solid fa-box-archive",
     "Order #{n} delivered",
     "Tracked status auto-updated by live tracking sync. Customer can now leave a review."),
    ("system",   "low",    "fa-solid fa-gear",
     "Auto-Email rule activated",
     "Order Confirmation template is now sending automatically on every new order."),
    ("order",    "medium", "fa-solid fa-cart-shopping",
     "12 new orders synced from store",
     "Daily auto-sync completed. 3 orders require permanent vendor assignment."),
    ("tracking", "low",    "fa-solid fa-route",
     "Live tracking sync re-enabled",
     "Playwright tracking scraper restored. All pending shipments now polling every 4 hours."),
]

VENDOR_TEMPLATES = [
    ("order",    "high",   "fa-solid fa-cart-shopping",
     "New order assigned: #{n}",
     "Christina Alders ordered Vanilla Candy Rock Sugar | 42 · 100ml × 1. Address: Toronto, CA."),
    ("tracking", "medium", "fa-solid fa-circle-check",
     "Tracking approved for #{n}",
     "Your tracking LX9384721 was approved. Order status updated to Shipped automatically."),
    ("tracking", "high",   "fa-solid fa-circle-exclamation",
     "Tracking rejected — please resubmit for #{n}",
     "Admin note: \"Tracking number format looks invalid. Please verify courier and resubmit.\""),
    ("vendor",   "low",    "fa-solid fa-thumbtack",
     "Permanent assignment activated",
     "You are now the default vendor for product PRD-882. All new orders auto-route to you."),
    ("chat",     "medium", "fa-solid fa-comments",
     "Message from admin",
     "Asjad: \"Maria, can you confirm if you have stock for PRD-991 before Tuesday?\""),
    ("task",     "medium", "fa-solid fa-box",
     "Stock arrival confirmation requested",
     "Admin needs you to confirm physical arrival of 240 units (PRD-882). Mark on Stock tab."),
    ("order",    "low",    "fa-solid fa-cart-shopping",
     "Order #{n} marked shipped by customer",
     "Tracking page accessed by Christina at 02:34 PM."),
    ("system",   "low",    "fa-solid fa-key",
     "Login credentials updated",
     "Your vendor account password was reset by admin."),
]

EMPLOYEE_TEMPLATES = [
    ("task",     "high",   "fa-solid fa-list-check",
     "New task assigned: Process refund for #{n}",
     "Sarah K. requested a refund. Verify the dispute and process via Stripe within 48h."),
    ("email",    "medium", "fa-solid fa-envelope",
     "Customer replied on thread #{n}",
     "James W: \"Thank you, the refund has been received. Could I get a discount code for next time?\""),
    ("chat",     "high",   "fa-solid fa-at",
     "You were mentioned in #support",
     "Asjad: \"@hira can you handle the dispute on #{n}? Customer is upset.\""),
    ("chat",     "low",    "fa-solid fa-comments",
     "New message in #operations",
     "Maria: \"USPS customs hold at LAX today, FYI to all order managers — 24h ETA delay.\""),
    ("task",     "low",    "fa-solid fa-circle-check",
     "Task completed by teammate",
     "Maria K. closed \"Reply to vendor about PRD-991 stock\". You were a watcher."),
    ("order",    "medium", "fa-solid fa-cart-shopping",
     "Order #{n} assigned to you",
     "Refund flow triggered. Original total: $214.50. Refund within 24h SLA."),
    ("task",     "high",   "fa-solid fa-clock",
     "Task overdue: Reply to vendor",
     "Due 1 hour ago. Original deadline: today, 12:00 PM."),
    ("system",   "low",    "fa-solid fa-shield-check",
     "Your role permissions updated",
     "Admin granted you \"approve_refund_over_500\" permission."),
]


class Command(BaseCommand):
    help = "Seed demo notifications for testing the notification bar UI."

    def add_arguments(self, parser):
        parser.add_argument("--user", type=str, help="Username or email (default: all active users)")
        parser.add_argument("--clear", action="store_true", help="Delete existing notifications first")
        parser.add_argument("--portal", choices=["admin", "vendor", "employee", "all"], default="all")
        parser.add_argument("--count", type=int, default=14, help="Number per portal (default 14)")

    def handle(self, *args, **opts):
        users_qs = User.objects.filter(is_active=True)
        if opts["user"]:
            users_qs = users_qs.filter(username=opts["user"]) | User.objects.filter(email=opts["user"])
        users = list(users_qs)
        if not users:
            self.stdout.write(self.style.WARNING("No users found."))
            return

        if opts["clear"]:
            deleted = Notification.objects.filter(recipient__in=users).delete()
            self.stdout.write(self.style.WARNING(f"Cleared {deleted[0]} existing notifications."))

        portals_to_seed = [opts["portal"]] if opts["portal"] != "all" else ["admin", "vendor", "employee"]

        template_map = {
            "admin":    ADMIN_TEMPLATES,
            "vendor":   VENDOR_TEMPLATES,
            "employee": EMPLOYEE_TEMPLATES,
        }

        now = timezone.now()
        total_created = 0

        for user in users:
            for portal in portals_to_seed:
                templates = template_map[portal]
                count = opts["count"]
                for i in range(count):
                    cat, pri, icon, title_t, body_t = random.choice(templates)
                    order_num = random.randint(11400, 11599)
                    title = title_t.format(n=order_num)
                    body  = body_t.format(n=order_num)
                    # Spread across the last 3 days, more recent items first
                    age_minutes = int((i / count) * (3 * 24 * 60))
                    created_at = now - timedelta(minutes=age_minutes + random.randint(0, 20))
                    is_read = age_minutes > 90 and random.random() < 0.65

                    n = Notification.objects.create(
                        recipient=user,
                        audience=portal,
                        category=cat,
                        priority=pri,
                        title=title,
                        body=body,
                        icon=icon,
                        is_read=is_read,
                        read_at=created_at if is_read else None,
                        related_order_id=order_num,
                        action_url=f"/dashboard/#order/{order_num}" if cat == "order" else "",
                        action_label="View" if cat == "order" else "",
                    )
                    # Override auto_now_add so we get realistic timestamps
                    Notification.objects.filter(pk=n.pk).update(created_at=created_at)
                    total_created += 1

            self.stdout.write(self.style.SUCCESS(
                f"  {user.username or user.email}: seeded {total_created} notifications"
            ))

        self.stdout.write(self.style.SUCCESS(f"\nDone. Total created: {total_created}"))
