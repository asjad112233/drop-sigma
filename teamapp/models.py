import uuid
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


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

    owner = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name="owned_team_members")
    user  = models.ForeignKey(User, on_delete=models.CASCADE, related_name="team_profile")

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

    owner          = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name="owned_assignment_rules")
    rule_type      = models.CharField(max_length=50, choices=RULE_TYPE_CHOICES)
    assign_to_role = models.CharField(max_length=50, choices=ASSIGN_TO_ROLE_CHOICES)
    is_active      = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.rule_type} → {self.assign_to_role}"


# ─── Team Chat ────────────────────────────────────────────────────────────────

class ChatChannel(models.Model):
    name         = models.CharField(max_length=100)
    slug         = models.SlugField(unique=True)
    description  = models.CharField(max_length=255, blank=True)
    is_dm        = models.BooleanField(default=False)
    participants = models.ManyToManyField(User, blank=True, related_name="dm_channels")
    members      = models.ManyToManyField(User, blank=True, related_name="channel_memberships", through="ChannelMember")
    created_at   = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"#{self.name}"


class ChannelMember(models.Model):
    channel   = models.ForeignKey(ChatChannel, on_delete=models.CASCADE, related_name="memberships")
    user      = models.ForeignKey(User, on_delete=models.CASCADE, related_name="channel_member_records")
    joined_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("channel", "user")


class ChatMessage(models.Model):
    channel    = models.ForeignKey(ChatChannel, on_delete=models.CASCADE, related_name="messages")
    sender     = models.ForeignKey(User, on_delete=models.CASCADE, related_name="chat_messages")
    content    = models.TextField(blank=True, default="")
    image      = models.FileField(upload_to="chat_images/", null=True, blank=True)
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


class ChatReadReceipt(models.Model):
    user        = models.ForeignKey(User, on_delete=models.CASCADE, related_name="chat_read_receipts")
    channel     = models.ForeignKey(ChatChannel, on_delete=models.CASCADE, related_name="read_receipts")
    last_read_at = models.DateTimeField()

    class Meta:
        unique_together = ("user", "channel")


# ─── Task Manager ─────────────────────────────────────────────────────────────

class Task(models.Model):
    PRIORITY_CHOICES = (
        ("high",   "High"),
        ("medium", "Medium"),
        ("low",    "Low"),
    )
    STATUS_CHOICES = (
        ("todo",        "To Do"),
        ("in_progress", "In Progress"),
        ("done",        "Done"),
    )
    CATEGORY_CHOICES = (
        ("orders",  "Orders"),
        ("refunds", "Refunds"),
        ("vendor",  "Vendor"),
        ("support", "Support"),
        ("email",   "Email"),
        ("other",   "Other"),
    )

    owner       = models.ForeignKey(User, on_delete=models.CASCADE, related_name="owned_tasks")
    title       = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    assigned_to = models.ForeignKey(TeamMember, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_tasks")
    priority    = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default="medium")
    category    = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="other")
    status      = models.CharField(max_length=20, choices=STATUS_CHOICES, default="todo")
    progress    = models.IntegerField(default=0)
    due_date    = models.DateField(null=True, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class TaskComment(models.Model):
    task       = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="comments")
    author     = models.ForeignKey(User, on_delete=models.CASCADE, related_name="task_comments")
    content    = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.author} on {self.task}: {self.content[:40]}"


# ─── Employee Invitation ───────────────────────────────────────────────────────

class EmployeeInvitation(models.Model):
    STATUS_CHOICES = (
        ("pending",  "Pending"),
        ("accepted", "Accepted"),
        ("expired",  "Expired"),
    )

    token       = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    owner       = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sent_invitations")
    name        = models.CharField(max_length=255)
    email       = models.EmailField()
    role        = models.CharField(max_length=50, default="support")
    initial_status = models.CharField(max_length=50, default="available")
    permissions = models.JSONField(default=dict, blank=True)
    status      = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    expires_at  = models.DateTimeField()
    created_at  = models.DateTimeField(auto_now_add=True)

    def is_valid(self):
        return self.status == "pending" and timezone.now() < self.expires_at

    def __str__(self):
        return f"Invitation → {self.email} ({self.status})"