from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
import os


class Command(BaseCommand):
    help = "Create superuser if none exists, always sync password from env"

    def handle(self, *args, **kwargs):
        username = os.getenv("DJANGO_SUPERUSER_USERNAME", "admin")
        email = os.getenv("DJANGO_SUPERUSER_EMAIL", "admin@dropsigma.com")
        password = os.getenv("DJANGO_SUPERUSER_PASSWORD", "admin1234")

        user, created = User.objects.get_or_create(username=username)
        user.email = email
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()

        if created:
            self.stdout.write(f"Superuser '{username}' created.")
        else:
            self.stdout.write(f"Superuser '{username}' updated.")
