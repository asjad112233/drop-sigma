from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
import os


class Command(BaseCommand):
    help = "Create superuser if none exists"

    def handle(self, *args, **kwargs):
        if User.objects.filter(is_superuser=True).exists():
            self.stdout.write("Superuser already exists.")
            return

        username = os.getenv("DJANGO_SUPERUSER_USERNAME", "admin")
        email = os.getenv("DJANGO_SUPERUSER_EMAIL", "admin@baghawat.com")
        password = os.getenv("DJANGO_SUPERUSER_PASSWORD", "admin1234")

        User.objects.create_superuser(username=username, email=email, password=password)
        self.stdout.write(f"Superuser '{username}' created.")
