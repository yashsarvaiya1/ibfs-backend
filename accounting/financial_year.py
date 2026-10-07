"""Financial-year business activity from document and cash dates, not GST returns."""
from datetime import date
from decimal import Decimal
from .models import Document, FinancialTransaction
from .calculations import document_totals, money

MEASURES = ('sales', 'sales_returns', 'purchases', 'purchase_returns', 'expenses', 'other_income', 'cash_received', 'cash_paid')
DOC_MEASURES = {'invoice': 'sales', 'cn': 'sales_returns', 'bill': 'purchases', 'dn': 'purchase_returns', 'expense': 'expenses', 'income': 'other_income'}


def financial_year_report(year):
    start, end = date(year, 4, 1), date(year + 1, 3, 31)
    months = {}
    for offset in range(12):
        y, m = divmod(year * 12 + 3 + offset, 12)
        months[f'{y}-{m + 1:02d}'] = {key: Decimal('0') for key in MEASURES}
    counts = {key: 0 for key in DOC_MEASURES.values()}
    base = {key: Decimal('0') for key in DOC_MEASURES.values()}
    missing = []
    for doc in Document.objects.filter(is_active=True, date__range=(start, end), type__in=DOC_MEASURES).iterator():
        key = DOC_MEASURES[doc.type]
        counts[key] += 1
        months[doc.date.strftime('%Y-%m')][key] += doc.total_amount or Decimal('0')
        if not doc.line_items:
            missing.append({'id': doc.pk, 'doc_id': doc.doc_id, 'reason': 'Amount-only document: pre-tax value is unavailable.'})
            continue
        try:
            totals = document_totals({field: getattr(doc, field) for field in ('line_items', 'charges', 'taxes', 'tax_mode', 'discount', 'discount_percentage')}, doc.type)
            if doc.total_amount is None or money(doc.total_amount) != totals['total']:
                raise ValueError('Saved total differs from item calculation.')
            base[key] += totals['taxable_amount']
        except (ValueError, TypeError, ArithmeticError) as exc:
            missing.append({'id': doc.pk, 'doc_id': doc.doc_id, 'reason': str(exc)})
    # Contra transfers are movements between owned accounts, not receipts/payments.
    for txn in FinancialTransaction.objects.filter(type='actual', payment_account__isnull=False, date__range=(start, end)).iterator():
        key = 'cash_received' if txn.amount >= 0 else 'cash_paid'
        months[txn.date.strftime('%Y-%m')][key] += abs(txn.amount)
    totals = {key: sum((month[key] for month in months.values()), Decimal('0')) for key in MEASURES}
    def text(values):
        return {key: str(money(value)) for key, value in values.items()}
    return {'fy': year, 'date_from': start.isoformat(), 'date_to': end.isoformat(),
            'basis': 'Active invoices less credit notes, bills less debit notes, expense documents and separately recorded other income dated in Apr–Mar. Document values include tax. Other income is the receipt amount entered, not a calculated investment profit. Cash follows actual bank/cash/UPI movement dates and excludes internal transfers. Orders, quotations and challans are excluded. This activity summary does not calculate profit, cost of goods sold or eligible ITC.',
            'totals': text({**totals, 'net_sales': totals['sales'] - totals['sales_returns'], 'net_purchases': totals['purchases'] - totals['purchase_returns']}),
            'known_pre_tax': text({**base, 'net_sales': base['sales'] - base['sales_returns'], 'net_purchases': base['purchases'] - base['purchase_returns']}),
            'document_counts': counts, 'review': missing,
            'months': [{'month': month, **text({**values, 'net_sales': values['sales'] - values['sales_returns'], 'net_purchases': values['purchases'] - values['purchase_returns']})} for month, values in months.items()]}


def financial_year_export(year, format):
    from .report_exports import csv_response
    report = financial_year_report(year)
    labels = {'net_sales': 'Sales less credit notes (including tax)', 'net_purchases': 'Purchases less debit notes (including tax)', 'expenses': 'Expense documents (including tax)', 'other_income': 'Other income receipts', 'cash_received': 'Cash received', 'cash_paid': 'Cash paid'}
    if format == 'csv':
        rows = [['Financial year', f'{year}-{year + 1}'], ['Basis', report['basis']], ['Metric', 'Amount']]
        rows += [[label, Decimal(report['totals'][key])] for key, label in labels.items()]
        rows += [['Month', *labels.values()]]
        rows += [[month['month'], *(Decimal(month[key]) for key in labels)] for month in report['months']]
        rows += [['Pre-tax review document', 'Reason'], *[[row['doc_id'], row['reason']] for row in report['review']]]
        return csv_response(rows, f'FY_Business_{year}_{year + 1}.csv')
    from io import BytesIO
    from django.http import FileResponse
    from django.template.loader import render_to_string
    from shared.models import Settings
    from .services import _render_playwright_pdf
    html = render_to_string('accounting/financial_year_report.html', {'report': report, 'profile': Settings.get(), 'summary': [{'label': label, 'value': report['totals'][key]} for key, label in labels.items()]})
    return FileResponse(BytesIO(_render_playwright_pdf(html)), as_attachment=True, content_type='application/pdf', filename=f'FY_Business_{year}_{year + 1}.pdf')
