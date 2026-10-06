from datetime import date
from io import BytesIO
from datetime import timedelta
from unittest.mock import patch
from django.utils import timezone
from pypdf import PdfReader

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase
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
        self.assertEqual(tax_component('GST Cess'), 'cess')
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

    def test_missing_line_amount_is_not_silently_counted_as_zero(self):
        doc = self.document(line_items=[{'name': 'Incomplete item', 'quantity': 2}], total_amount=0)
        row = gst_document_row(doc)
        self.assertEqual(row['bucket'], 'review')
        self.assertIsNone(row['taxable_amount'])

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

    def test_review_filter_keeps_full_period_summary(self):
        self.document()
        self.document(line_items=[])
        report = gst_report(self.start, self.end, review_only=True)
        self.assertEqual(report['count'], 1)
        self.assertEqual(report['document_count'], 2)
        self.assertEqual(len(report['results']), 1)
        self.assertEqual(report['totals']['output']['gst_total'], '18.00')

    def test_authentication_dates_and_page_validation(self):
        self.assertEqual(self.client.get('/api/reports/gst/', {'fy': 2026}).status_code, 200)
        for params in ({'date_from': 'oops'}, {'date_from': '2026-06-01', 'date_to': '2026-04-01'}, {'page': 0}, {'page_size': 101}):
            self.assertEqual(self.client.get('/api/reports/gst/', params).status_code, 400)
        self.client.force_authenticate(None)
        self.assertIn(self.client.get('/api/reports/gst/').status_code, (401, 403))


class CAExportTests(TransactionTestCase):
    setUp = GSTReportTests.setUp
    document = GSTReportTests.document

    def test_list_has_stable_chronological_scope_and_all_types(self):
        later = self.document(date=self.end)
        earlier = self.document('quotation')
        self.document(is_active=False)
        self.document(date=date(2026, 3, 31))
        response = self.client.get('/api/reports/ca_documents/', {'fy': 2026, 'page_size': 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(response.data['results'][0]['id'], earlier.pk)
        as_of = response.data['as_of']
        self.document('bill')  # Newly created documents are not silently selected.
        second = self.client.get('/api/reports/ca_documents/', {'fy': 2026, 'page_size': 1, 'page': 2, 'as_of': as_of})
        self.assertEqual(second.data['count'], 2)
        self.assertEqual(second.data['results'][0]['id'], later.pk)

    @patch('accounting.reports.generate_ca_pack')
    def test_exclusions_and_date_type_filters_apply_on_server(self, generate):
        first = self.document()
        second = self.document()
        self.document('bill')
        self.document(date=date(2026, 3, 31))
        generate.return_value = BytesIO(b'%PDF-test')
        response = self.client.post('/api/reports/ca_export/', {'fy': 2026, 'types': ['invoice'], 'excluded_ids': [first.pk], 'expected_count': 1}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([doc.pk for doc in generate.call_args.args[0]], [second.pk])
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertEqual(response['X-Document-Count'], '1')
        self.assertIn('CA_Documents_2026-04-01_2027-03-31.pdf', response['Content-Disposition'])
        response.close()

    @patch('accounting.reports.generate_ca_pack')
    def test_empty_selection_never_falls_back_to_every_document(self, generate):
        doc = self.document()
        for selection in ({'ids': []}, {'excluded_ids': [doc.pk]}):
            response = self.client.post('/api/reports/ca_export/', {'fy': 2026, **selection}, format='json')
            self.assertEqual(response.status_code, 400)
        generate.assert_not_called()

    @patch('accounting.reports.generate_ca_pack')
    def test_changed_or_removed_documents_require_review(self, generate):
        doc = self.document()
        as_of = timezone.now()
        Document.objects.filter(pk=doc.pk).update(updated_at=as_of + timedelta(seconds=1))
        response = self.client.post('/api/reports/ca_export/', {'fy': 2026, 'as_of': as_of.isoformat(), 'ids': [doc.pk]}, format='json')
        self.assertEqual(response.status_code, 409)
        response = self.client.post('/api/reports/ca_export/', {'fy': 2026, 'ids': [doc.pk, 999999]}, format='json')
        self.assertEqual(response.status_code, 409)
        generate.assert_not_called()

    def test_pack_validation_and_authentication(self):
        for data in ({'types': ['invalid']}, {'ids': ['oops']}, {'ids': [], 'excluded_ids': []}, {'date_from': '2026-05-01', 'date_to': '2026-04-01'}):
            self.assertEqual(self.client.post('/api/reports/ca_export/', data, format='json').status_code, 400)
        self.client.force_authenticate(None)
        self.assertIn(self.client.post('/api/reports/ca_export/', {}, format='json').status_code, (401, 403))

    def test_real_pdf_contains_selected_documents_and_preserves_final_totals(self):
        short = self.document(doc_id='CA-SHORT-001')
        self.document(doc_id='CA-EXCLUDED-002')
        long = self.document(doc_id='CA-LONG-003', line_items=[{'name': f'Item {i:03d}', 'amount': 100} for i in range(32)], total_amount=3776)
        response = self.client.post('/api/reports/ca_export/', {'fy': 2026, 'ids': [short.pk, long.pk]}, format='json')
        self.assertEqual(response.status_code, 200)
        pages = [page.extract_text() for page in PdfReader(BytesIO(b''.join(response.streaming_content))).pages]
        response.close()
        self.assertGreater(len(pages), 2)
        all_text = '\n'.join(pages)
        self.assertIn('CA-SHORT-001', pages[0])
        self.assertNotIn('CA-EXCLUDED-002', all_text)
        self.assertEqual(all_text.count('3,776.00'), 1)
        long_pages = [text for text in pages if 'CA-LONG-003' in text]
        for text in long_pages:
            self.assertIn('Total INR', text)
            self.assertIn('amount in words', text.lower())
        for text in long_pages[:-1]:
            self.assertNotIn('3,776.00', text)
        self.assertIn('3,776.00', long_pages[-1])
