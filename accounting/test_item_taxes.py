from decimal import Decimal
from django.test import TestCase
from rest_framework.exceptions import ValidationError
from .calculations import document_totals
from .commands import DocumentWriteSerializer, command_data
from .document_updates import update_document
from .services import process_document_create
from .reports import gst_document_row
from shared.models import Contact, PaymentAccount, Settings
from inventory.models import Product


class ItemTaxTests(TestCase):
    def test_mixed_rates_and_cent_allocation_conserve_taxable_value(self):
        data = {'tax_mode': 'item', 'discount': '.01', 'charges': [], 'line_items': [
            {'name': 'A', 'amount': '0.03', 'taxes': [{'name': 'IGST', 'percentage': 5}]},
            {'name': 'B', 'amount': '0.03', 'taxes': [{'name': 'IGST', 'percentage': 18}]},
            {'name': 'C', 'amount': '0.03', 'taxes': []}]}
        totals = document_totals(data)
        self.assertEqual(sum(row['taxable_amount'] for row in totals['line_details']), Decimal('.08'))
        self.assertEqual(totals['line_details'][0]['taxable_amount'], Decimal('.02'))
        self.assertEqual(totals['total'], Decimal('.09'))
        data['line_items'] = [{'name': 'A', 'amount': 100, 'taxes': [{'name': 'IGST', 'percentage': 5}]},
                              {'name': 'B', 'amount': 100, 'taxes': [{'name': 'IGST', 'percentage': 18}]}]
        data['discount'] = 20
        self.assertEqual(document_totals(data)['total'], Decimal('200.70'))

    def test_item_mode_rejects_double_tax_and_missing_details(self):
        for data in ({'type': 'invoice', 'tax_mode': 'item'},
                     {'type': 'invoice', 'tax_mode': 'item', 'line_items': [{'name': 'A', 'amount': 1}], 'taxes': [{'name': 'IGST', 'percentage': 5}]},
                     {'type': 'invoice', 'tax_mode': 'item', 'line_items': [{'name': 'A', 'amount': 1, 'taxes': [{'name': 'IGST', 'percentage': 101}]}]},
                     {'type': 'invoice', 'supply_category': 'exempt', 'line_items': [{'name': 'A', 'amount': 1}], 'taxes': [{'name': 'IGST', 'percentage': 18}]}):
            serializer = DocumentWriteSerializer(data=data)
            self.assertFalse(serializer.is_valid())

    def test_tax_only_edit_updates_obligation_and_retains_cash_and_stock(self):
        settings = Settings.get(); settings.auto_stock = True; settings.auto_transaction = True; settings.save()
        contact = Contact.objects.create(contact_name='Mixed review', phone='123')
        account = PaymentAccount.objects.create(name='Bank', type='bank', current_balance=1000)
        product = Product.objects.create(name='A', rate=100)
        item = {'name': 'A', 'amount': 100, 'quantity': 1, 'rate': 100, 'product_id': product.pk, 'taxes': [{'name': 'IGST', 'percentage': 5}]}
        doc = process_document_create('invoice', {'tax_mode': 'item', 'line_items': [item], 'payment_account': account.pk, 'reverse_charge': False, 'place_of_supply': 'Delhi'}, contact)
        actual = list(doc.transactions.filter(type='actual').values_list('id', 'amount'))
        item['taxes'][0]['percentage'] = 18
        doc = update_document(doc, {'line_items': [item]})
        self.assertEqual(doc.total_amount, Decimal('118'))
        self.assertEqual(doc.transactions.get(type='record').amount, Decimal('-118'))
        self.assertEqual(list(doc.transactions.filter(type='actual').values_list('id', 'amount')), actual)
        account.refresh_from_db(); product.refresh_from_db()
        self.assertEqual(account.current_balance, Decimal('1105'))
        self.assertEqual(product.current_stock, -1)
        row = gst_document_row(doc)
        self.assertEqual(row['amounts']['igst'], '18.00')
        self.assertEqual(row['bucket'], 'output')

    def test_partial_metadata_edit_checks_existing_classification_and_taxes(self):
        doc = process_document_create('invoice', {'line_items': [{'name': 'A', 'amount': 100}], 'taxes': [{'name': 'IGST', 'percentage': 18}]})
        with self.assertRaises(ValidationError):
            update_document(doc, {'supply_category': 'nil_rated'})

    def test_mixed_exempt_and_taxable_items_keep_separate_hsn_classification(self):
        from .report_exports import hsn_report
        from datetime import date
        data = {'type': 'invoice', 'tax_mode': 'item', 'reverse_charge': False, 'place_of_supply': 'Delhi', 'date': '2026-04-01', 'line_items': [
            {'name': 'A', 'hsn': '1001', 'unit': 'pcs', 'quantity': 1, 'amount': 100, 'supply_category': 'taxable', 'taxes': [{'name': 'IGST', 'percentage': 18}]},
            {'name': 'B', 'hsn': '1002', 'unit': 'pcs', 'quantity': 1, 'amount': 100, 'supply_category': 'exempt', 'taxes': []}]}
        serializer = DocumentWriteSerializer(data=data); self.assertTrue(serializer.is_valid(), serializer.errors)
        doc = process_document_create('invoice', command_data(serializer))
        self.assertEqual(doc.total_amount, Decimal('218'))
        report = hsn_report(date(2026, 4, 1), date(2027, 3, 31))
        self.assertEqual([row['supply_category'] for row in report['results']], ['taxable', 'exempt'])
        data['line_items'][1]['taxes'] = [{'name': 'IGST', 'percentage': 18}]
        self.assertFalse(DocumentWriteSerializer(data=data).is_valid())
