"""Backfill the three new template style families — Minimal Mono,
Vibrant Gradient, and Editorial Boutique — for every store that already
has the original seed templates installed.

The post_save signal in stores/signals.py only fires on NEW store creation,
so existing tenants never receive new styles without this step.

Idempotent: for each store, skips any template whose name already exists
under that store. Safe to re-apply."""
from django.db import migrations


COPY_FIELDS = [
    "name", "category", "status", "description", "tags",
    "from_email", "sender_name", "reply_to", "cc_emails", "bcc_emails",
    "use_default_signature", "custom_signature", "subject", "preheader",
    "body_html", "footer", "trigger_type", "trigger_delay_minutes",
    "working_hours_only", "throttle_per_day", "is_category_default", "is_global",
]

NEW_STYLE_MARKERS = ("Minimal Mono", "Vibrant Gradient", "Editorial Boutique")


def forwards(apps, schema_editor):
    EmailTemplate = apps.get_model("emails", "EmailTemplate")

    try:
        from emails.default_templates import PORTAL_DEFAULT_TEMPLATES
    except Exception:
        return

    new_seeds = [
        t for t in PORTAL_DEFAULT_TEMPLATES
        if any(marker in t.get("name", "") for marker in NEW_STYLE_MARKERS)
    ]
    if not new_seeds:
        return

    # Only stores that already have at least one seeded template — leaves
    # stores that intentionally have no templates alone. Use set() to be
    # robust against any quirks in distinct() with default ordering.
    seeded_store_ids = set(
        EmailTemplate.objects.exclude(store__isnull=True)
        .values_list("store_id", flat=True)
    )

    to_create = []
    for store_id in seeded_store_ids:
        existing_names = set(
            EmailTemplate.objects.filter(store_id=store_id).values_list("name", flat=True)
        )
        for tpl in new_seeds:
            if tpl.get("name") in existing_names:
                continue
            data = {f: tpl.get(f) for f in COPY_FIELDS}
            data["store_id"] = store_id
            to_create.append(EmailTemplate(**data))

    if to_create:
        EmailTemplate.objects.bulk_create(to_create, batch_size=200)


def backwards(apps, schema_editor):
    # Reversal would delete user data — intentionally a no-op.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("emails", "0032_backfill_delivered_returned_templates"),
        ("stores", "__latest__"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
