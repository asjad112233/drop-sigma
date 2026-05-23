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

    # Gmail real-time push (Pub/Sub) — users.watch() bookkeeping.
    # history_id = last Gmail historyId we processed (for incremental fetch).
    # expiration = when the current watch lapses (Gmail caps watches at 7 days).
    gmail_watch_history_id = models.CharField(max_length=64, blank=True, default="")
    gmail_watch_expiration = models.DateTimeField(null=True, blank=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Sync settings
    last_synced = models.DateTimeField(null=True, blank=True)
    fetch_limit = models.IntegerField(default=30)
    sync_folder = models.CharField(max_length=100, default="INBOX")
    mark_read_in_gmail = models.BooleanField(default=False)
    sync_on_tab_focus = models.BooleanField(default=True)
    live_sync_enabled = models.BooleanField(default=True)
    sync_interval = models.IntegerField(default=60)

    # AI settings
    ai_tone = models.CharField(max_length=50, default="friendly")
    ai_language = models.CharField(max_length=50, default="english")
    ai_auto_suggest = models.BooleanField(default=True)
    ai_auto_draft = models.BooleanField(default=False)
    ai_include_order = models.BooleanField(default=True)

    # AI Auto-Reply mode: off / suggest / auto / hybrid
    AI_REPLY_MODES = (
        ("off",     "Off"),
        ("suggest", "Suggest Draft"),
        ("auto",    "Auto-Send"),
        ("hybrid",  "Hybrid (auto for simple, suggest for complex)"),
    )
    ai_reply_mode = models.CharField(max_length=20, choices=AI_REPLY_MODES, default="off")
    ai_custom_instructions = models.TextField(blank=True, default="")
    ai_use_signature = models.BooleanField(default=True)

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
    body_html = models.TextField(blank=True, default='')

    # 📊 Category (refund, shipping, etc)
    category = models.CharField(max_length=50, default="general")

    # 🟢 NEW: Read / Unread tracking
    is_read = models.BooleanField(default=False)

    # 🟢 NEW: Gmail unique ID (duplicate prevent + threading)
    gmail_uid = models.CharField(max_length=255, blank=True, null=True)

    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default="new")
    ai_draft = models.TextField(blank=True, null=True)

    # ─── AI-handling status (separate from operational `status`) ───────────
    # `auto_handled`     — AI drafted a high-confidence reply, ready to send
    # `review_suggested` — Draft generated but AI is unsure; humans should glance
    # `needs_human`      — Either AI explicitly escalated OR the catch-all rule
    #                       fired (no draft was produced for any reason)
    AI_STATUS_CHOICES = (
        ("auto_handled",     "Auto Handled"),
        ("review_suggested", "Review Suggested"),
        ("needs_human",      "Needs Human"),
    )
    AI_ESCALATION_CATEGORIES = (
        ("low_confidence",  "Low Confidence"),
        ("missing_context", "Missing Context"),
        ("ambiguous",       "Ambiguous"),
        ("sensitive",       "Sensitive"),
        ("policy_conflict", "Policy Conflict"),
        ("language",        "Language"),
        ("manual_request",  "Manual Request"),
        ("ai_failure",      "AI Failure"),  # catch-all when no draft generated
    )
    ai_status              = models.CharField(max_length=20, choices=AI_STATUS_CHOICES, default="auto_handled")
    ai_confidence_score    = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    ai_escalation_reason   = models.TextField(blank=True, default="")
    ai_escalation_category = models.CharField(max_length=30, choices=AI_ESCALATION_CATEGORIES, blank=True, default="")
    escalated_at           = models.DateTimeField(null=True, blank=True)
    resolved_by_human_at   = models.DateTimeField(null=True, blank=True)

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

# ════════════════════════════════════════════════════════════════════════════
# 🧠 AI Training Studio — Per-store profile + knowledge snippets
# ════════════════════════════════════════════════════════════════════════════

class AiTrainingProfile(models.Model):
    """
    Per-store AI training profile.
    Built progressively via the AI Setup Wizard, edited in AI Training Studio.
    Feeds into every AI reply (live emails + playground).
    """
    MODE_CHOICES = [
        ('auto',    'Fully Automatic'),
        ('draft',   'Draft & Review'),
        ('suggest', 'Suggest on Open'),
    ]

    store = models.OneToOneField(
        'stores.Store', on_delete=models.CASCADE,
        related_name='ai_training_profile'
    )

    # Brand identity
    business_name = models.CharField(max_length=255, blank=True, default='')
    niche         = models.CharField(max_length=255, blank=True, default='')
    description   = models.TextField(blank=True, default='')
    language      = models.CharField(max_length=100, blank=True, default='English')
    support_hours = models.CharField(max_length=255, blank=True, default='')

    # Tone & voice
    tones          = models.JSONField(default=list, blank=True)      # ["friendly","empathetic"]
    reply_length   = models.CharField(max_length=50, blank=True, default='Medium')
    signoff        = models.CharField(max_length=255, blank=True, default='')
    voice_example  = models.TextField(blank=True, default='')

    # AI behavior
    mode           = models.CharField(max_length=20, choices=MODE_CHOICES, default='draft')
    toggles        = models.JSONField(default=dict, blank=True)
    # toggles example:
    #   {"include_order_context": True, "auto_detect_language": True,
    #    "escalate_low_confidence": True, "use_brand_signature": True}

    # Wizard tracking
    wizard_answers = models.JSONField(default=dict, blank=True)
    wizard_completed_at = models.DateTimeField(blank=True, null=True)

    # Raw extras (anything not captured above)
    extras         = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"AI Profile · {self.business_name or self.store_id}"


