from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from shared.models import Settings, Contact, PaymentAccount
from .models import Document, FinancialTransaction
from .services import process_document_create, process_send_receive, process_document_delete
from .payments import payment_status


class PaymentFlowTests(TestCase):
    def setUp(self):
        self.settings = Settings.get()
        self.contact = Contact.objects.create(contact_name='Customer',phone='123')
        self.account = PaymentAccount.objects.create(name='Cash',type='cash',current_balance=1000)
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user('tester'))
        self.doc = process_document_create('invoice',{'date':'2026-01-01','total_amount':100},self.contact)

    def pay(self, amount, lines=None):
        response = self.client.post(f'/api/documents/{self.doc.pk}/record_payment/',
            {'amount':amount,'payment_account':self.account.pk,'date':'2026-01-02',
             'interest_lines':lines or []}, format='json')
        self.assertEqual(response.status_code,201,response.data)
        return response.data

    def test_charge_is_separate_from_principal(self):
        self.pay(100,[{'name':'Charge','amount':20,'type':'charge'}])
        status = payment_status(self.doc)
        self.assertEqual(float(status['paid']),80)
        self.assertEqual(float(status['remaining']),20)
        self.assertFalse(status['is_paid'])

    def test_discount_settles_the_remainder_without_extra_cash(self):
        self.pay(80,[{'name':'Discount','amount':20,'type':'discount'}])
        self.assertTrue(payment_status(self.doc)['is_paid'])
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance,1080)

    def test_cash_voucher_stays_printable_and_settles_invoice(self):
        self.settings.enable_vouchers=True; self.settings.save()
        result = process_send_receive(self.contact,{'amount':80,'payment_account':self.account.pk,
            'document':self.doc.pk,'date':'2026-01-02'},'receive')
        payment = FinancialTransaction.objects.get(pk=result['ftxn'])
        self.assertEqual(payment.document.type,'cash_receipt_voucher')
        self.assertEqual(payment.document.reference_id,self.doc.pk)
        self.assertEqual(float(payment_status(self.doc)['remaining']),20)
        response = self.client.get(f'/api/documents/{self.doc.pk}/')
        self.assertIn(payment.pk,[row['id'] for row in response.data['transactions']])

    def test_payment_edits_and_deletion_refresh_settlement(self):
        result = self.pay(100)
        self.assertTrue(payment_status(self.doc)['is_paid'])
        response = self.client.patch(f"/api/transactions/{result['ftxn']}/",{'amount':40},format='json')
        self.assertEqual(response.status_code,200)
        self.assertEqual(float(payment_status(self.doc)['remaining']),60)
        self.client.delete(f"/api/transactions/{result['ftxn']}/")
        self.assertEqual(float(payment_status(self.doc)['remaining']),100)

    def test_payment_can_be_split_across_two_invoices(self):
        other = process_document_create('invoice',{'date':'2026-01-01','total_amount':100},self.contact)
        result = self.pay(100)
        response = self.client.post(f"/api/transactions/{result['ftxn']}/allocate/",{'allocations':[
            {'document':self.doc.pk,'amount':40},{'document':other.pk,'amount':60}]},format='json')
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(float(payment_status(self.doc)['remaining']),60)
        self.assertEqual(float(payment_status(other)['remaining']),40)

    def test_partial_filter_works_before_pagination(self):
        self.pay(40)
        response = self.client.get('/api/documents/?payment_status=partial&page_size=1')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data['count'],1)
        self.assertEqual(response.data['results'][0]['id'],self.doc.pk)

    def test_nonpositive_payments_are_rejected(self):
        for amount in (-20,0,'NaN'):
            response = self.client.post(f'/api/documents/{self.doc.pk}/record_payment/',
                {'amount':amount,'payment_account':self.account.pk},format='json')
            self.assertEqual(response.status_code,400)
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance,1000)

    def test_invoice_reversal_also_reverses_its_cash_voucher(self):
        self.settings.enable_vouchers=True; self.settings.save()
        result = process_send_receive(self.contact,{'amount':100,'payment_account':self.account.pk,
            'document':self.doc.pk,'date':'2026-01-02'},'receive')
        voucher = FinancialTransaction.objects.get(pk=result['ftxn']).document
        process_document_delete(self.doc,'revert')
        self.account.refresh_from_db(); voucher.refresh_from_db()
        self.assertEqual(self.account.current_balance,1000)
        self.assertFalse(voucher.is_active)

    def test_relink_keeps_discount_and_cash_balance(self):
        result = self.pay(80,[{'name':'Discount','amount':20,'type':'discount'}])
        other = process_document_create('invoice',{'date':'2026-01-01','total_amount':100},self.contact)
        response = self.client.post(f"/api/transactions/{result['ftxn']}/link_document/",
            {'document':other.pk},format='json')
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(float(payment_status(self.doc)['paid']),0)
        self.assertEqual(float(payment_status(other)['paid']),100)
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance,1080)

    def test_reversal_rejects_payment_shared_with_another_invoice(self):
        result = self.pay(100)
        other = process_document_create('invoice',{'date':'2026-01-01','total_amount':100},self.contact)
        self.client.post(f"/api/transactions/{result['ftxn']}/allocate/",{'allocations':[
            {'document':self.doc.pk,'amount':40},{'document':other.pk,'amount':60}]},format='json')
        response = self.client.delete(f'/api/documents/{self.doc.pk}/?strategy=revert')
        self.assertEqual(response.status_code,400,response.data)
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance,1100)

    def test_charge_edit_updates_principal_without_moving_cash(self):
        result=self.pay(100,[{'name':'Charge','amount':20,'type':'charge'}])
        response=self.client.patch(f"/api/documents/{result['interest_doc']}/",{'line_items':[{'name':'Charge','amount':30,'type':'charge'}]},format='json')
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(float(payment_status(self.doc)['paid']),70)
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance,1100)

    def test_voucher_edit_updates_cash_and_invoice_allocation(self):
        self.settings.enable_vouchers=True;self.settings.save()
        result=process_send_receive(self.contact,{'amount':100,'payment_account':self.account.pk,'document':self.doc.pk,'date':'2026-01-02'},'receive')
        payment=FinancialTransaction.objects.get(pk=result['ftxn'])
        response=self.client.patch(f'/api/documents/{payment.document_id}/',{'total_amount':60},format='json')
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(float(payment_status(self.doc)['paid']),60)
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance,1060)

    def test_contact_correction_updates_sole_linked_voucher(self):
        self.settings.enable_vouchers=True;self.settings.save()
        result=process_send_receive(self.contact,{'amount':100,'payment_account':self.account.pk,'document':self.doc.pk,'date':'2026-01-02'},'receive')
        other=Contact.objects.create(contact_name='Correct customer',phone='456')
        response=self.client.patch(f'/api/documents/{self.doc.pk}/',{'contact':other.pk},format='json')
        self.assertEqual(response.status_code,200,response.data)
        payment=FinancialTransaction.objects.get(pk=result['ftxn'])
        self.assertEqual(payment.contact_id,other.pk)
        self.assertEqual(payment.document.contact_id,other.pk)

    def test_discount_cannot_make_invoice_total_negative(self):
        response=self.client.post('/api/documents/',{'type':'invoice','contact':self.contact.pk,'date':'2026-01-01',
            'line_items':[{'name':'Item','quantity':1,'rate':10,'amount':10}],'discount':20},format='json')
        self.assertEqual(response.status_code,400,response.data)
