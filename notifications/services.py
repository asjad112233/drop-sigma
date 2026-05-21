"""
Lightweight notification helpers. Import `notify()` anywhere in the codebase
to record a notification for a user — same function works across admin /
vendor / employee audiences.

Failures are swallowed silently so a notification bug can never break the
upstream business event (e.g. an order create must still succeed even if
the notification table is misconfigured).
"""

import logging

log = logging.getLogger("dropsigma.notifications")

# Category → default FA icon mapping
DEFAULT_ICONS = {
    "order":    "fa-solid fa-cart-shopping",
    "vendor":   "fa-solid fa-handshake",
    "tracking": "fa-solid fa-truck-fast",
    "email":    "fa-solid fa-envelope",
    "chat":     "fa-solid fa-comments",
    "task":     "fa-solid fa-list-check",
    "store":    "fa-solid fa-shop",
    "payment":  "fa-solid fa-credit-card",
    "system":   "fa-solid fa-gear",
    "ai":       "fa-solid fa-wand-magic-sparkles",
}


def notify(
    recipient,
    audience,
    category,
    title,
    body="",
    priority="medium",
    icon=None,
    action_url="",
    action_label="",
    related_order_id=None,
    related_email_id=None,
    related_task_id=None,
    **metadata,
):
    """Create a Notification row. Returns the Notification or None on failure.

    `recipient` may be a User instance or a user id. Empty/None recipient is
    a silent no-op (e.g. when a vendor has no linked User account yet).
    """
    if recipient is None:
        return None

    recipient_id = recipient.id if hasattr(recipient, "id") else recipient
    if not recipient_id:
        return None

    try:
        # Local import keeps this helper safe to import during app loading.
        from .models import Notification
        return Notification.objects.create(
            recipient_id=recipient_id,
            audience=audience,
            category=category,
            title=title[:255],
            body=body or "",
            priority=priority,
            icon=icon or DEFAULT_ICONS.get(category, "fa-regular fa-bell"),
            action_url=action_url or "",
            action_label=action_label or "",
            related_order_id=related_order_id,
            related_email_id=related_email_id,
            related_task_id=related_task_id,
            metadata=metadata or {},
        )
    except Exception as e:
        log.warning("notify() failed: %s", e)
        return None
