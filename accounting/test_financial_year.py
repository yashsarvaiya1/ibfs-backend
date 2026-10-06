from datetime import date
from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from .models import Document, FinancialTransaction
from .financial_year import financial_year_report
from shared.models import PaymentAccount


class FinancialYearTests(TestCase):
    def doc(self, kind, amount, date='2026-04-01', **extra):
        return Document.objects.create(type=kind, doc_id=f'FY-{Document.objects.count()}', total_amount=amount, date=date, **extra)

    def test_fy_business_activity_excludes_orders_stock_and_internal_transfers(self):
        self.doc('invoice', 118, line_items=[{'name': 'A', 'amount': 100}], taxes=[{'name': 'IGST', 'percentage': 18}])
        self.doc('cn', 10); self.doc('bill', 70); self.doc('dn', 5); self.doc('expense', 8)
        self.doc('invoice', 900, '2026-03-31');self.doc('invoice', 1000, '2027-04-01')
        self.doc('quotation', 2000);self.doc('challan', 0);self.doc('invoice', 3000, is_active=False)
        account = PaymentAccount.objects.create(name='Bank')
        FinancialTransaction.objects.create(type='actual', amount=50, payment_account=account, date='2026-05-01')
        FinancialTransaction.objects.create(type='actual', amount=-20, payment_account=account, date='2026-05-01')
        FinancialTransaction.objects.create(type='contra', amount=1000, payment_account=account, date='2026-05-01')
        FinancialTransaction.objects.create(type='record', amount=-900, date='2026-05-01')
        report = financial_year_report(2026)
        self.assertEqual(report['date_from'], '2026-04-01');self.assertEqual(report['date_to'], '2027-03-31')
        self.assertEqual(report['totals']['net_sales'], '108.00');self.assertEqual(report['totals']['net_purchases'], '65.00')
        self.assertEqual(report['totals']['expenses'], '8.00');self.assertEqual(report['totals']['cash_received'], '50.00')
        self.assertEqual(report['totals']['cash_paid'], '20.00');self.assertEqual(len(report['months']), 12)
        self.assertEqual(report['months'][-1]['month'], '2027-03')
        self.assertEqual(report['known_pre_tax']['sales'], '100.00')
        self.assertEqual(len(report['review']), 4)

    def test_fy_api_csv_and_empty_year(self):
        client = APIClient(); client.force_authenticate(get_user_model().objects.create_user('fy-review'))
        self.doc('invoice', 20)
        response = client.get('/api/reports/financial_year/', {'fy': 2026})
        self.assertEqual(response.status_code, 200);self.assertEqual(response.data['totals']['net_sales'], '20.00')
        csv = client.get('/api/reports/financial_year_export/', {'fy': 2026, 'export_format': 'csv'})
        self.assertEqual(csv.status_code, 200); self.assertIn(b'Sales less credit notes', b''.join(csv.streaming_content))
        self.assertEqual(financial_year_report(2024)['totals']['net_sales'], '0.00')
        self.assertEqual(client.get('/api/reports/financial_year/', {'fy': 10000}).status_code, 400)
