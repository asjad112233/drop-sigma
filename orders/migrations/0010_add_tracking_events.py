"""Add real-carrier tracking event history to Order.

Backs the rewrite of tracking_public/stage_mapper.py so that
track.dropsigma.com renders the *actual* carrier journey (events +
real timestamps, sanitised) rather than the synthesised 6-stage
progression. The synthesised version is kept as a fallback for the
brief window after order creation, before the first carrier-event
pull lands.

Fields:
  - tracking_events            JSON list of {ts, raw, stage}
  - tracking_events_updated_at last successful poll
  - tracking_events_attempts   poll attempt counter (backoff for
                               un-poll-able numbers — e.g. a carrier
                               that never returns anything useful)
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0009_shopifygdprrequest"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="tracking_events",
            field=models.JSONField(blank=True, null=True, default=list),
        ),
        migrations.AddField(
            model_name="order",
            name="tracking_events_updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="order",
            name="tracking_events_attempts",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