class PendingAiTrainingSnapshot(models.Model):
    """When a tenant deletes a store without checking "also reset AI", we
    snapshot the AI training data here so it can be auto-restored if they
    later reconnect the SAME store URL. If they connect a DIFFERENT store
    URL we discard this snapshot — that's the "auto-reset on replacement"
    semantic the tenant asked for.

    One snapshot per user at a time — re-deleting overwrites the previous."""
    user                = models.OneToOneField(
        'auth.User', on_delete=models.CASCADE,
        related_name='pending_ai_training_snapshot'
    )
    source_store_url    = models.URLField(max_length=600)
    source_store_name   = models.CharField(max_length=255, blank=True, default='')
    profile_json        = models.JSONField(default=dict, blank=True)
    snippets_json       = models.JSONField(default=list, blank=True)
    feedbacks_json      = models.JSONField(default=list, blank=True)
    created_at          = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Pending AI snapshot for {self.user_id} ← {self.source_store_url}"


class KnowledgeSnippet(models.Model):
    """
    Knowledge base entry for a store's AI.
    AI picks the most relevant snippets per query to inject into the system prompt.
    """
    CATEGORY_CHOICES = [
        ('Shipping',  'Shipping'),
        ('Refund',    'Refund'),
        ('Product',   'Product'),
        ('Payment',   'Payment'),
        ('FAQ',       'FAQ'),
        ('Brand',     'Brand'),
        ('Support',   'Support'),
        ('AI',        'AI Behavior'),
        ('Guardrail', 'Guardrail'),
        # v2 topic categories (kept distinct so legacy snippets stay readable)
        ('Disputes',         'Disputes'),
        ('Address',          'Address Updates'),
        ('Cancellations',    'Cancellations'),
        ('OutOfStock',       'Out of Stock'),
        ('ShippingDelays',   'Shipping Delays'),
        ('ProductDefects',   'Product Defects'),
        ('WrongItem',        'Wrong Item Received'),
        ('PaymentIssues',    'Payment Issues'),
    ]

    store    = models.ForeignKey(
        'stores.Store', on_delete=models.CASCADE,
        related_name='knowledge_snippets'
    )
    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES, default='FAQ')
    title    = models.CharField(max_length=255)
    text     = models.TextField()

    from_wizard = models.BooleanField(default=False)
    order_idx   = models.IntegerField(default=0)

    # v2: per-snippet enable/disable. Topic-level toggle lives in
    # AiTrainingProfile.toggles and overrides this when off.
    is_enabled  = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order_idx', '-updated_at']

    def __str__(self):
        return f"[{self.category}] {self.title}"


# ════════════════════════════════════════════════════════════════════════════
# 📚 AI Reply Feedback / Corrections — quality-improvement loop
# ════════════════════════════════════════════════════════════════════════════

class AiReplyFeedback(models.Model):
    """
    Captures the difference between an AI-drafted reply and the version the
    admin actually sent (or a customer-complaint correction). Used as
    few-shot training data for future replies in the same store.
    """
    FEEDBACK_TYPES = [
        ('edit',      'Admin edited before sending'),
        ('reject',    'Admin discarded the AI draft'),
        ('approve',   'Admin sent as-is (positive signal)'),
        ('complaint', 'Customer complained about a sent reply'),
    ]

    store = models.ForeignKey(
        'stores.Store', on_delete=models.CASCADE,
        related_name='ai_reply_feedbacks'
    )
    email_message = models.ForeignKey(
        'emails.EmailMessage', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='ai_feedbacks'
    )

    feedback_type = models.CharField(max_length=20, choices=FEEDBACK_TYPES, default='edit')

    # Original AI-generated draft
    ai_draft       = models.TextField(blank=True, default='')
    # What the admin actually sent (or empty for reject/complaint)
    final_text     = models.TextField(blank=True, default='')
    # Optional admin-written note explaining the correction
    correction_note= models.TextField(blank=True, default='')

    # Customer-facing fields at the time the reply was generated
    customer_message = models.TextField(blank=True, default='')

    actor = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL,
        null=True, blank=True
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.feedback_type}] Store={self.store_id} #{self.id}"


