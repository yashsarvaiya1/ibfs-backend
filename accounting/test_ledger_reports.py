from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import TestCase
from rest_framework.test import APIClient

from shared.models import Contact, PaymentAccount, Settings
from .models import FinancialTransaction
from .services import process_document_create, process_send_receive, process_transfer
from .payments import payment_status


class LedgerReportTests(TestCase):
    def setUp(self):
        settings = Settings.get()
        settings.auto_transaction = False
        settings.auto_stock = False
        settings.save()
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user('ledger-report-review'))
        self.contact = Contact.objects.create(contact_name='Customer', phone='123', opening_balance=-50)
        self.account = PaymentAccount.objects.create(name='Bank', type='bank', current_balance=1000)
        self.invoice = process_document_create('invoice', {'date': '2026-01-01', 'total_amount': 100}, self.contact)
        result = process_send_receive(self.contact, {'date': '2026-01-02', 'amount': 80,
            'payment_account': self.account.pk, 'document': self.invoice.pk,
            'interest_lines': [{'name': 'Goodwill waiver', 'type': 'discount', 'amount': 20}]}, 'receive')
        self.receipt = FinancialTransaction.objects.get(pk=result['ftxn'])
        self.waiver = FinancialTransaction.objects.get(pk=result['interest_ftxn'])
        expense = process_document_create('expense', {'date': '2026-01-03', 'total_amount': 30,
            'payment_account': self.account.pk}, self.contact)
        self.expense = expense.transactions.get()
        other = PaymentAccount.objects.create(name='Cash', type='cash')
        process_transfer({'from_account': self.account.pk, 'to_account': other.pk,
                          'date': '2026-01-04', 'amount': 10})
        self.transfer = self.account.transactions.get(type='contra')
        # Imported contra rows may carry contacts; they still do not affect the contact ledger.
        self.transfer.contact = self.contact
        self.transfer.save()
        process_send_receive(self.contact, {'date': '2026-01-05', 'amount': 20,
                             'payment_account': self.account.pk}, 'receive')

    def print_context(self, **params):
        with patch('accounting.services.render_to_string', wraps=render_to_string) as render, \
             patch('accounting.services._render_playwright_pdf', return_value=b'%PDF-review'):
            response = self.client.get('/api/transactions/print/', params)
        self.assertEqual(response.status_code, 200)
        return render.call_args.args[1]

    def test_contact_print_includes_obligations_and_correct_debit_credit_sides(self):
        settings = Settings.get(); settings.auto_transaction = True; settings.save()
        context = self.print_context(contact=self.contact.pk, view='ledger')
        rows = {row['doc_id']: row for row in context['transactions'] if row['type_raw'] == 'record'}
        self.assertTrue(rows[self.invoice.doc_id]['debit'])
        self.assertFalse(rows[self.invoice.doc_id]['credit'])
        self.assertTrue(rows[self.waiver.document.doc_id]['credit'])
        receipt = next(row for row in context['transactions'] if row['amount'] == '80.00')
        self.assertTrue(receipt['credit'])
        self.assertEqual(receipt['running_cf'], '50.00')
        self.assertTrue(receipt['balance_debit'])
        self.assertTrue(context['opening_balance_debit'])
        self.assertEqual(context['closing_balance'], '30.00')
        for row in context['transactions']:
            if row['is_expense'] or row['is_contra']:
                self.assertFalse(row['debit'])
                self.assertFalse(row['credit'])

    def test_contact_date_range_and_hidden_rows_keep_authoritative_balances(self):
        context = self.print_context(contact=self.contact.pk, view='ledger', type='actual',
                                     date_from='2026-01-02', date_to='2026-01-02')
        self.assertEqual(context['opening_balance_val'], '150.00')
        self.assertEqual(context['closing_balance'], '50.00')
        self.assertEqual(context['date_from'], date(2026, 1, 2))
        self.assertEqual(len(context['transactions']), 1)
        api = self.client.get(f'/api/contacts/{self.contact.pk}/ledger/',
                              {'date_from': '2026-01-05', 'date_to': '2026-01-05'})
        self.assertEqual(api.data['opening_balance_at'], '-50.00')
        self.assertEqual(api.data['results'][0]['running_cf'], '-30.00')

    def test_account_opening_running_balances_and_expense_transfer_sides(self):
        api = self.client.get(f'/api/accounts/{self.account.pk}/transactions/',
                              {'view': 'ledger', 'date_from': '2026-01-02', 'date_to': '2026-01-04',
                               'page_size': 1, 'page': 2})
        self.assertEqual(api.status_code, 200, api.data)
        self.assertEqual(api.data['balance_before_period'], '1000.00')
        self.assertEqual(api.data['results'][0]['id'], self.expense.pk)
        self.assertEqual(api.data['results'][0]['running_balance'], '1050.00')
        unfiltered = self.client.get(f'/api/accounts/{self.account.pk}/transactions/')
        self.assertEqual(unfiltered.data['balance_before_period'], '1000.00')
        context = self.print_context(account=self.account.pk, view='ledger',
                                     date_from='2026-01-02', date_to='2026-01-04')
        self.assertEqual(context['opening_balance_val'], '1000.00')
        self.assertTrue(context['opening_balance_debit'])
        self.assertEqual(context['closing_balance'], '1040.00')
        self.assertTrue(context['transactions'][0]['debit'])
        self.assertTrue(context['transactions'][1]['credit'])
        self.assertTrue(context['transactions'][2]['credit'])
        self.assertEqual([row['running_cf'] for row in context['transactions']], ['1080.00', '1050.00', '1040.00'])

    def test_contact_opening_balance_edit_refreshes_ledgers_without_changing_cash_or_history(self):
        history = list(FinancialTransaction.objects.values_list('pk', 'amount'))
        self.account.refresh_from_db(); cash = self.account.current_balance
        for opening in ('-25.00', '40.00', '0.00'):
            response = self.client.patch(f'/api/contacts/{self.contact.pk}/', {'opening_balance': opening}, format='json')
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(Decimal(response.data['current_cf']), Decimal(opening) + 20)
            ledger = self.client.get(f'/api/contacts/{self.contact.pk}/ledger/')
            self.assertEqual(Decimal(ledger.data['opening_balance_at']), Decimal(opening))
            self.assertEqual(Decimal(ledger.data['results'][-1]['running_cf']), Decimal(opening) + 20)
        self.assertEqual(list(FinancialTransaction.objects.values_list('pk', 'amount')), history)
        self.account.refresh_from_db(); self.assertEqual(self.account.current_balance, cash)

    def test_account_overdraft_and_zero_opening_use_correct_balance_directions(self):
        for opening in (-100, 0):
            with self.subTest(opening=opening):
                account = PaymentAccount.objects.create(name='Overdraft review', type='bank', current_balance=opening)
                context = self.print_context(account=account.pk, view='ledger')
                self.assertEqual(context['opening_balance_val'], f'{abs(opening):.2f}')
                self.assertFalse(context['opening_balance_debit'])
                self.assertFalse(context['closing_balance_debit'])
                self.assertEqual(context['opening_balance_zero'], opening == 0)
                self.assertEqual(context['closing_balance_zero'], opening == 0)
                listing = self.print_context(account=account.pk, view='list')
                self.assertEqual(listing['balance_before_period'], f'{abs(opening):.2f}')
                self.assertFalse(listing['balance_before_period_debit'])
                self.assertEqual(listing['balance_before_period_zero'], opening == 0)

    def test_bill_invoice_and_notes_use_correct_contact_sides(self):
        for kind, side in (('bill', 'credit'), ('invoice', 'debit'), ('cn', 'credit'), ('dn', 'debit')):
            doc = process_document_create(kind, {'date': '2026-02-01', 'total_amount': 100}, self.contact)
            context = self.print_context(contact=self.contact.pk, view='ledger', date_from='2026-02-01')
            row = next(row for row in context['transactions'] if row['doc_id'] == doc.doc_id)
            self.assertTrue(row[side], kind)
            self.assertFalse(row['credit' if side == 'debit' else 'debit'], kind)

    def test_invalid_ranges_rejected_and_empty_ledgers_keep_opening_balance(self):
        for endpoint in (f'/api/contacts/{self.contact.pk}/ledger/',
                         f'/api/accounts/{self.account.pk}/transactions/', '/api/transactions/print/'):
            response = self.client.get(endpoint, {'date_from': '2026-02-01', 'date_to': '2026-01-01'})
            self.assertEqual(response.status_code, 400)
        context = self.print_context(contact=self.contact.pk, view='ledger',
                                     date_from='2026-02-01', date_to='2026-02-28')
        self.assertEqual(context['transactions'], [])
        self.assertEqual(context['opening_balance_val'], '30.00')
        self.assertEqual(context['closing_balance'], '30.00')

    def test_cash_vouchers_and_adjustments_settle_all_financial_document_types_once(self):
        settings = Settings.get(); settings.enable_vouchers = True; settings.save()
        for kind, direction, obligation_side in (('invoice', 'receive', 'debit'), ('bill', 'send', 'credit'),
                                                  ('cn', 'send', 'credit'), ('dn', 'receive', 'debit')):
            for adjustment, cash, remaining in (('discount', 80, 0), ('charge', 100, 20)):
                with self.subTest(kind=kind, adjustment=adjustment):
                    contact = Contact.objects.create(contact_name=f'{kind} {adjustment}', phone='456')
                    account = PaymentAccount.objects.create(name=f'{kind} cash', type='cash', current_balance=1000)
                    doc = process_document_create(kind, {'date': '2026-03-01', 'total_amount': 100}, contact)
                    response = self.client.post(f'/api/contacts/{contact.pk}/{direction}/', {
                        'amount': cash, 'payment_account': account.pk, 'document': doc.pk, 'date': '2026-03-02',
                        'interest_lines': [{'name': 'Late fee' if adjustment == 'charge' else 'Goodwill waiver',
                                           'type': adjustment, 'amount': 20}]}, format='json')
                    self.assertEqual(response.status_code, 201, response.data)
                    payment = FinancialTransaction.objects.get(pk=response.data['ftxn'])
                    self.assertEqual(payment.document.type, 'cash_receipt_voucher' if direction == 'receive' else 'cash_payment_voucher')
                    self.assertEqual(payment.document.total_amount, cash)
                    self.assertEqual(payment.document.reference_id, doc.pk)
                    self.assertEqual(contact.transactions.filter(type='actual').count(), 1)
                    account.refresh_from_db()
                    self.assertEqual(account.current_balance, 1000 + (cash if direction == 'receive' else -cash))
                    self.assertEqual(Decimal(payment_status(doc)['remaining']), remaining)
                    balance = self.client.get(f'/api/contacts/{contact.pk}/').data['current_cf']
                    self.assertEqual(Decimal(balance), remaining * (-1 if direction == 'receive' else 1))
                    context = self.print_context(contact=contact.pk, view='ledger')
                    self.assertEqual(len(context['transactions']), 3)
                    obligation = next(row for row in context['transactions'] if row['doc_id'] == doc.doc_id)
                    self.assertTrue(obligation[obligation_side])
                    actual = next(row for row in context['transactions'] if row['type_raw'] == 'actual')
                    self.assertTrue(actual['credit' if direction == 'receive' else 'debit'])
                    interest = next(row for row in context['transactions'] if row['doc_id'] not in (doc.doc_id, payment.document.doc_id))
                    expected_side = obligation_side if adjustment == 'charge' else ('credit' if obligation_side == 'debit' else 'debit')
                    self.assertTrue(interest[expected_side])
                    self.assertEqual(context['closing_balance'], f'{remaining:.2f}')
                    account_context = self.print_context(account=account.pk, view='ledger')
                    self.assertEqual(len(account_context['transactions']), 1)
                    self.assertTrue(account_context['transactions'][0]['debit' if direction == 'receive' else 'credit'])

    def test_standalone_interest_charge_and_waiver_direction_and_edits(self):
        from .services import process_standalone_interest
        from .document_updates import update_document
        from .models import Document
        for toggle, charge_side in (('we_receive', 'debit'), ('we_pay', 'credit')):
            for line_type in ('charge', 'discount'):
                with self.subTest(toggle=toggle, line_type=line_type):
                    contact = Contact.objects.create(contact_name=f'{toggle} {line_type}', phone='789')
                    result = process_standalone_interest(contact, {'date': '2026-03-01', 'toggle': toggle,
                        'line_items': [{'name': 'Interest adjustment', 'amount': 20, 'type': line_type}]})
                    context = self.print_context(contact=contact.pk, view='ledger')
                    self.assertEqual(len(context['transactions']), 1)
                    side = charge_side if line_type == 'charge' else ('credit' if charge_side == 'debit' else 'debit')
                    self.assertTrue(context['transactions'][0][side])
                    self.assertEqual(context['closing_balance'], '20.00')
                    doc = Document.objects.get(pk=result['interest_doc'])
                    update_document(doc, {'line_items': [{'name': 'Interest adjustment', 'amount': 30, 'type': line_type}]})
                    edited = self.print_context(contact=contact.pk, view='ledger')
                    self.assertTrue(edited['transactions'][0][side])
                    self.assertEqual(edited['closing_balance'], '30.00')
                    self.assertFalse(contact.transactions.filter(type='actual').exists())

    def test_linked_credit_and_debit_notes_reduce_original_balance(self):
        for original_type, note_type, expected, side in (('invoice', 'cn', -70, 'credit'), ('bill', 'dn', 70, 'debit')):
            contact = Contact.objects.create(contact_name=original_type, phone='111')
            original = process_document_create(original_type, {'date': '2026-03-01', 'total_amount': 100}, contact)
            note = process_document_create(note_type, {'date': '2026-03-02', 'total_amount': 30, 'reference': original.pk}, contact)
            self.assertEqual(Decimal(self.client.get(f'/api/contacts/{contact.pk}/').data['current_cf']), expected)
            context = self.print_context(contact=contact.pk, view='ledger')
            self.assertTrue(next(row for row in context['transactions'] if row['doc_id'] == note.doc_id)[side])
            self.assertEqual(context['closing_balance'], '70.00')
