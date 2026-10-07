from datetime import date
from decimal import Decimal
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import TestCase
from rest_framework.test import APIClient
from shared.models import Contact, PaymentAccount, Settings
from inventory.models import Product, StockTransaction
from .models import Document, FinancialTransaction, PaymentAllocation
from .services import process_document_create, _create_ftxn
from .payments import payment_status
from .financial_year import financial_year_report
from .reports import gst_report
from .report_exports import allocation_review


class IncomeTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user('income-review'))
        self.contact = Contact.objects.create(contact_name='Employer', phone='123', opening_balance=-100)
        self.account = PaymentAccount.objects.create(name='Bank', type='bank', current_balance=1000)
        self.other = PaymentAccount.objects.create(name='Cash', type='cash', current_balance=50)
        self.payload = {'type': 'income', 'date': '2026-10-01', 'contact': self.contact.pk,
                        'payment_account': self.account.pk, 'line_items': [{'name': 'Salary', 'amount': 200}]}

    def create(self, **changes):
        response = self.client.post('/api/documents/', {**self.payload, **changes}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        return Document.objects.get(pk=response.data['id'])

    def test_income_posts_once_without_settlement_stock_or_contact_balance(self):
        for automatic in (True, False):
            with self.subTest(automatic=automatic):
                settings = Settings.get(); settings.auto_transaction = automatic; settings.enable_challan = automatic; settings.enable_vouchers = True; settings.save()
                product = Product.objects.create(name='Unused inventory', rate=1, current_stock=10)
                doc = self.create(line_items=[{'name': 'Salary', 'amount': 200, 'product_id': product.pk, 'quantity': 1}])
                self.assertTrue(doc.doc_id.startswith('INC-'))
                self.assertEqual(doc.stock_mode, 'none')
                txn = doc.transactions.get()
                self.assertEqual((txn.type, txn.amount, txn.monthly_cumulative_delta), ('actual', Decimal('200'), Decimal('0')))
                self.assertFalse(PaymentAllocation.objects.filter(payment=txn).exists())
                self.assertFalse(StockTransaction.objects.filter(document=doc).exists())
                product.refresh_from_db(); self.assertEqual(product.current_stock, 10)
                self.assertEqual(Decimal(self.client.get(f'/api/contacts/{self.contact.pk}/').data['current_cf']), -100)
                detail = self.client.get(f'/api/documents/{doc.pk}/').data
                self.assertIsNone(detail['payment_status']); self.assertIsNone(detail['stock_status'])
        self.account.refresh_from_db(); self.assertEqual(self.account.current_balance, 1400)

    def test_optional_contact_and_income_filters(self):
        without_contact = self.create(contact=None)
        with_contact = self.create()
        ordinary_contact = Contact.objects.create(contact_name='Customer', phone='789')
        invoice = process_document_create('invoice', {'date': '2026-10-01', 'total_amount': 100}, ordinary_contact)
        payment = _create_ftxn('actual', Decimal('25'), ordinary_contact, self.other, invoice, date(2026, 10, 2))
        expense = self.create(type='expense', contact=None, payment_account=self.other.pk,
                              line_items=[{'name': 'Office costs', 'amount': 10}])
        income_ids = {without_contact.transactions.get().pk, with_contact.transactions.get().pk}
        def result_ids(url, params):
            response = self.client.get(url, params)
            self.assertEqual(response.status_code, 200, response.data)
            ids = {row['id'] for row in response.data['results']}
            self.assertEqual(response.data['count'], len(ids))
            return ids
        self.assertEqual(result_ids('/api/transactions/', {'types': 'income'}), income_ids)
        self.assertEqual(result_ids('/api/transactions/', {'types': 'actual'}), {payment.pk})
        self.assertEqual(result_ids('/api/transactions/', {'types': 'income,actual'}), income_ids | {payment.pk})
        self.assertEqual(result_ids('/api/transactions/', {'types': 'expense'}), {expense.transactions.get().pk})
        self.assertEqual(result_ids('/api/documents/', {'type': 'income'}), {without_contact.pk, with_contact.pk})
        self.assertEqual(result_ids('/api/documents/', {'type': 'invoice'}), {invoice.pk})
        self.assertEqual(self.client.get(f'/api/contacts/{self.contact.pk}/ledger/').data['results'][0]['running_cf'], '-100.00')
        self.assertEqual(self.client.get(f'/api/accounts/{self.account.pk}/transactions/', {'view': 'ledger'}).data['results'][-1]['running_balance'], '1400.00')
        self.assertEqual(with_contact.contact_id, self.contact.pk)

    def test_existing_payment_cannot_be_relinked_to_income_by_put_or_patch(self):
        income = self.create()
        invoice = process_document_create('invoice', {'date': '2026-10-01', 'total_amount': 100}, self.contact)
        payment = _create_ftxn('actual', Decimal('25'), self.contact, self.account, invoice, date(2026, 10, 2))
        allocation = payment.allocations.get()
        for method in (self.client.patch, self.client.put):
            response = method(f'/api/transactions/{payment.pk}/', {
                'document': income.pk, 'amount': 999, 'date': '2026-11-01',
                'payment_account': self.other.pk, 'notes': 'Should not be saved',
            }, format='json')
            self.assertEqual(response.status_code, 400, response.data)
            self.assertIn('document', response.data)
            payment.refresh_from_db(); allocation.refresh_from_db()
            self.assertEqual((payment.document_id, payment.amount, payment.payment_account_id, payment.date),
                             (invoice.pk, Decimal('25'), self.account.pk, date(2026, 10, 2)))
            self.assertEqual(allocation.amount, Decimal('25'))
            self.assertEqual(income.transactions.count(), 1)
            self.account.refresh_from_db(); self.other.refresh_from_db()
            self.assertEqual((self.account.current_balance, self.other.current_balance), (1225, 50))
        response = self.client.delete(f'/api/documents/{income.pk}/', {'strategy': 'revert'}, format='json')
        self.assertEqual(response.status_code, 204)
        self.assertTrue(FinancialTransaction.objects.filter(pk=payment.pk).exists())
        self.assertTrue(PaymentAllocation.objects.filter(pk=allocation.pk).exists())
        self.account.refresh_from_db(); self.assertEqual(self.account.current_balance, 1025)

    def test_edits_move_receipt_between_accounts_and_contacts_without_duplicate_posting(self):
        doc = self.create()
        other_contact = Contact.objects.create(contact_name='Income source', phone='456', opening_balance=30)
        response = self.client.patch(f'/api/documents/{doc.pk}/', {'line_items': [{'name': 'Bonus', 'amount': 300}],
            'payment_account': self.other.pk, 'contact': other_contact.pk, 'date': '2026-11-01'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.account.refresh_from_db(); self.other.refresh_from_db(); doc.refresh_from_db()
        self.assertEqual(self.account.current_balance, 1000); self.assertEqual(self.other.current_balance, 350)
        txn = doc.transactions.get(); self.assertEqual(txn.amount, 300)
        self.assertEqual(txn.contact_id, other_contact.pk); self.assertEqual(str(txn.date), '2026-11-01')
        self.assertEqual(txn.monthly_cumulative_delta, 0)
        for contact, expected in ((self.contact, -100), (other_contact, 30)):
            self.assertEqual(Decimal(self.client.get(f'/api/contacts/{contact.pk}/').data['current_cf']), expected)
        self.assertFalse(PaymentAllocation.objects.exists())

    def test_income_is_blocked_from_every_settlement_and_direct_mutation_path(self):
        doc = self.create(); txn = doc.transactions.get()
        invoice = process_document_create('invoice', {'date': '2026-10-01', 'total_amount': 100}, self.contact)
        urls = [(f'/api/documents/{doc.pk}/record_payment/', {'amount': 20, 'payment_account': self.account.pk}),
                (f'/api/documents/{doc.pk}/mark_paid/', {'is_paid': True}),
                (f'/api/transactions/{txn.pk}/allocate/', {'allocations': [{'document': invoice.pk, 'amount': 20}]}),
                (f'/api/transactions/{txn.pk}/link_document/', {'document': invoice.pk}),
                (f'/api/contacts/{self.contact.pk}/receive/', {'amount': 20, 'document': doc.pk}),
                ('/api/transactions/', {'type': 'actual', 'document': doc.pk, 'date': '2026-10-02', 'amount': 20})]
        for url, data in urls:
            self.assertEqual(self.client.post(url, data, format='json').status_code, 400, url)
        self.assertEqual(self.client.patch(f'/api/transactions/{txn.pk}/', {'amount': 400}, format='json').status_code, 400)
        self.assertEqual(self.client.delete(f'/api/transactions/{txn.pk}/').status_code, 400)
        self.assertFalse(doc.payment_allocations.exists())
        self.account.refresh_from_db(); self.assertEqual(self.account.current_balance, 1200)

    def test_income_validation_is_atomic(self):
        for change in ({'payment_account': None}, {'line_items': [], 'total_amount': 0},
                       {'taxes': [{'name': 'IGST', 'percentage': 18}]}, {'discount': 10},
                       {'reference': process_document_create('invoice', {'date': '2026-10-01', 'total_amount': 100}, self.contact).pk}):
            response = self.client.post('/api/documents/', {**self.payload, **change}, format='json')
            self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(Document.objects.filter(type='income').exists())
        self.account.refresh_from_db(); self.assertEqual(self.account.current_balance, 1000)

    def test_delete_can_reverse_or_keep_cash_but_never_changes_contact_standing(self):
        for strategy, expected in (('revert', 1000), ('manual', 1200)):
            with self.subTest(strategy=strategy):
                doc = self.create()
                response = self.client.delete(f'/api/documents/{doc.pk}/', {'strategy': strategy}, format='json')
                self.assertEqual(response.status_code, 204)
                self.account.refresh_from_db(); self.assertEqual(self.account.current_balance, expected)
                self.assertEqual(Decimal(self.client.get(f'/api/contacts/{self.contact.pk}/').data['current_cf']), -100)
                self.assertEqual(self.client.delete(f'/api/documents/{doc.pk}/', {'strategy': strategy}, format='json').status_code, 404)

    def test_income_is_other_income_in_fy_and_excluded_from_gst_and_payment_review(self):
        self.create()
        report = financial_year_report(2026)
        self.assertEqual(report['totals']['other_income'], '200.00')
        self.assertEqual(report['totals']['cash_received'], '200.00')
        self.assertEqual(report['totals']['net_sales'], '0.00')
        self.assertEqual(gst_report(date(2026, 10, 1), date(2026, 10, 31))['count'], 0)
        self.assertEqual(allocation_review(date(2026, 10, 1), date(2026, 10, 31), 1, 20)['count'], 0)

    def test_income_print_does_not_affect_party_debit_credit_but_is_cash_debit(self):
        self.create()
        for params, debit in (({'contact': self.contact.pk}, False), ({'account': self.account.pk}, True)):
            with patch('accounting.services.render_to_string', wraps=render_to_string) as render, patch('accounting.services._render_playwright_pdf', return_value=b'%PDF-test'):
                response = self.client.get('/api/transactions/print/', {**params, 'view': 'ledger'})
            self.assertEqual(response.status_code, 200)
            context = render.call_args.args[1]; row = context['transactions'][0]
            self.assertTrue(row['is_income']); self.assertEqual(row['debit'], debit); self.assertFalse(row['credit'])
            self.assertEqual(context['closing_balance'], '1200.00' if debit else '100.00')
