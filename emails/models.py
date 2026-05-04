from django.db import models
from stores.models import Store
from orders.models import Order


class EmailAccount(models.Model):
    store = models.ForeignKey(
        Store,
        on_delete=models.CASCADE,
        related_name="email_accounts"
    )

    email = models.EmailField()
    app_password = models.CharField(max_length=255)

    imap_host = models.CharField(max_length=255, default="imap.gmail.com")
    imap_port = models.IntegerField(default=993)

    smtp_host = models.CharField(max_length=255, default="smtp.gmail.com")
    smtp_port = models.IntegerField(default=587)

    auth_type = models.CharField(max_length=20, default="password")  # 'password' or 'oauth'
    oauth_refresh_token = models.TextField(blank=True, default="")

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Sync settings
    last_synced = models.DateTimeField(null=True, blank=True)
    fetch_limit = models.IntegerField(default=30)
    sync_folder = models.CharField(max_length=100, default="INBOX")
    mark_read_in_gmail = models.BooleanField(default=False)
    sync_on_tab_focus = models.BooleanField(default=True)

    # AI settings
    ai_tone = models.CharField(max_length=50, default="friendly")
    ai_language = models.CharField(max_length=50, default="english")
    ai_auto_suggest = models.BooleanField(default=True)
    ai_auto_draft = models.BooleanField(default=False)
    ai_include_order = models.BooleanField(default=True)

    # Signature
    signature = models.TextField(blank=True, default="")

    # Notifications
    notify_browser = models.BooleanField(default=True)
    notify_sound = models.BooleanField(default=False)
    notify_unread_only = models.BooleanField(default=True)
    notify_assigned_only = models.BooleanField(default=False)

    # Thread behavior
    auto_close_after_reply = models.BooleanField(default=False)
    auto_mark_read_on_open = models.BooleanField(default=True)
    show_cc_bcc = models.BooleanField(default=False)

    # Auto email on order status change
    auto_email_enabled = models.BooleanField(default=False)
    templates_seeded = models.BooleanField(default=False)

    def __str__(self):
        return self.email


class EmailMessage(models.Model):
    STATUS_CHOICES = (
        ("new", "New"),
        ("assigned", "Assigned"),
        ("drafted", "AI Drafted"),
        ("replied", "Replied"),
        ("closed", "Closed"),
    )

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="emails")
    order = models.ForeignKey(
        Order,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="emails"
    )

    sender = models.EmailField(blank=True, null=True)
    recipient = models.EmailField(blank=True, null=True)

    sender_name = models.CharField(max_length=255, blank=True, null=True)

    subject = models.CharField(max_length=255, blank=True, null=True)
    body = models.TextField(blank=True, null=True)

    # 📊 Category (refund, shipping, etc)
    category = models.CharField(max_length=50, default="general")

    # 🟢 NEW: Read / Unread tracking
    is_read = models.BooleanField(default=False)

    # 🟢 NEW: Gmail unique ID (duplicate prevent + threading)
    gmail_uid = models.CharField(max_length=255, blank=True, null=True)

    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default="new")
    ai_draft = models.TextField(blank=True, null=True)

    raw_data = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.subject or "No Subject"


class EmailThreadAssignment(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="thread_assignments")
    contact = models.EmailField()
    assigned_to = models.ForeignKey(
        'teamapp.TeamMember',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_threads"
    )
    co_assignees = models.ManyToManyField(
        'teamapp.TeamMember',
        blank=True,
        related_name="co_assigned_threads"
    )
    assigned_at = models.DateTimeField(auto_now=True)
    is_resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [('store', 'contact')]

    def __str__(self):
        return f"{self.contact} → {self.assigned_to}"


class EmailAttachment(models.Model):
    email = models.ForeignKey(EmailMessage, on_delete=models.CASCADE, related_name="attachments")
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100, blank=True)
    file = models.FileField(upload_to="email_attachments/")
    size = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.filename


class EmailTemplate(models.Model):
    CATEGORY_CHOICES = [
        ('order', 'Order Confirmation'),
        ('shipping', 'Shipping Notification'),
        ('cancelled', 'Order Cancelled'),
        ('failed', 'Payment Failed'),
        ('refund', 'Refund'),
        ('dispute', 'Dispute'),
        ('welcome', 'Welcome'),
        ('followup', 'Follow-up'),
        ('custom', 'Custom'),
    ]
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('draft', 'Draft'),
        ('archived', 'Archived'),
    ]
    TRIGGER_CHOICES = [
        ('manual', 'Manual Only'),
        ('order_placed', 'Order Placed'),
        ('tracking_added', 'Tracking Added'),
        ('order_cancelled', 'Order Cancelled'),
        ('payment_failed', 'Payment Failed'),
        ('order_delivered', 'Order Delivered'),
        ('no_activity_7d', '7 Days No Activity'),
    ]

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='email_templates', null=True, blank=True)
    is_global = models.BooleanField(default=False)

    # Basic info
    name = models.CharField(max_length=255, default='Untitled Template')
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='custom')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    description = models.TextField(blank=True, default='')
    tags = models.JSONField(default=list, blank=True)

    # Sender config
    from_email = models.EmailField(blank=True, null=True)
    sender_name = models.CharField(max_length=255, blank=True, default='')
    reply_to = models.EmailField(blank=True, null=True)
    cc_emails = models.JSONField(default=list, blank=True)
    bcc_emails = models.JSONField(default=list, blank=True)
    use_default_signature = models.BooleanField(default=True)
    custom_signature = models.TextField(blank=True, default='')

    # Content
    subject = models.CharField(max_length=500, blank=True, default='')
    preheader = models.CharField(max_length=255, blank=True, default='')
    body_html = models.TextField(blank=True, default='')
    footer = models.TextField(blank=True, default='')

    # Trigger
    trigger_type = models.CharField(max_length=50, choices=TRIGGER_CHOICES, default='manual')
    trigger_delay_minutes = models.IntegerField(default=0)
    working_hours_only = models.BooleanField(default=False)
    throttle_per_day = models.BooleanField(default=False)

    is_category_default = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return self.name