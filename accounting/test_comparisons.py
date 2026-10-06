from datetime import date
from django.test import TestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from .models import FinancialTransaction, Document
from .comparisons import bank_comparison, purchase_comparison, read_csv, BANK_COLUMNS
from shared.models import Contact, PaymentAccount


class ComparisonTests(TestCase):
    def setUp(self):
        self.start, self.end = date(2026, 4, 1), date(2027, 3, 31)
        self.account = PaymentAccount.objects.create(name='Bank', type='bank', current_balance=1000)
        self.contact = Contact.objects.create(contact_name='Supplier', phone='123', gstin='27ABCDE1234F1Z5')

    def test_bank_candidates_never_post_and_duplicates_are_ambiguous(self):
        txn = FinancialTransaction.objects.create(type='actual', date=self.start, amount=-100, payment_account=self.account)
        row = {'date': '2026-04-01', 'amount': '-100', 'reference': 'REF'}
        data = bank_comparison([row], self.account, self.start, self.end)
        self.assertEqual(data['results'][0]['status'], 'candidate')
        self.assertEqual(data['results'][0]['matches'][0]['id'], txn.pk)
        data = bank_comparison([row, row], self.account, self.start, self.end)
        self.assertTrue(all(r['status'] == 'ambiguous' for r in data['results']))
        self.assertEqual(len(data['unmatched_books']), 1)
        self.account.refresh_from_db(); self.assertEqual(self.account.current_balance, 1000)
        self.assertEqual(FinancialTransaction.objects.count(), 1)
        for amount in ('', 'NaN', '100.001'):
            self.assertEqual(bank_comparison([{**row, 'amount': amount}], self.account, self.start, self.end)['results'][0]['status'], 'invalid')
        self.assertEqual(bank_comparison([{**row, 'date': '20260401'}], self.account, self.start, self.end)['results'][0]['status'], 'invalid')

    def test_purchase_uses_supplier_number_and_compares_components(self):
        doc = Document.objects.create(type='bill', doc_id='IBFS-BILL-001', supplier_invoice_number='S-1', contact=self.contact, date=self.start, line_items=[{'name': 'A', 'amount': 100}], taxes=[{'name': 'CGST', 'percentage': 9}, {'name': 'SGST', 'percentage': 9}], total_amount=118, reverse_charge=False, place_of_supply='Maharashtra')
        row = {'supplier_gstin': self.contact.gstin, 'supplier_invoice_number': 'S-1', 'date': '2026-04-01', 'taxable_amount': '100', 'cgst': '9', 'sgst_utgst': '9', 'igst': '0', 'cess': '0'}
        self.assertEqual(purchase_comparison([row], self.start, self.end)['results'][0]['status'], 'values_agree')
        self.assertEqual(purchase_comparison([{**row, 'igst': '18'}], self.start, self.end)['results'][0]['status'], 'mismatch')
        self.assertEqual(purchase_comparison([{**row, 'cgst': ''}], self.start, self.end)['results'][0]['status'], 'invalid')
        self.assertEqual(purchase_comparison([{**row, 'date': '20260401'}], self.start, self.end)['results'][0]['status'], 'invalid')
        doc.supplier_invoice_number = None; doc.save()
        result = purchase_comparison([{**row, 'supplier_invoice_number': doc.doc_id}], self.start, self.end)
        self.assertEqual(result['results'][0]['status'], 'unmatched')
        self.assertEqual(result['unmatched_books'][0]['id'], doc.pk)

    def test_csv_headers_authentication_and_account_are_checked(self):
        client = APIClient(); file = SimpleUploadedFile('bad.csv', b'date,amount\n2026-04-01,100\n')
        self.assertIn(client.post('/api/reports/compare_csv/', {'file': file, 'kind': 'bank'}, format='multipart').status_code, (401, 403))
        client.force_authenticate(get_user_model().objects.create_user('comparison'))
        file = SimpleUploadedFile('valid.csv', b'date,amount,reference\n2026-04-01,-100,REF\n')
        response = client.post('/api/reports/compare_csv/', {'file': file, 'kind': 'bank', 'account': self.account.pk, 'fy': 2026}, format='multipart')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['results'][0]['status'], 'unmatched')
        from rest_framework.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            read_csv(SimpleUploadedFile('bad.csv', b'date,amount,reference\n2026-04-01,100,REF,EXTRA\n'), BANK_COLUMNS)
