"""Backfill the new "Delivered" and "Returned" template categories for
every store that already has the original seed templates installed.

The post_save signal in stores/signals.py only fires on NEW store creation,
so existing tenants never receive the new categories without this step.

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


def forwards(apps, schema_editor):
    EmailTemplate = apps.get_model("emails", "EmailTemplate")
    Store = apps.get_model("stores", "Store")

    # Import the seed list lazily; if it ever fails to import, abort cleanly.
    try:
        from emails.default_templates import PORTAL_DEFAULT_TEMPLATES
    except Exception:
        return

    new_seeds = [t for t in PORTAL_DEFAULT_TEMPLATES if t.get("category") in ("delivered", "returned")]
    if not new_seeds:
        return

    # Only stores that already have at least one seeded template — leaves
    # stores that intentionally have no templates alone.
    seeded_store_ids = (
        EmailTemplate.objects.exclude(store__isnull=True)
        .values_list("store_id", flat=True).distinct()
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
        ("emails", "0031_alter_emailtemplate_category_and_more"),
        ("stores", "__latest__"),  # ensure Store model is migrated to its current shape
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
