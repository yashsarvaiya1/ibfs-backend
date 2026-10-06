from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from shared.models import Contact
from .models import Document
from .reports import gst_document_row, gst_report, tax_component


class GSTReportTests(TestCase):
    def setUp(self):
        self.contact = Contact.objects.create(contact_name='CA review', phone='9876543210', gstin='27TEST1234X1Z1')
        self.start, self.end = date(2026, 4, 1), date(2027, 3, 31)
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user('report-review'))

    def document(self, kind='invoice', **overrides):
        data = dict(type=kind, doc_id=f'DOC-{Document.objects.count()}', contact=self.contact,
                    date=self.start, line_items=[{'name': 'Item', 'amount': 100}],
                    taxes=[{'name': 'CGST', 'percentage': 9}, {'name': 'SGST', 'percentage': 9}],
                    total_amount=118, reverse_charge=False, place_of_supply='Maharashtra (27)')
        data.update(overrides)
        return Document.objects.create(**data)

    def test_decimal_base_charges_discount_and_rounding(self):
        doc = self.document(line_items=[{'name': 'Item', 'amount': '100.05'}],
                            charges=[{'name': 'Packing', 'amount': 10}], discount=5, total_amount='123.95')
        row = gst_document_row(doc)
        self.assertEqual(row['amounts']['cgst'], '9.45')
        self.assertEqual(row['taxable_amount'], '105.05')
        self.assertEqual(row['bucket'], 'output')

    def test_financial_year_inclusive_and_non_posting_exclusion(self):
        self.document(date=self.end)
        self.document(date=date(2026, 3, 31))
        self.document(date=date(2027, 4, 1))
        self.document(is_active=False)
        for kind in ('quotation', 'po', 'pi', 'challan', 'cash_receipt_voucher', 'interest'):
            self.document(kind)
        report = gst_report(self.start, self.end)
        self.assertEqual(report['count'], 1)
        self.assertEqual(report['totals']['output']['gst_total'], '18.00')

    def test_notes_match_ibfs_sales_and_purchase_return_flow(self):
        invoice = self.document()
        bill = self.document('bill')
        self.document('cn', reference=invoice)
        self.document('dn', reference=bill)
        self.document('dn', reference=invoice)
        self.document('cn')
        report = gst_report(self.start, self.end)
        self.assertEqual(report['totals']['output']['gst_total'], '0.00')
        self.assertEqual(report['totals']['purchase']['gst_total'], '0.00')
        self.assertEqual(report['review_count'], 2)
        self.assertEqual(report['totals']['review']['gst_total'], '-36.00')

    def test_rcm_is_separate_from_normal_purchase_gst_and_difference(self):
        self.document()
        self.document('bill', reverse_charge=True)
        report = gst_report(self.start, self.end)
        self.assertEqual(report['totals']['purchase']['gst_total'], '0.00')
        self.assertEqual(report['totals']['rcm_purchase']['gst_total'], '18.00')
        self.assertEqual(report['difference']['gst_total'], '18.00')

    def test_unsplit_and_other_tax_are_not_invented_components(self):
        self.assertEqual(tax_component('CGST 9%'), 'cgst')
        doc = self.document(taxes=[{'name': 'GST', 'percentage': 18}, {'name': 'VAT', 'percentage': 1}], total_amount=119)
        row = gst_document_row(doc)
        self.assertEqual(row['amounts']['unsplit_gst'], '18.00')
        self.assertEqual(row['amounts']['other_tax'], '1.00')
        self.assertEqual(row['amounts']['gst_total'], '18.00')
        self.assertTrue(row['issues'])

    def test_fast_and_mismatched_totals_need_review(self):
        fast = self.document(line_items=[], total_amount=1180)
        mismatch = self.document(total_amount=120)
        self.assertIsNone(gst_document_row(fast)['taxable_amount'])
        self.assertEqual(gst_document_row(fast)['bucket'], 'review')
        self.assertEqual(gst_document_row(mismatch)['bucket'], 'review')
        self.assertEqual(gst_report(self.start, self.end)['totals']['output']['gst_total'], '0.00')

    def test_pagination_does_not_reduce_summary_or_months(self):
        for _ in range(3):
            self.document()
        report = gst_report(self.start, self.end, page=2, page_size=1)
        self.assertEqual(len(report['results']), 1)
        self.assertEqual(report['count'], 3)
        self.assertEqual(report['totals']['output']['gst_total'], '54.00')
        self.assertEqual(report['months'][0]['totals']['output']['gst_total'], '54.00')

    def test_expense_tax_and_conflicting_components_are_reviewed(self):
        expense = self.document('expense')
        conflict = self.document(taxes=[{'name': 'IGST', 'percentage': 9}, {'name': 'CGST', 'percentage': 9}])
        self.assertEqual(gst_document_row(expense)['bucket'], 'review')
        self.assertEqual(gst_document_row(conflict)['bucket'], 'review')

    def test_authentication_dates_and_page_validation(self):
        self.assertEqual(self.client.get('/api/reports/gst/', {'fy': 2026}).status_code, 200)
        for params in ({'date_from': 'oops'}, {'date_from': '2026-06-01', 'date_to': '2026-04-01'}, {'page': 0}, {'page_size': 101}):
            self.assertEqual(self.client.get('/api/reports/gst/', params).status_code, 400)
        self.client.force_authenticate(None)
        self.assertIn(self.client.get('/api/reports/gst/').status_code, (401, 403))
