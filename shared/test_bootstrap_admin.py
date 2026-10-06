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

    def test_repeat_start_keeps_hash_and_changed_environment_updates_target_only(self):
        self.run_bootstrap(VALID)
        user = get_user_model().objects.get(username='first-owner')
        password_hash = user.password
        self.run_bootstrap(VALID)
        user.refresh_from_db()
        self.assertEqual(user.password, password_hash)
        other = get_user_model().objects.create_user('other', password='Other-Account-Password-88!')
        other_hash = other.password
        new_password = 'New-Initial-Password-85!'
        self.run_bootstrap({**VALID, 'DJANGO_SUPERUSER_PASSWORD': new_password})
        user.refresh_from_db(); other.refresh_from_db()
        self.assertTrue(user.check_password(new_password))
        self.assertFalse(user.check_password(VALID['DJANGO_SUPERUSER_PASSWORD']))
        self.assertEqual(other.password, other_hash)
        self.assertFalse(other.is_superuser)

    def test_target_created_when_other_users_exist_and_target_reactivated(self):
        other = get_user_model().objects.create_user('existing', password='Existing-Password-92!')
        self.run_bootstrap(VALID)
        self.assertEqual(get_user_model().objects.count(), 2)
        other.refresh_from_db()
        self.assertFalse(other.is_superuser)
        user = get_user_model().objects.get(username='first-owner')
        user.is_active = user.is_staff = user.is_superuser = False
        user.save()
        self.run_bootstrap(VALID)
        user.refresh_from_db()
        self.assertTrue(user.is_active and user.is_staff and user.is_superuser)

    def test_synchronized_env_password_signs_into_app_with_csrf(self):
        from rest_framework.test import APIClient
        from django.core.cache import cache
        cache.clear()
        get_user_model().objects.create_superuser('first-owner', password='Old-Login-Password-94!')
        self.run_bootstrap(VALID)
        client = APIClient(enforce_csrf_checks=True)
        token = client.get('/api/session/status/').data['csrf_token']
        response = client.post('/api/session/login/', {'username': VALID['DJANGO_SUPERUSER_USERNAME'], 'password': VALID['DJANGO_SUPERUSER_PASSWORD']}, format='json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(client.get('/api/contacts/').status_code, 200)

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
