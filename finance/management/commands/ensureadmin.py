"""Ensure a Django superuser exists, provisioned from configuration.

Reads the optional ``django.admin`` block of the service configuration
(``config.yaml`` + env) and creates that superuser on first boot if it does not
already exist. Idempotent: re-running never duplicates or overwrites the user.

    python manage.py ensureadmin

Does nothing (and exits 0) when no ``django.admin`` block is configured.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from bank_server.configuration import Settings


class Command(BaseCommand):
    help = "Create the configured superuser (django.admin) if it does not already exist."
    requires_migrations_checks = True

    def handle(self, *args, **options) -> None:
        admin = Settings().django.admin
        if admin is None:
            self.stdout.write("No django.admin configured — skipping superuser provisioning.")
            return

        User = get_user_model()
        if User.objects.filter(username=admin.username).exists():
            self.stdout.write(f"Superuser '{admin.username}' already exists — nothing to do.")
            return

        User.objects.create_superuser(
            username=admin.username,
            email=admin.email or "",
            password=admin.password,
        )
        self.stdout.write(self.style.SUCCESS(f"Created superuser '{admin.username}'."))
