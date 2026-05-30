from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Store

COPY_FIELDS = [
    'name', 'category', 'status', 'description', 'tags',
    'from_email', 'sender_name', 'reply_to', 'cc_emails', 'bcc_emails',
    'use_default_signature', 'custom_signature', 'subject', 'preheader',
    'body_html', 'footer', 'trigger_type', 'trigger_delay_minutes',
    'working_hours_only', 'throttle_per_day', 'is_category_default', 'is_global',
]


@receiver(post_save, sender=Store)
def seed_templates_on_new_store(sender, instance, created, **kwargs):
    if not created:
        return
    try:
        from emails.models import EmailTemplate
        from emails.default_templates import PORTAL_DEFAULT_TEMPLATES

        if EmailTemplate.objects.filter(store=instance).exists():
            return

        bulk = []
        for tpl in PORTAL_DEFAULT_TEMPLATES:
            data = {f: tpl.get(f) for f in COPY_FIELDS}
            data['store'] = instance
            bulk.append(EmailTemplate(**data))
        EmailTemplate.objects.bulk_create(bulk)
    except Exception:
        pass  # Never let template seeding break store creation


@receiver(post_save, sender=Store)
def seed_default_chat_channels_on_new_store(sender, instance, created, **kwargs):
    """When a tenant creates their first Store, give them their own
    #general / #operations / #support channels (owned by them).

    Idempotent — skips if the tenant already has any non-DM channel.
    Wrapped in try/except so chat seeding never breaks store creation.
    """
    if not created:
        return
    tenant = getattr(instance, "user", None)
    if tenant is None:
        return
    try:
        from teamapp.models import ChatChannel
        from teamapp.services import ensure_tenant_default_channels

        if ChatChannel.objects.filter(owner=tenant, is_dm=False).exists():
            return
        ensure_tenant_default_channels(tenant)
    except Exception:
        pass  # Never let chat seeding break store creation
