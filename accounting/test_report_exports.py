from datetime import date
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient
from pypdf import PdfReader
from .models import Document, FinancialTransaction, PaymentAllocation
from .report_exports import hsn_report, gst_csv_rows, spreadsheet_cell, allocation_review
from shared.models import Contact


class ExportCalculationTests(TestCase):
    def setUp(self):
        self.start, self.end = date(2026, 4, 1), date(2027, 3, 31)
        self.contact = Contact.objects.create(contact_name='=UNSAFE()', phone='123', gstin='27TEST1234X1Z1')

    def document(self, **data):
        return Document.objects.create(type='invoice', doc_id=f'DOC-{Document.objects.count()}', date=self.start,
            contact=self.contact, reverse_charge=False, place_of_supply='Delhi', **data)

    def test_shared_tax_hsn_allocation_conserves_exact_component_totals(self):
        self.document(line_items=[{'name': 'A', 'amount': '.05', 'hsn': '0010', 'unit': 'pcs', 'quantity': 1}, {'name': 'B', 'amount': '.05', 'hsn': '0020', 'unit': 'pcs', 'quantity': 1}], taxes=[{'name': 'IGST', 'percentage': 18}], discount='.01', total_amount='.11')
        data = hsn_report(self.start, self.end)
        self.assertEqual(sum(Decimal(row['taxable_amount']) for row in data['results']), Decimal('.09'))
        self.assertEqual(sum(Decimal(row['igst']) for row in data['results']), Decimal('.02'))
        self.assertEqual(data['results'][0]['hsn'], '0010')
        rows = list(gst_csv_rows(self.start, self.end))
        self.assertEqual(len(rows[0]), len(rows[1]))
        self.assertEqual(spreadsheet_cell(rows[1][4]), "'=UNSAFE()")
        self.assertEqual(spreadsheet_cell(Decimal('-18.00')), '-18.00')
        self.assertEqual(spreadsheet_cell('  +SUM(A1)'), "'  +SUM(A1)")

    def test_item_rates_and_missing_quantity_are_not_inferred(self):
        self.document(tax_mode='item', line_items=[{'name': 'A', 'amount': 100, 'hsn': '1001', 'taxes': [{'name': 'IGST', 'percentage': 5}]}, {'name': 'B', 'amount': 100, 'hsn': '1002', 'quantity': 2, 'taxes': [{'name': 'IGST', 'percentage': 18}]}], total_amount=223)
        data = hsn_report(self.start, self.end)
        self.assertEqual(data['results'][0]['quantity'], None)
        self.assertEqual([row['gst_total'] for row in data['results']], ['5.00', '18.00'])
        self.assertEqual(data['results'][0]['unit'], '')

    def test_review_documents_are_not_silently_added_to_hsn_totals(self):
        self.document(line_items=[], total_amount=100)
        data = hsn_report(self.start, self.end)
        self.assertEqual(len(data['excluded']), 1)
        self.assertEqual(data['results'], [])

    def test_payment_review_reserves_adjustments_and_excludes_expenses(self):
        invoice = self.document(line_items=[{'name': 'A', 'amount': 100}], total_amount=100)
        payment = FinancialTransaction.objects.create(type='actual', date=self.start, amount=110, contact=self.contact)
        interest = Document.objects.create(type='interest', doc_id='INT', date=self.start, total_amount=10)
        PaymentAllocation.objects.create(payment=payment, document=invoice, amount=100)
        PaymentAllocation.objects.create(payment=payment, document=interest, amount=10)
        self.assertEqual(allocation_review(self.start, self.end, 1, 50)['count'], 0)
        payment.amount = 120; payment.save()
        self.assertEqual(allocation_review(self.start, self.end, 1, 50)['results'][0]['unallocated'], '10.00')


class ExportEndpointTests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient(); self.client.force_authenticate(get_user_model().objects.create_user('export'))
        for i in range(52):
            Document.objects.create(type='invoice', doc_id=f'EXPORT-{i}', date='2026-05-01', line_items=[{'name': 'A', 'amount': 100, 'quantity': 1, 'hsn': '0010', 'unit': 'pcs'}], taxes=[{'name': 'IGST', 'percentage': 18}], total_amount=118, reverse_charge=False, place_of_supply='Delhi')

    def test_csv_covers_all_pages_and_does_not_mutate_books(self):
        response = self.client.get('/api/reports/gst_export/', {'fy': 2026, 'export_format': 'csv'})
        self.assertEqual(response.status_code, 200)
        text = b''.join(response.streaming_content).decode('utf-8-sig')
        self.assertIn('EXPORT-51', text)
        self.assertEqual(len(text.splitlines()), 53)
        self.assertEqual(FinancialTransaction.objects.count(), 0)
        self.assertEqual(self.client.get('/api/reports/gst_export/', {'export_format': 'bad'}).status_code, 400)

    def test_real_report_pdf_contains_full_register_and_repeated_headers(self):
        response = self.client.get('/api/reports/gst_export/', {'fy': 2026, 'export_format': 'pdf'})
        self.assertEqual(response.status_code, 200)
        reader = PdfReader(BytesIO(b''.join(response.streaming_content)))
        text = '\n'.join(page.extract_text() for page in reader.pages)
        self.assertGreater(len(reader.pages), 1)
        self.assertIn('EXPORT-51', text)
        self.assertIn('936.00', text)
        self.assertIn('Document / supplier', reader.pages[-1].extract_text())
