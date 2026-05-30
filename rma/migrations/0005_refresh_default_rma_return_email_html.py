"""Refresh the RMASettings.auto_email_body for any tenant who is still on
the original plain-text default. We rewrite ONLY rows that match the old
default verbatim — tenants who have customized their template stay
untouched."""

from django.db import migrations


OLD_BODY = (
    "Hi {{customer_name}},\n\n"
    "Thanks for reaching out — we're sorry to hear about the issue. "
    "To start your return, please open this short form and submit the details:\n\n"
    "{{return_link}}\n\n"
    "You'll get live status updates on the same link and can chat with us directly there.\n\n"
    "— {{store_name}} Support"
)


def forwards(apps, schema_editor):
    RMASettings = apps.get_model("rma", "RMASettings")
    new_default = RMASettings._meta.get_field("auto_email_body").default
    if not callable(new_default):
        # `default` is the string literal at class-load time.
        new_html = new_default
    else:
        new_html = new_default()
    # Update only rows whose body is empty OR matches the OLD plain-text default.
    qs = RMASettings.objects.filter(auto_email_body__in=("", OLD_BODY))
    updated = qs.update(auto_email_body=new_html)
    # Bonus: also catch rows that have customized whitespace but otherwise
    # match — defensive trim-compare.
    if updated == 0:
        for row in RMASettings.objects.all():
            if (row.auto_email_body or "").strip() == OLD_BODY.strip():
                row.auto_email_body = new_html
                row.save(update_fields=["auto_email_body"])


def backwards(apps, schema_editor):
    # Reversal would clobber tenant customizations — intentionally a no-op.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("rma", "0004_alter_rmasettings_auto_email_body"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
