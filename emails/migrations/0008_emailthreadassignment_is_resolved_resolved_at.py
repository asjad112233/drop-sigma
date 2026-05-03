from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('emails', '0007_emailthreadassignment'),
    ]

    operations = [
        migrations.AddField(
            model_name='emailthreadassignment',
            name='is_resolved',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='emailthreadassignment',
            name='resolved_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
