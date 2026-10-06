from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from .calculations import document_totals
from .commands import DocumentWriteSerializer, command_data
from .services import process_document_create
from .document_updates import update_document
from .reports import gst_document_row
from shared.models import Contact, Settings


class PercentageDiscountTests(TestCase):
    def data(self):
        return {'type': 'invoice', 'tax_mode': 'item', 'discount_percentage': 10,
                'charges': [{'name': 'Packing', 'amount': 20}],
                'line_items': [{'name': 'A', 'amount': 100, 'taxes': [{'name': 'IGST', 'percentage': 5}]}, {'name': 'B', 'amount': 100, 'taxes': [{'name': 'IGST', 'percentage': 18}]}]}

    def test_percentage_uses_subtotal_before_charges_and_mixed_tax(self):
        totals = document_totals(self.data())
        self.assertEqual(totals['discount'], Decimal('20.00'))
        self.assertEqual(totals['taxable_amount'], Decimal('200'))
        self.assertEqual(totals['total'], Decimal('223'))
        shared = {**self.data(), 'tax_mode': 'document', 'taxes': [{'name': 'IGST', 'percentage': 18}]}
        self.assertEqual(document_totals(shared)['total'], Decimal('236'))

    def test_create_edit_and_report_keep_percentage_and_decimal_deduction(self):
        settings = Settings.get(); settings.auto_transaction = True; settings.auto_stock = False; settings.save()
        contact = Contact.objects.create(contact_name='Discount review')
        serializer = DocumentWriteSerializer(data=self.data()); self.assertTrue(serializer.is_valid(), serializer.errors)
        doc = process_document_create('invoice', command_data(serializer), contact)
        self.assertEqual(doc.discount_percentage, Decimal('10'))
        self.assertEqual(doc.discount, Decimal('20'))
        self.assertEqual(doc.total_amount, Decimal('223'))
        items = [{**item, 'amount': 200} for item in self.data()['line_items']]
        edit = DocumentWriteSerializer(doc, data={'line_items': items}, partial=True); self.assertTrue(edit.is_valid(), edit.errors)
        doc = update_document(doc, command_data(edit))
        self.assertEqual(doc.discount, Decimal('40'))
        self.assertEqual(doc.total_amount, Decimal('423.70'))
        self.assertEqual(gst_document_row(doc)['amounts']['igst'], '43.70')
        edit = DocumentWriteSerializer(doc, data={'discount': 0, 'discount_percentage': None}, partial=True); self.assertTrue(edit.is_valid(), edit.errors)
        doc = update_document(doc, command_data(edit)); self.assertIsNone(doc.discount_percentage)
        self.assertEqual(doc.total_amount, Decimal('468.30'))

    def test_preview_recalculates_item_tax_percentage(self):
        client = APIClient(); client.force_authenticate(get_user_model().objects.create_user('preview-review'))
        data = self.data()
        response = client.post('/api/documents/preview_totals/', data, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Decimal(str(response.data['total'])), Decimal('223'))
        data['line_items'][0]['taxes'][0]['percentage'] = 12
        response = client.post('/api/documents/preview_totals/', data, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Decimal(str(response.data['total'])), Decimal('230'))

    def test_invalid_percentages_and_amount_only_mode_rejected(self):
        for data in ({**self.data(), 'discount_percentage': -1}, {**self.data(), 'discount_percentage': 101}, {'type': 'invoice', 'discount_percentage': 5, 'total_amount': 100}):
            serializer = DocumentWriteSerializer(data=data)
            self.assertFalse(serializer.is_valid())
        self.assertEqual(document_totals({**self.data(), 'discount_percentage': 100, 'charges': []})['total'], Decimal('0'))
