"""Provision the first administrator without changing existing accounts."""
import os
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from shared.models import Settings


class Command(BaseCommand):
    help = 'Create an initial superuser from environment fields only when there are no users.'

    def handle(self, *args, **options):
        user_model = get_user_model()
        with transaction.atomic():
            # Serialize concurrent container starts using the existing singleton.
            settings = Settings.get()
            Settings.objects.select_for_update().get(pk=settings.pk)
            if user_model.objects.exists():
                self.stdout.write('Existing users retained; initial administrator setup skipped.')
                return
            username = os.getenv('DJANGO_SUPERUSER_USERNAME', '').strip()
            password = os.getenv('DJANGO_SUPERUSER_PASSWORD', '')
            email = os.getenv('DJANGO_SUPERUSER_EMAIL', '').strip()
            if not any((username, password, email)):
                self.stdout.write('Initial administrator fields are unset; use createsuperuser for manual setup.')
                return
            if not username or not password:
                raise CommandError('First-start setup requires DJANGO_SUPERUSER_USERNAME and DJANGO_SUPERUSER_PASSWORD; email is optional.')
            user = user_model(username=username, email=email)
            try:
                user.full_clean(exclude=['password'])
                validate_password(password, user=user)
            except ValidationError as exc:
                raise CommandError('Initial administrator validation failed: ' + '; '.join(exc.messages)) from exc
            user_model.objects.create_superuser(username=username, email=email, password=password)
            self.stdout.write(self.style.SUCCESS('Initial administrator created.'))
