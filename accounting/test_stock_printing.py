from datetime import date
from decimal import Decimal
from django.test import TestCase
from django.template.loader import render_to_string
from inventory.models import Product, StockTransaction
from .services import _build_stock_history_context


class StockPrintTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(name='Widget', rate=10, current_stock=11, unit='pcs')
        self.before = self.movement('actual', 2, date(2026, 5, 1))
        self.actual = self.movement('actual', 3, date(2026, 6, 1))
        self.expected_in = self.movement('record', 5, date(2026, 6, 2))
        self.expected_out = self.movement('record', -1, date(2026, 6, 3))
        self.after = self.movement('actual', -4, date(2026, 7, 1))
        self.start, self.end = date(2026, 6, 1), date(2026, 6, 30)

    def movement(self, kind, quantity, day):
        return StockTransaction.objects.create(type=kind, quantity=quantity, date=day, product=self.product)

    def test_expected_signed_brackets_do_not_change_physical_closing(self):
        context = _build_stock_history_context([self.actual, self.expected_in, self.expected_out], self.product, self.start, self.end)
        self.assertEqual(context['opening_stock'], '12.00')
        self.assertEqual(context['closing_stock'], '15.00')
        self.assertEqual([row['running_stock'] for row in context['transactions']], ['15.00', None, None])
        html = render_to_string('inventory/stock_transactions_print.html', context)
        self.assertIn('(+5.00) pcs', html)
        self.assertIn('(-1.00) pcs', html)
        self.assertIn('+3.00 pcs', html)
        self.assertNotIn('(+3.00)', html)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal('11'))

    def test_empty_and_expected_only_filtered_reports_keep_period_balance(self):
        for movements in ([], [self.expected_in, self.expected_out]):
            context = _build_stock_history_context(movements, self.product, self.start, self.end)
            self.assertEqual(context['opening_stock'], '12.00')
            self.assertEqual(context['closing_stock'], '15.00')
            self.assertTrue(context['show_closing'])
        context = _build_stock_history_context([], self.product, date(2026, 6, 5), self.end)
        self.assertEqual(context['opening_stock'], '15.00')
        self.assertEqual(context['closing_stock'], '15.00')

    def test_filtered_actual_row_balance_includes_hidden_actual_movements(self):
        movement = self.movement('actual', -1, date(2026, 6, 4))
        self.product.current_stock = 10
        self.product.save()
        context = _build_stock_history_context([movement], self.product, self.start, self.end)
        self.assertEqual(context['transactions'][0]['running_stock'], '14.00')
        self.assertEqual(context['closing_stock'], '14.00')

    def test_global_history_has_product_units_without_combined_balance(self):
        other = Product.objects.create(name='Flour', rate=20, current_stock=4, unit='kg')
        movement = StockTransaction.objects.create(type='actual', product=other, quantity=4, date=self.start)
        context = _build_stock_history_context([self.expected_out, movement], date_from=self.start, date_to=self.end)
        html = render_to_string('inventory/stock_transactions_print.html', context)
        self.assertIn('Widget', html)
        self.assertIn('Flour', html)
        self.assertIn('+4.00 kg', html)
        self.assertNotIn('Running Stock', html)
        self.assertNotIn('Closing Stock', html)
        self.assertFalse(context['show_closing'])

    def test_zero_opening_and_closing_remain_visible(self):
        self.product.current_stock = 0
        self.product.save()
        context = _build_stock_history_context([], self.product, date(2026, 8, 1), date(2026, 8, 31))
        self.assertEqual(context['opening_stock'], '0.00')
        self.assertEqual(context['closing_stock'], '0.00')
        html = render_to_string('inventory/stock_transactions_print.html', context)
        self.assertIn('Opening Stock', html)
        self.assertIn('Closing Stock', html)
        self.assertGreaterEqual(html.count('0.00 pcs'), 2)
