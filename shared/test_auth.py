import tempfile
from pathlib import Path
from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APIClient


class SessionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user=get_user_model().objects.create_user('session-user',password='local-test-pass')
        self.client=APIClient(enforce_csrf_checks=True)

    def login(self):
        token=self.client.get('/api/session/status/').data['csrf_token']
        return self.client.post('/api/session/login/',{'username':'session-user','password':'local-test-pass'},format='json',HTTP_X_CSRFTOKEN=token)

    def test_login_requires_csrf_and_cookie_authenticates_api(self):
        response=self.client.post('/api/session/login/',{'username':'session-user','password':'local-test-pass'},format='json')
        self.assertEqual(response.status_code,403)
        response=self.login()
        self.assertEqual(response.status_code,200,response.data)
        self.assertTrue(self.client.cookies['sessionid']['httponly'])
        self.assertEqual(self.client.get('/api/contacts/').status_code,200)
        response=self.client.post('/api/session/logout/',{},format='json',HTTP_X_CSRFTOKEN=response.data['csrf_token'])
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.client.get('/api/contacts/').status_code,401)

    def test_uploads_are_private_and_not_cacheable(self):
        with tempfile.TemporaryDirectory() as root,override_settings(MEDIA_ROOT=root):
            Path(root,'document.txt').write_text('private test')
            self.assertEqual(self.client.get('/media/document.txt').status_code,401)
            self.login()
            response=self.client.get('/media/document.txt')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response['Cache-Control'],'private, no-store')
            response.close()

    def test_basic_auth_remains_available_for_api_clients(self):
        import base64
        self.client.credentials(HTTP_AUTHORIZATION='Basic '+base64.b64encode(b'session-user:local-test-pass').decode())
        self.assertEqual(self.client.get('/api/contacts/').status_code,200)
