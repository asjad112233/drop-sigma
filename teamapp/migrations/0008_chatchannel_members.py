from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("teamapp", "0007_chatreadreceipt"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="chatchannel",
            name="members",
            field=models.ManyToManyField(
                blank=True,
                related_name="channel_memberships",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