# ════════════════════════════════════════════════════════════════════════════
# CUSTOM LABELS — tenants create their own email labels (colored chips) and
# assign them to threads. Scoped per-tenant (User) since multiple stores share
# the same user/owner. is_shared distinguishes team-wide vs personal labels.
# ════════════════════════════════════════════════════════════════════════════
class EmailLabel(models.Model):
    """User-defined email label. Tenant-scoped via `owner`."""
    owner       = models.ForeignKey(
        'auth.User', on_delete=models.CASCADE,
        related_name='email_labels'
    )
    name        = models.CharField(max_length=50)
    color       = models.CharField(max_length=12, default="#6366f1")  # hex
    icon        = models.CharField(max_length=8, blank=True, default="")  # emoji char
    is_shared   = models.BooleanField(default=True)                       # team-wide
    position    = models.IntegerField(default=0)                          # for ordering
    created_by  = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='created_email_labels'
    )
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['position', 'name']
        unique_together = [('owner', 'name')]

    def __str__(self):
        return f"{self.name} ({self.owner_id})"


class EmailThreadLabel(models.Model):
    """Junction: which thread (by contact email) has which label.
    Threads are identified by (store, contact) — matching EmailThreadAssignment.
    """
    store       = models.ForeignKey('stores.Store', on_delete=models.CASCADE,
                                    related_name='thread_labels')
    contact     = models.EmailField()
    label       = models.ForeignKey(EmailLabel, on_delete=models.CASCADE,
                                    related_name='thread_links')
    assigned_by = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL,
        null=True, blank=True
    )
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('store', 'contact', 'label')]
        indexes = [
            models.Index(fields=['store', 'contact']),
            models.Index(fields=['label']),
        ]


# ════════════════════════════════════════════════════════════════════════════
# AI FALLBACK LOG — every time the catch-all rule fires (no draft generated
# for any reason), log it for debugging. Separate from user-facing reasons.
# ════════════════════════════════════════════════════════════════════════════
class AiFallbackLog(models.Model):
    """Logs why the AI failed to generate a draft (technical reasons)."""
    REASONS = (
        ("api_timeout",       "API Timeout"),
        ("empty_response",    "Empty Response"),
        ("malformed_output",  "Malformed Output"),
        ("refused",           "AI Refused"),
        ("content_filter",    "Content Filter"),
        ("token_limit",       "Token Limit"),
        ("network_error",     "Network Error"),
        ("low_confidence",    "Low Confidence"),
        ("missing_context",   "Missing Context"),
        ("sensitive_keyword", "Sensitive Keyword"),
        ("manual_request",    "Customer Requested Human"),
        ("policy_conflict",   "Policy Conflict"),
        ("other",             "Other"),
    )
    email_message  = models.ForeignKey(
        'emails.EmailMessage', on_delete=models.CASCADE,
        related_name='fallback_logs'
    )
    store          = models.ForeignKey('stores.Store', on_delete=models.CASCADE,
                                       related_name='ai_fallback_logs')
    reason         = models.CharField(max_length=30, choices=REASONS)
    technical_note = models.TextField(blank=True, default="")
    created_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['store', 'created_at']),
            models.Index(fields=['reason']),
        ]


# ════════════════════════════════════════════════════════════════════════════
# FOLDER ASSIGNMENT — persistent rule that auto-assigns matching threads
# to a team member. Two kinds:
#   1) Built-in folder (inbox / unread / needs_human / refunds / etc.)
#   2) Custom label (FK to EmailLabel)
# When a new incoming email lands AND it matches an active rule, an
# EmailThreadAssignment is auto-created so the team member sees it in
# their employee portal automatically.
# ════════════════════════════════════════════════════════════════════════════
class FolderAssignment(models.Model):
    FOLDER_CHOICES = (
        ("inbox",       "Inbox"),
        ("unread",      "Unread"),
        ("needs_human", "Needs Human"),
        ("ai_drafts",   "AI Drafts"),
        ("scheduled",   "Scheduled"),
        ("sent",        "Sent"),
        ("resolved",    "Resolved"),
        ("refunds",     "Refunds"),
        ("returns",     "Returns"),
        ("dispute",     "Dispute"),
        ("spam",        "Spam"),
        ("archive",     "Archive"),
    )
    store        = models.ForeignKey(
        'stores.Store', on_delete=models.CASCADE,
        related_name='folder_assignments'
    )
    # Exactly one of (folder, label) is set on a given row.
    folder       = models.CharField(max_length=20, choices=FOLDER_CHOICES, blank=True, default="")
    label        = models.ForeignKey(
        EmailLabel, on_delete=models.CASCADE,
        null=True, blank=True, related_name='folder_assignments'
    )
    assigned_to  = models.ForeignKey(
        'teamapp.TeamMember', on_delete=models.CASCADE,
        related_name='folder_assignments'
    )
    is_active    = models.BooleanField(default=True)
    created_by   = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        # One active rule per (store, folder/label) combination
        constraints = [
            models.UniqueConstraint(
                fields=['store', 'folder'],
                condition=models.Q(label__isnull=True) & ~models.Q(folder=""),
                name='uniq_active_folder_assign',
            ),
            models.UniqueConstraint(
                fields=['store', 'label'],
                condition=models.Q(label__isnull=False),
                name='uniq_active_label_assign',
            ),
        ]
        indexes = [
            models.Index(fields=['store', 'is_active']),
            models.Index(fields=['assigned_to', 'is_active']),
        ]

    def __str__(self):
        target = f"label#{self.label_id}" if self.label_id else self.folder
        return f"{target} → {self.assigned_to_id}"
