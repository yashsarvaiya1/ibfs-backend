from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .commands import DocumentWriteSerializer
from .models import Document


class TotalsPreviewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user('totals-preview-review'))

    def test_unfinished_rows_calculate_without_saving_or_relaxing_save_validation(self):
        data = {'type': 'bill', 'tax_mode': 'item', 'discount_percentage': 10,
                'line_items': [
                    {'name': '', 'quantity': 1, 'rate': 100, 'discount_percentage': 10,
                     'taxes': [{'name': 'IGST', 'percentage': 5}]},
                    {'name': 'Named item', 'amount': 100, 'discount': 20,
                     'taxes': [{'name': 'IGST', 'percentage': 18}]},
                ]}
        response = self.client.post('/api/documents/preview_totals/', data, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['total'], Decimal('170.01'))
        self.assertEqual(response.data['item_discount_total'], Decimal('30'))
        self.assertEqual(response.data['discount'], Decimal('17'))
        self.assertEqual(Document.objects.count(), 0)
        create = self.client.post('/api/documents/', data, format='json')
        self.assertEqual(create.status_code, 400)
        self.assertIn('description', str(create.data))
        self.assertEqual(Document.objects.count(), 0)
        edit = DocumentWriteSerializer(data={'line_items': data['line_items']}, partial=True)
        self.assertFalse(edit.is_valid())
        self.assertIn('description', str(edit.errors))

    def test_blank_zero_row_and_value_only_rows_show_backend_total(self):
        for amount in (0, 100):
            data = {'type': 'bill', 'line_items': [{'name': '', 'amount': amount}]}
            response = self.client.post('/api/documents/preview_totals/', data, format='json')
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data['total'], Decimal(amount))

    def test_unfinished_rows_still_validate_taxes_discounts_and_line_shapes(self):
        for row in ({'name': '', 'amount': 100, 'discount': 101},
                    {'name': '', 'amount': 100, 'taxes': [{'name': 'IGST', 'percentage': 101}]},
                    {'name': '', 'amount': 'NaN'}, 'not a line'):
            response = self.client.post('/api/documents/preview_totals/',
                                        {'type': 'bill', 'line_items': [row]}, format='json')
            self.assertEqual(response.status_code, 400, response.data)
