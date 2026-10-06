from decimal import Decimal
from datetime import date
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from .calculations import document_totals
from .commands import DocumentWriteSerializer, command_data
from .document_updates import update_document
from .services import process_document_create, _build_document_context
from .reports import gst_document_row
from .report_exports import hsn_report
from shared.models import Contact, Settings
from inventory.models import Product


class ItemDiscountTests(TestCase):
    def data(self):
        return {'type': 'invoice', 'date': '2026-04-01', 'tax_mode': 'item', 'discount_percentage': 10,
                'line_items': [{'name': 'A', 'hsn': '1001', 'amount': 100, 'quantity': 1, 'discount_percentage': 10, 'taxes': [{'name': 'IGST', 'percentage': 5}]},
                               {'name': 'B', 'hsn': '1002', 'amount': 100, 'quantity': 1, 'discount': 20, 'taxes': [{'name': 'IGST', 'percentage': 18}]}]}

    def test_item_discounts_then_overall_discount_then_mixed_tax(self):
        totals = document_totals(self.data())
        self.assertEqual(totals['gross_subtotal'], Decimal('200'))
        self.assertEqual(totals['item_discount_total'], Decimal('30'))
        self.assertEqual(totals['subtotal'], Decimal('170'))
        self.assertEqual(totals['discount'], Decimal('17'))
        self.assertEqual(totals['taxable_amount'], Decimal('153'))
        self.assertEqual(totals['tax_total'], Decimal('17.01'))
        self.assertEqual(totals['total'], Decimal('170.01'))
        shared = {**self.data(), 'tax_mode': 'document', 'taxes': [{'name': 'IGST', 'percentage': 18}]}
        self.assertEqual(document_totals(shared)['total'], Decimal('180.54'))
        with_charges = {**self.data(), 'charges': [{'name': 'Packing', 'amount': 10}]}
        self.assertEqual(document_totals(with_charges)['total'], Decimal('181.12'))

    def test_edit_updates_ledger_and_hsn_while_reference_and_print_keep_item_discounts(self):
        settings = Settings.get();settings.auto_transaction = False;settings.auto_stock = False;settings.save()
        product = Product.objects.create(name='A', rate=100, unit='pcs')
        data = self.data();data['line_items'][0]['product_id'] = product.pk
        contact = Contact.objects.create(contact_name='Item discount review')
        serializer = DocumentWriteSerializer(data=data);self.assertTrue(serializer.is_valid(), serializer.errors)
        doc = process_document_create('invoice', command_data(serializer), contact)
        self.assertEqual(doc.total_amount, Decimal('170.01'))
        self.assertEqual(doc.transactions.get(type='record').amount, Decimal('-170.01'))
        client = APIClient();client.force_authenticate(get_user_model().objects.create_user('item-discount-review'))
        copied = client.get(f'/api/documents/{doc.pk}/reference_data/').data
        self.assertEqual(copied['line_items'][0]['discount_percentage'], 10)
        self.assertEqual(copied['line_items'][1]['discount'], 20)
        context = _build_document_context(doc, settings)
        self.assertEqual(context['line_items'][0]['amount_display'], '90.00')
        self.assertEqual(context['line_items'][0]['item_discount_display'], '10.00')
        self.assertEqual(context['line_items'][1]['amount_display'], '80.00')
        rows = data['line_items'];rows[0] = {**rows[0], 'amount': 200, 'quantity': 2}
        edit = DocumentWriteSerializer(doc, data={'line_items': rows}, partial=True);self.assertTrue(edit.is_valid(), edit.errors)
        doc = update_document(doc, command_data(edit))
        self.assertEqual(doc.discount, Decimal('26'))
        self.assertEqual(doc.total_amount, Decimal('255.06'))
        self.assertEqual(doc.line_items[0]['discount'], 20)
        self.assertEqual(doc.transactions.get(type='record').amount, Decimal('-255.06'))
        self.assertEqual(gst_document_row(doc)['amounts']['igst'], '21.06')
        report = hsn_report(date(2026, 4, 1), date(2027, 3, 31))
        self.assertEqual(sum(Decimal(row['taxable_amount']) for row in report['results']), Decimal('234'))
        self.assertEqual(sum(Decimal(row['igst']) for row in report['results']), Decimal('21.06'))

    def test_bad_discounts_rejected_and_zero_full_discount_valid(self):
        for patch in ({'discount': -1}, {'discount': 101}, {'discount_percentage': -1}, {'discount_percentage': 101}, {'discount_percentage': 'NaN'}):
            data = self.data();data['line_items'][0] = {'name': 'A', 'amount': 100, **patch}
            self.assertFalse(DocumentWriteSerializer(data=data).is_valid())
        data = {'line_items': [{'name': 'Free', 'amount': '.05', 'discount_percentage': 100, 'taxes': [{'name': 'IGST', 'percentage': 18}]}], 'tax_mode': 'item'}
        self.assertEqual(document_totals(data)['total'], Decimal('0'))
