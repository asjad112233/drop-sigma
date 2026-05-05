from django.db import models
from django.contrib.auth.models import User


class TeamMember(models.Model):
    ROLE_CHOICES = (
        ("support", "Support"),
        ("order_manager", "Order Manager"),
        ("refund_manager", "Refund Manager"),
        ("vendor_manager", "Vendor Manager"),
        ("email_manager", "Email Manager"),
    )

    STATUS_CHOICES = (
        ("available", "Available"),
        ("busy", "Busy"),
        ("limited", "Limited"),
        ("offline", "Offline"),
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="team_profile")

    name = models.CharField(max_length=255)
    email = models.EmailField()

    role = models.CharField(max_length=50, choices=ROLE_CHOICES, default="support")

    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default="available")
    workload = models.IntegerField(default=0)

    is_active = models.BooleanField(default=True)

    permissions = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.name} ({self.role})"


class AssignmentRule(models.Model):
    RULE_TYPE_CHOICES = (
        ("tracking_missing", "Tracking Missing"),
        ("refund_dispute", "Refund / Dispute"),
        ("new_order", "New Order"),
        ("failed_payment", "Failed Payment"),
    )

    ASSIGN_TO_ROLE_CHOICES = (
        ("support", "Support"),
        ("order_manager", "Order Manager"),
        ("refund_manager", "Refund Manager"),
        ("vendor_manager", "Vendor Manager"),
        ("email_manager", "Email Manager"),
    )

    rule_type = models.CharField(max_length=50, choices=RULE_TYPE_CHOICES)
    assign_to_role = models.CharField(max_length=50, choices=ASSIGN_TO_ROLE_CHOICES)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.rule_type} → {self.assign_to_role}"


# ─── Team Chat ────────────────────────────────────────────────────────────────

class ChatChannel(models.Model):
    name        = models.CharField(max_length=100)
    slug        = models.SlugField(unique=True)
    description = models.CharField(max_length=255, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"#{self.name}"


class ChatMessage(models.Model):
    channel    = models.ForeignKey(ChatChannel, on_delete=models.CASCADE, related_name="messages")
    sender     = models.ForeignKey(User, on_delete=models.CASCADE, related_name="chat_messages")
    content    = models.TextField()
    parent     = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="replies")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.sender} → #{self.channel.name}: {self.content[:40]}"


class ChatReaction(models.Model):
    message = models.ForeignKey(ChatMessage, on_delete=models.CASCADE, related_name="reactions")
    sender  = models.ForeignKey(User, on_delete=models.CASCADE, related_name="chat_reactions")
    emoji   = models.CharField(max_length=10)

    class Meta:
        unique_together = ("message", "sender", "emoji")