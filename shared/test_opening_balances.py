from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from .models import Contact, PaymentAccount, Settings
from accounting.services import process_document_create, process_send_receive, process_transfer
from accounting.models import FinancialTransaction, PaymentAllocation
from accounting.payments import payment_status


class OpeningBalanceTests(TestCase):
    def setUp(self):
        self.client = APIClient(); self.client.force_authenticate(get_user_model().objects.create_user('opening-review'))
        settings = Settings.get(); settings.auto_transaction = False; settings.save()
        self.contact = Contact.objects.create(contact_name='Customer', phone='123', opening_balance=-50)
        self.account = PaymentAccount.objects.create(name='Bank', type='bank', current_balance=1000)
        self.invoice = process_document_create('invoice', {'date': '2026-10-01', 'total_amount': 100}, self.contact)
        process_send_receive(self.contact, {'amount': 80, 'payment_account': self.account.pk, 'document': self.invoice.pk, 'date': '2026-10-02'}, 'receive')
        process_document_create('expense', {'date': '2026-10-03', 'total_amount': 30, 'payment_account': self.account.pk}, self.contact)
        other = PaymentAccount.objects.create(name='Cash', type='cash')
        process_transfer({'from_account': self.account.pk, 'to_account': other.pk, 'amount': 10, 'date': '2026-10-04'})

    def test_account_opening_edit_rebases_every_page_without_cash_transactions_or_settlement_changes(self):
        history = list(FinancialTransaction.objects.values_list('pk', 'amount', 'date')); allocations = list(PaymentAllocation.objects.values_list('pk', 'amount'))
        for opening in ('2000.00', '0.00', '-100.00'):
            result = self.client.patch(f'/api/accounts/{self.account.pk}/', {'opening_balance': opening}, format='json')
            self.assertEqual(result.status_code, 200, result.data)
            self.assertEqual(result.data['opening_balance'], opening)
            self.assertEqual(Decimal(result.data['current_balance']), Decimal(opening) + 40)
            ledger = self.client.get(f'/api/accounts/{self.account.pk}/transactions/', {'view': 'ledger', 'page_size': 1, 'page': 2})
            self.assertEqual(Decimal(ledger.data['balance_before_period']), Decimal(opening))
            self.assertEqual(Decimal(ledger.data['results'][0]['running_balance']), Decimal(opening) + 50)
            filtered = self.client.get(f'/api/accounts/{self.account.pk}/transactions/', {'view': 'ledger', 'date_from': '2026-10-04'})
            self.assertEqual(Decimal(filtered.data['balance_before_period']), Decimal(opening) + 50)
            self.assertEqual(Decimal(filtered.data['results'][0]['running_balance']), Decimal(opening) + 40)
        self.assertEqual(list(FinancialTransaction.objects.values_list('pk', 'amount', 'date')), history)
        self.assertEqual(list(PaymentAllocation.objects.values_list('pk', 'amount')), allocations)
        self.assertEqual(Decimal(payment_status(self.invoice)['remaining']), 20)
        self.assertEqual(self.client.get('/api/accounts/').data['results'][0]['opening_balance'], '-100.00')

    def test_contact_opening_edit_rebases_standing_without_reassigning_settled_documents(self):
        status = payment_status(self.invoice); self.account.refresh_from_db(); cash = self.account.current_balance
        result = self.client.patch(f'/api/contacts/{self.contact.pk}/', {'opening_balance': '40'}, format='json')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data['current_cf'], '20.00')
        ledger = self.client.get(f'/api/contacts/{self.contact.pk}/ledger/', {'page_size': 1, 'page': 2})
        self.assertEqual(ledger.data['results'][0]['running_cf'], '20.00')
        self.assertEqual(payment_status(self.invoice), status)
        self.account.refresh_from_db(); self.assertEqual(self.account.current_balance, cash)

    def test_create_accepts_opening_balance_and_reconciliation_stays_separate(self):
        result = self.client.post('/api/accounts/', {'name': 'New bank', 'type': 'bank', 'opening_balance': '500'}, format='json')
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(result.data['current_balance'], '500.00'); self.assertEqual(result.data['opening_balance'], '500.00')
        result = self.client.patch(f'/api/accounts/{self.account.pk}/', {'current_balance': '1200'}, format='json')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data['opening_balance'], '1000.00')
        ambiguous = self.client.patch(f'/api/accounts/{self.account.pk}/', {'opening_balance': '50', 'current_balance': '60'}, format='json')
        self.assertEqual(ambiguous.status_code, 400)
