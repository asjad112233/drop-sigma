"""Add per-carrier tracking metadata to Order.

Unlocks the per-carrier stage-template renderer in
tracking_public/stage_mapper.py — instead of forcing every shipment
into a single fixed 6-stage flow, the public tracking page now
mirrors the carrier's own progression (e.g. Yuntrack/YunExpress's
5-stage stepper, pre-pended with our own 2 platform stages).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0010_add_tracking_events"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="tracking_carrier",
            field=models.CharField(max_length=40, blank=True, null=True),
        ),
        migrations.AddField(
            model_name="order",
            name="tracking_carrier_stage",
            field=models.CharField(max_length=40, blank=True, null=True),
        ),
    ]
