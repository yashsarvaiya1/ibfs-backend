"""Synchronize the explicitly configured administrator at container startup."""
import os
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from shared.models import Settings


class Command(BaseCommand):
    help = 'Create or synchronize the administrator named in DJANGO_SUPERUSER_* environment fields.'

    def handle(self, *args, **options):
        username = os.getenv('DJANGO_SUPERUSER_USERNAME', '').strip()
        password = os.getenv('DJANGO_SUPERUSER_PASSWORD', '')
        email = os.getenv('DJANGO_SUPERUSER_EMAIL', '').strip()
        if not any((username, password, email)):
            self.stdout.write('Administrator environment fields are unset; existing users retained.')
            return
        if not username or not password:
            raise CommandError('Administrator setup requires DJANGO_SUPERUSER_USERNAME and DJANGO_SUPERUSER_PASSWORD; email is optional.')
        user_model = get_user_model()
        with transaction.atomic():
            # Serialize simultaneous container starts using the existing singleton.
            settings = Settings.get()
            Settings.objects.select_for_update().get(pk=settings.pk)
            user = user_model.objects.select_for_update().filter(username=username).first()
            creating = user is None
            if creating:
                user = user_model(username=username)
            changed = []
            for field in ('is_active', 'is_staff', 'is_superuser'):
                if not getattr(user, field):
                    setattr(user, field, True)
                    changed.append(field)
            if email and user.email != email:
                user.email = email
                changed.append('email')
            change_password = creating or not user.check_password(password)
            try:
                user.full_clean(exclude=['password'])
                if change_password:
                    validate_password(password, user=user)
            except ValidationError as exc:
                raise CommandError('Administrator environment validation failed: ' + '; '.join(exc.messages)) from exc
            if change_password:
                user.set_password(password)
                changed.append('password')
            if creating:
                user.save()
                self.stdout.write(self.style.SUCCESS('Configured administrator created.'))
            elif changed:
                user.save(update_fields=changed)
                self.stdout.write(self.style.SUCCESS('Configured administrator synchronized from environment.'))
            else:
                self.stdout.write('Configured administrator already matches environment; no changes needed.')
