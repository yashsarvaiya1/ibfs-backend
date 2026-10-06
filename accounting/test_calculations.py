from types import SimpleNamespace
from decimal import Decimal
from django.test import SimpleTestCase
from .calculations import document_totals, decimal_value
from .workflows import stock_mode, OUTGOING_TYPES


class WorkflowCalculationTests(SimpleTestCase):
    def test_totals_include_charges_discount_and_tax(self):
        result = document_totals({'line_items': [{'amount': 200}],
            'charges': [{'amount': 20}], 'discount': 20, 'taxes': [{'percentage': 18}]})
        self.assertEqual(result['total'], Decimal('236.00'))

    def test_interest_discount_is_subtracted(self):
        result = document_totals({'line_items': [{'amount': 100, 'type': 'charge'},
            {'amount': 20, 'type': 'discount'}]}, 'interest')
        self.assertEqual(result['total'], Decimal('80.00'))

    def test_money_uses_explicit_half_up_rounding(self):
        self.assertEqual(document_totals({'line_items': [{'amount': '1.005'}]})['total'], Decimal('1.01'))

    def test_nonfinite_values_are_rejected(self):
        for value in ('NaN', 'Infinity', '-Infinity'):
            with self.assertRaises(ValueError):
                decimal_value(value)

    def test_challan_owns_stock_when_enabled(self):
        settings = SimpleNamespace(auto_stock=True, enable_challan=True)
        self.assertEqual(stock_mode('invoice', settings), 'none')
        self.assertEqual(stock_mode('challan', settings, 'invoice'), 'actual')
        self.assertEqual(stock_mode('challan', settings), 'none')

    def test_reference_documents_never_post_stock(self):
        settings = SimpleNamespace(auto_stock=False, enable_challan=False)
        for kind in ('quotation', 'po', 'pi', 'expense', 'interest'):
            self.assertEqual(stock_mode(kind, settings), 'none')
        self.assertEqual(stock_mode('bill', settings), 'record')

    def test_note_payment_directions_match_existing_ledger_signs(self):
        self.assertIn('cn', OUTGOING_TYPES)
        self.assertNotIn('dn', OUTGOING_TYPES)
