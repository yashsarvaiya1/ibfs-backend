import os
from io import StringIO
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import TestCase

FIELDS = {'DJANGO_SUPERUSER_USERNAME': '', 'DJANGO_SUPERUSER_PASSWORD': '', 'DJANGO_SUPERUSER_EMAIL': ''}
VALID = {**FIELDS, 'DJANGO_SUPERUSER_USERNAME': 'first-owner', 'DJANGO_SUPERUSER_PASSWORD': 'Unique-Initial-Cedar-72!', 'DJANGO_SUPERUSER_EMAIL': 'owner@example.com'}


class BootstrapAdminTests(TestCase):
    def run_bootstrap(self, fields):
        output = StringIO()
        with patch.dict(os.environ, fields):
            call_command('bootstrap_admin', stdout=output)
        self.assertNotIn(fields['DJANGO_SUPERUSER_PASSWORD'], output.getvalue()) if fields['DJANGO_SUPERUSER_PASSWORD'] else None
        return output.getvalue()

    def test_first_start_creates_hashed_active_superuser(self):
        self.run_bootstrap(VALID)
        user = get_user_model().objects.get(username='first-owner')
        self.assertTrue(user.is_superuser and user.is_staff and user.is_active)
        self.assertTrue(user.check_password(VALID['DJANGO_SUPERUSER_PASSWORD']))
        self.assertNotEqual(user.password, VALID['DJANGO_SUPERUSER_PASSWORD'])
        self.assertEqual(user.email, VALID['DJANGO_SUPERUSER_EMAIL'])

    def test_restart_and_changed_environment_never_reset_existing_account(self):
        self.run_bootstrap(VALID)
        password_hash = get_user_model().objects.get().password
        self.run_bootstrap({**VALID, 'DJANGO_SUPERUSER_USERNAME': 'another-owner', 'DJANGO_SUPERUSER_PASSWORD': 'New-Initial-Password-85!'})
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertEqual(get_user_model().objects.get().password, password_hash)

    def test_existing_regular_user_is_not_promoted_or_replaced(self):
        user = get_user_model().objects.create_user('existing', password='Existing-Password-92!')
        self.run_bootstrap(VALID)
        user.refresh_from_db()
        self.assertFalse(user.is_superuser)
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_unset_fields_leave_manual_setup_available_and_email_is_optional(self):
        self.run_bootstrap(FIELDS)
        self.assertFalse(get_user_model().objects.exists())
        self.run_bootstrap({**VALID, 'DJANGO_SUPERUSER_EMAIL': ''})
        self.assertTrue(get_user_model().objects.get().is_superuser)

    def test_incomplete_or_invalid_credentials_fail_without_creating_account(self):
        for fields in ({**FIELDS, 'DJANGO_SUPERUSER_USERNAME': 'owner'}, {**VALID, 'DJANGO_SUPERUSER_PASSWORD': '123'}, {**VALID, 'DJANGO_SUPERUSER_EMAIL': 'invalid'}):
            with self.assertRaises(CommandError):
                self.run_bootstrap(fields)
            self.assertFalse(get_user_model().objects.exists())
