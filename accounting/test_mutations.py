from datetime import date
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient
from shared.models import Settings, Contact, PaymentAccount
from inventory.models import Product, StockTransaction
from .models import Document
from .services import process_document_create, process_document_delete, _create_ftxn, _create_stxn, compute_opening_balance_for_print


class AccountingMutationTests(TestCase):
    def setUp(self):
        self.contact = Contact.objects.create(contact_name='Customer', phone='123')
        self.account = PaymentAccount.objects.create(type='bank', name='Bank', current_balance=1000)
        self.product = Product.objects.create(name='Product', rate=10)
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user('tester'))
        Settings.get()

    def test_credit_and_debit_notes_settle_in_the_correct_direction(self):
        for kind, sign in [('cn', -1), ('dn', 1)]:
            doc = process_document_create(kind, {'date':'2026-01-01', 'total_amount':80}, self.contact)
            response = self.client.post(f'/api/documents/{doc.pk}/record_payment/',
                {'amount':80, 'payment_account':self.account.pk, 'date':'2026-01-02'}, format='json')
            self.assertEqual(response.status_code, 201)
            self.assertEqual(doc.transactions.get(type='actual').amount, Decimal(sign * 80))

    def test_reverse_multiple_movements_restores_each_balance(self):
        doc = process_document_create('bill', {'date':'2026-01-01', 'total_amount':100}, self.contact)
        _create_ftxn('actual', -30, self.contact, self.account, doc, '2026-01-02')
        _create_ftxn('actual', -20, self.contact, self.account, doc, '2026-01-03')
        _create_stxn('record', 5, self.product, doc, '2026-01-01')
        _create_stxn('actual', 2, self.product, doc, '2026-01-02')
        _create_stxn('actual', 3, self.product, doc, '2026-01-03')
        process_document_delete(doc, 'revert')
        self.account.refresh_from_db(); self.product.refresh_from_db()
        self.assertEqual(self.account.current_balance, 1000)
        self.assertEqual(self.product.current_stock, 0)
        self.assertFalse(StockTransaction.objects.filter(document=doc).exists())

    def test_edit_amount_and_account_together(self):
        txn = _create_ftxn('actual', 50, self.contact, self.account, date='2026-01-01')
        other = PaymentAccount.objects.create(type='cash', name='Cash', current_balance=200)
        response = self.client.patch(f'/api/transactions/{txn.pk}/',
            {'amount':'70', 'payment_account':other.pk}, format='json')
        self.assertEqual(response.status_code, 200)
        self.account.refresh_from_db(); other.refresh_from_db()
        self.assertEqual(self.account.current_balance, 1000)
        self.assertEqual(other.current_balance, 270)

    def test_direct_creation_updates_balances_and_stock(self):
        response = self.client.post('/api/transactions/', {'type':'actual','amount':50,
            'date':'2026-01-01','payment_account':self.account.pk}, format='json')
        self.assertEqual(response.status_code, 201)
        response = self.client.post('/api/stock-transactions/', {'type':'actual','quantity':2,
            'date':'2026-01-01','product':self.product.pk}, format='json')
        self.assertEqual(response.status_code, 201)
        self.account.refresh_from_db(); self.product.refresh_from_db()
        self.assertEqual(self.account.current_balance, 1050)
        self.assertEqual(self.product.current_stock, 2)

    def test_deletion_requires_a_strategy_and_products_are_archived(self):
        doc = process_document_create('invoice', {'date':'2026-01-01','total_amount':50}, self.contact)
        self.assertEqual(self.client.delete(f'/api/documents/{doc.pk}/').status_code, 400)
        _create_stxn('actual', 2, self.product, doc, '2026-01-01')
        self.assertEqual(self.client.delete(f'/api/products/{self.product.pk}/').status_code, 204)
        self.assertTrue(StockTransaction.objects.filter(product=self.product).exists())

    def test_expense_does_not_reset_contact_opening_balance(self):
        process_document_create('invoice', {'date':'2026-01-01','total_amount':500}, self.contact)
        process_document_create('expense', {'date':'2026-01-31','total_amount':20}, self.contact)
        self.assertEqual(compute_opening_balance_for_print(self.contact, date(2026,2,1)), -500)


class ConcurrentBalanceTests(TransactionTestCase):
    def test_simultaneous_payments_do_not_lose_an_update(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Concurrency requires PostgreSQL.')
        account = PaymentAccount.objects.create(type='bank',name='Bank',current_balance=0)
        barrier = Barrier(2)
        def pay(_):
            close_old_connections()
            try:
                account_copy = PaymentAccount.objects.get(pk=account.pk)
                barrier.wait(timeout=5)
                _create_ftxn('actual', Decimal('25'), account=account_copy, date='2026-01-01')
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(pay, range(2)))
        account.refresh_from_db()
        self.assertEqual(account.current_balance, 50)
