from decimal import Decimal
from django.test import TestCase
from rest_framework.exceptions import ValidationError
from shared.models import Settings, Contact, PaymentAccount
from inventory.models import Product, StockTransaction
from .services import process_document_create, process_move_stock, process_standalone_interest
from .document_updates import update_document
from .commands import EditConflict


class DocumentEditTests(TestCase):
    def setUp(self):
        self.settings = Settings.get()
        self.contact = Contact.objects.create(contact_name='Customer',phone='123')
        self.product = Product.objects.create(name='Product',rate=10)
        self.account = PaymentAccount.objects.create(name='Bank',type='bank',current_balance=1000)

    def line(self, quantity):
        return {'name':'Product','quantity':quantity,'rate':10,'amount':quantity*10,'product_id':self.product.pk}

    def create(self, kind='invoice', **data):
        return process_document_create(kind, {'date':'2026-01-01','line_items':[self.line(5)], **data}, self.contact)

    def test_auto_stock_edit_adjusts_actual_stock_without_creating_records(self):
        doc = self.create()
        update_document(doc, {'line_items':[self.line(7)]})
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, -7)
        self.assertFalse(StockTransaction.objects.filter(document=doc,type='record').exists())

    def test_challan_keeps_inventory_responsibility_after_settings_change(self):
        self.settings.enable_challan=True; self.settings.save()
        doc = self.create()
        self.settings.enable_challan=False; self.settings.save()
        update_document(doc, {'line_items':[self.line(7)]})
        self.assertFalse(StockTransaction.objects.filter(document=doc).exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, 0)

    def test_orders_remain_non_posting_when_edited(self):
        for kind in ('quotation','po','pi'):
            doc = self.create(kind)
            update_document(doc, {'line_items':[self.line(7)]})
            self.assertFalse(doc.transactions.exists())
            self.assertFalse(doc.stock_transactions.exists())

    def test_duplicate_products_are_aggregated_and_movement_is_capped(self):
        self.settings.auto_stock=False; self.settings.save()
        doc = self.create(line_items=[self.line(2),self.line(3)])
        update_document(doc, {'line_items':[self.line(3),self.line(4)]})
        result = process_move_stock(doc, {'items':[{'product_id':self.product.pk,'quantity':5},
            {'product_id':self.product.pk,'quantity':5}]})
        self.assertEqual(Decimal(result['moved'][0]['quantity']), 7)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, -7)

    def test_fast_details_keep_posted_amount_and_follow_auto_stock(self):
        doc = self.create(line_items=[],total_amount=100)
        result = update_document(doc, {'line_items':[self.line(5)]}, preserve_total=True)
        self.assertEqual(result.total_amount, 100)
        self.assertEqual(result.transactions.get(type='record').amount, -100)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, -5)

    def test_expense_edit_updates_account_and_stays_out_of_contact_ledger(self):
        doc = self.create('expense',payment_account=self.account.pk)
        result = update_document(doc, {'line_items':[{'name':'Expense','amount':70}]})
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance, 930)
        self.assertEqual(result.transactions.get().monthly_cumulative_delta, 0)

    def test_interest_edit_keeps_discount_and_receive_direction(self):
        result = process_standalone_interest(self.contact, {'date':'2026-01-01','toggle':'we_receive',
            'line_items':[{'name':'Charge','amount':100,'type':'charge'},{'name':'Discount','amount':20,'type':'discount'}]})
        from .models import Document
        doc = Document.objects.get(pk=result['interest_doc'])
        doc = update_document(doc, {'line_items':[{'name':'Charge','amount':100,'type':'charge'},
            {'name':'Discount','amount':30,'type':'discount'}]})
        self.assertEqual(doc.total_amount, 70)
        self.assertEqual(doc.transactions.get().amount, -70)

    def test_stale_edits_are_rejected(self):
        doc = self.create()
        stale = doc.updated_at.isoformat()
        update_document(doc, {'notes':'First edit'})
        with self.assertRaises(EditConflict):
            update_document(doc, {'notes':'Second edit','expected_updated_at':stale})

    def test_contact_and_date_change_recalculates_old_and_new_months(self):
        doc = self.create()
        later = self.create(date='2026-01-20')
        other = Contact.objects.create(contact_name='Other',phone='456')
        doc = update_document(doc, {'contact':other.pk,'date':'2026-02-01'})
        self.assertEqual(later.transactions.get(type='record').monthly_cumulative_delta, -50)
        self.assertEqual(doc.transactions.get(type='record').monthly_cumulative_delta, -50)
