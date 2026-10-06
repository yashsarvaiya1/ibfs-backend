from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from shared.models import Contact, PaymentAccount
from accounting.services import process_document_create, _create_ftxn


class LedgerTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user('ledger'))
        self.contact = Contact.objects.create(contact_name='Customer', phone='123', opening_balance=50)
        self.account = PaymentAccount.objects.create(name='Cash', type='cash', current_balance=100)

    def test_running_balance_keeps_previous_pages_and_hidden_records(self):
        process_document_create('invoice', {'total_amount':100,'date':'2026-01-01'}, self.contact)
        _create_ftxn('actual', 40, self.contact, self.account, None, '2026-01-02')
        _create_ftxn('actual', 20, self.contact, self.account, None, '2026-01-03')
        response = self.client.get(f'/api/contacts/{self.contact.pk}/ledger/?type=actual&page=2&page_size=1')
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(float(response.data['results'][0]['running_cf']),10)
        detail = self.client.get(f'/api/contacts/{self.contact.pk}/')
        self.assertEqual(float(detail.data['current_cf']),10)

    def test_balance_reconciliation_leaves_history(self):
        response = self.client.post(f'/api/accounts/{self.account.pk}/set_balance/',{'current_balance':'125'},format='json')
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(float(response.data['current_balance']),125)
        row = self.account.transactions.get()
        self.assertEqual(row.amount,25)
        self.assertEqual(row.notes,'Balance reconciliation')

    def test_account_summary_is_not_page_subtotal(self):
        PaymentAccount.objects.create(name='Bank',type='bank',current_balance=200)
        response = self.client.get('/api/accounts/summary/?is_active=true&page_size=1')
        self.assertEqual(float(response.data['total_balance']),300)
