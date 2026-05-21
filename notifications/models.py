from django.db import models
from django.conf import settings


class Notification(models.Model):
    AUDIENCE_CHOICES = [
        ("admin",    "Admin (Tenant Owner)"),
        ("vendor",   "Vendor"),
        ("employee", "Team Member"),
    ]

    CATEGORY_CHOICES = [
        ("order",    "Order"),
        ("vendor",   "Vendor"),
        ("tracking", "Tracking"),
        ("email",    "Email"),
        ("chat",     "Chat"),
        ("task",     "Task"),
        ("store",    "Store"),
        ("payment",  "Payment"),
        ("system",   "System"),
        ("ai",       "AI"),
    ]

    PRIORITY_CHOICES = [
        ("low",     "Low"),
        ("medium",  "Medium"),
        ("high",    "High"),
        ("urgent",  "Urgent"),
    ]

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    audience = models.CharField(max_length=20, choices=AUDIENCE_CHOICES, db_index=True)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="system", db_index=True)
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default="medium")

    title    = models.CharField(max_length=255)
    body     = models.TextField(blank=True, default="")
    # FA icon class, e.g. "fa-solid fa-cart-shopping"
    icon     = models.CharField(max_length=80, blank=True, default="")

    action_url   = models.CharField(max_length=500, blank=True, default="")
    action_label = models.CharField(max_length=60, blank=True, default="")

    is_read = models.BooleanField(default=False, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    # Optional FK-by-id (kept loose so deletes don't cascade-orphan notifications)
    related_order_id = models.PositiveIntegerField(null=True, blank=True)
    related_email_id = models.PositiveIntegerField(null=True, blank=True)
    related_task_id  = models.PositiveIntegerField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "is_read", "-created_at"]),
            models.Index(fields=["recipient", "audience", "-created_at"]),
        ]

    def __str__(self):
        return f"[{self.audience}/{self.category}] {self.title} → {self.recipient_id}"
