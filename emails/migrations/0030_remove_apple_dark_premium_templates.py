"""Permanently remove the "Apple Style" and "Dark Premium" email templates
from every category. These two presets were retired by product decision;
their entries were dropped from default_templates.py at the same time, so
no future re-seed will recreate them.

Reverse is a no-op — we don't restore the dropped rows."""
from django.db import migrations
from django.db.models import Q


def forwards(apps, schema_editor):
    EmailTemplate = apps.get_model("emails", "EmailTemplate")
    EmailTemplate.objects.filter(
        Q(name__icontains="Apple Style") | Q(name__icontains="Dark Premium")
    ).delete()


def backwards(apps, schema_editor):
    # Intentionally a no-op — these templates were retired permanently.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("emails", "0029_emailthreadactivity"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
