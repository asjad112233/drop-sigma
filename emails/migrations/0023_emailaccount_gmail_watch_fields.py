from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('emails', '0022_aireplyfeedback'),
    ]

    operations = [
        migrations.AddField(
            model_name='emailaccount',
            name='gmail_watch_history_id',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='emailaccount',
            name='gmail_watch_expiration',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
