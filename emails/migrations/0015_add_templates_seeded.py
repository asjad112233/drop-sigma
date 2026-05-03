from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('emails', '0014_add_auto_email_enabled'),
    ]

    operations = [
        migrations.AddField(
            model_name='emailaccount',
            name='templates_seeded',
            field=models.BooleanField(default=False),
        ),
    ]
