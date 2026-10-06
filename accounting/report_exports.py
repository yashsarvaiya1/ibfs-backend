"""Local CA review exports; no portal submissions or inferred ITC."""
import csv
from decimal import Decimal
from io import BytesIO
from django.http import FileResponse, StreamingHttpResponse
from django.template.loader import render_to_string
from django.utils import timezone
from shared.models import Settings
from .calculations import decimal_value, document_totals, money
from .models import FinancialTransaction
from .reports import COMPONENTS, MEASURES, gst_documents, gst_document_row, gst_report, tax_component


BUCKET_LABELS = {'output': 'Sales', 'purchase': 'Purchases', 'rcm_output': 'RCM sales', 'rcm_purchase': 'RCM purchases', 'review': 'Excluded / review'}


def spreadsheet_cell(value):
    if value is None:
        return ''
    # Numeric book values are Decimal, never user-authored strings. Preserve text
    # identifiers (including leading zeros) and neutralise spreadsheet formulas.
    if isinstance(value, str):
        if value[:1] in ('=', '+', '-', '@', '\t', '\r', '\n') or value.lstrip()[:1] in ('=', '+', '-', '@'):
            return "'" + value
    return str(value)


class Echo:
    def write(self, value): return value


def csv_response(rows, filename):
    writer = csv.writer(Echo())
    def stream():
        yield '\ufeff'
        for row in rows:
            yield writer.writerow([spreadsheet_cell(value) for value in row])
    response = StreamingHttpResponse(stream(), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def gst_csv_rows(start, end, review_only=False):
    yield ['Date', 'Document type', 'Document number', 'Supplier invoice number', 'Party', 'GSTIN', 'Reference', 'Place of supply', 'Reverse charge', 'Supply classification', 'Bucket', 'Adjustment sign', 'Document total', 'Taxable value', 'CGST', 'SGST', 'IGST', 'UTGST', 'Cess', 'GST without split', 'Other tax', 'GST total', 'Review notes']
    for doc in gst_documents(start, end).iterator(chunk_size=1000):
        row = gst_document_row(doc)
        if review_only and not row['issues']:
            continue
        yield [row['date'], row['type'], row['doc_id'], row['supplier_invoice_number'], row['contact'], row['gstin'], row['reference'], row['place_of_supply'], '' if row['reverse_charge'] is None else ('Yes' if row['reverse_charge'] else 'No'), row['supply_category'], BUCKET_LABELS[row['bucket']], row['adjustment_sign'], Decimal(row['total_amount']) if row['total_amount'] is not None else None, Decimal(row['taxable_amount']) if row['taxable_amount'] is not None else None, *[Decimal(row['amounts'][key]) for key in (*COMPONENTS, 'gst_total')], ' | '.join(row['issues'])]


def hsn_report(start, end):
    groups = {}
    excluded = []
    for doc in gst_documents(start, end).iterator(chunk_size=1000):
        row = gst_document_row(doc)
        if row['bucket'] == 'review':
            excluded.append({'id': doc.pk, 'doc_id': doc.doc_id, 'issues': row['issues']})
            continue
        totals = document_totals({key: getattr(doc, key) for key in ('line_items', 'charges', 'taxes', 'discount', 'tax_mode')}, doc.type)
        details = totals['line_details']
        if len(details) != len(doc.line_items):
            excluded.append({'id': doc.pk, 'doc_id': doc.doc_id, 'issues': ['Item amounts cannot allocate the document taxable value.']})
            continue
        for item, detail in zip(doc.line_items, details):
            hsn, unit = str(item.get('hsn') or '').strip(), str(item.get('unit') or '').strip()
            rate = sum((tax['percentage'] for tax in detail['taxes'] if tax_component(tax['name']) in ('cgst', 'sgst', 'igst', 'utgst', 'unsplit_gst')), Decimal('0'))
            category = item.get('supply_category') or doc.supply_category
            key = (row['bucket'], hsn, unit, str(rate), category or '')
            if key not in groups:
                groups[key] = {'bucket': row['bucket'], 'hsn': hsn, 'unit': unit, 'rate': str(rate), 'supply_category': category, 'quantity': Decimal('0'), 'quantity_complete': True, 'taxable_amount': Decimal('0'), **{name: Decimal('0') for name in COMPONENTS}, 'document_ids': set(), 'issues': set()}
            group = groups[key]
            sign = row['adjustment_sign']
            if item.get('quantity') is None:
                group['quantity_complete'] = False
                group['issues'].add('Quantity unavailable for one or more lines.')
            else:
                try:
                    group['quantity'] += decimal_value(item['quantity']) * sign
                except (ValueError, TypeError, ArithmeticError):
                    group['quantity_complete'] = False
                    group['issues'].add('Saved quantity cannot be calculated.')
            group['taxable_amount'] += detail['taxable_amount'] * sign
            for tax in detail['taxes']:
                group[tax_component(tax['name'])] += tax['amount'] * sign
            group['document_ids'].add(doc.pk)
            group['issues'].update(row['issues'])
            if not hsn: group['issues'].add('HSN/SAC missing; no code inferred.')
            elif not hsn.isdigit() or len(hsn) not in (4, 6, 8): group['issues'].add('Verify the saved HSN/SAC format.')
            if not unit: group['issues'].add('Unit missing; no UQC inferred.')
            if not category: group['issues'].add('Supply classification unspecified.')
    rows = []
    for _, group in sorted(groups.items()):
        group['quantity'] = str(group['quantity']) if group.pop('quantity_complete') else None
        group['document_count'] = len(group.pop('document_ids'))
        group['issues'] = sorted(group['issues'])
        group['gst_total'] = sum((group[name] for name in COMPONENTS if name != 'other_tax'), Decimal('0'))
        for key in MEASURES:
            group[key] = str(money(group[key]))
        rows.append(group)
    return {'date_from': start.isoformat(), 'date_to': end.isoformat(), 'basis': 'Saved HSN/SAC, unit and classification. Shared-rate document GST, charges and discounts are allocated proportionally in cents; per-item mode uses the saved item rates. CN reduces sales; DN reduces purchases. No GST portal or eligible-ITC calculation.', 'results': rows, 'excluded': excluded}


def hsn_csv_rows(report):
    yield ['Bucket', 'HSN/SAC', 'Saved unit', 'GST rate %', 'Supply classification', 'Quantity', 'Taxable value', 'CGST', 'SGST', 'IGST', 'UTGST', 'Cess', 'GST without split', 'Other tax', 'GST total', 'Documents', 'Review notes']
    for row in report['results']:
        yield [BUCKET_LABELS[row['bucket']], row['hsn'], row['unit'], row['rate'], row['supply_category'], Decimal(row['quantity']) if row['quantity'] is not None else None, *[Decimal(row[key]) for key in MEASURES], row['document_count'], ' | '.join(row['issues'])]
    for row in report['excluded']:
        yield [BUCKET_LABELS['review'], '', '', '', '', '', *[''] * len(MEASURES), '', f"{row['doc_id']}: " + ' | '.join(row['issues'])]


def report_pdf(start, end, *, kind='gst', review_only=False):
    from .services import _render_playwright_pdf
    if kind == 'hsn':
        report = hsn_report(start, end)
    else:
        count = gst_documents(start, end).count()
        report = gst_report(start, end, page_size=max(count, 1), review_only=review_only)
    report['generated_at'] = timezone.localtime().strftime('%d %b %Y %H:%M %Z')
    report['bucket_rows'] = [{'label': BUCKET_LABELS[bucket], **values} for bucket, values in report.get('totals', {}).items()]
    for row in report['results']:
        row['bucket_label'] = BUCKET_LABELS[row['bucket']]
    html = render_to_string('accounting/report_print.html', {'report': report, 'business': Settings.get(), 'kind': kind, 'review_only': review_only})
    return FileResponse(BytesIO(_render_playwright_pdf(html)), as_attachment=True, content_type='application/pdf', filename=f'{kind.upper()}_Book_Report_{start}_{end}.pdf')


def allocation_review(start, end, page, size):
    # Expenses/contra transfers have no bill/invoice settlement purpose.
    payments = (FinancialTransaction.objects.filter(type='actual', date__range=(start, end))
                .exclude(document__type='expense').select_related('document', 'contact', 'payment_account')
                .prefetch_related('allocations__document').order_by('date', 'pk'))
    rows, count = [], 0
    for payment in payments.iterator(chunk_size=500):
        allocations = list(payment.allocations.all())
        principal = sum((abs(row.amount) for row in allocations if row.document.type != 'interest'), Decimal('0'))
        adjustments = sum((row.amount for row in allocations if row.document.type == 'interest'), Decimal('0'))
        available = money(abs(payment.amount) - principal - adjustments)
        if available == 0:
            continue
        count += 1
        if (page - 1) * size < count <= page * size:
            rows.append({'id': payment.pk, 'date': payment.date.isoformat(), 'contact': str(payment.contact) if payment.contact else '', 'account': str(payment.payment_account) if payment.payment_account else '', 'amount': str(payment.amount), 'unallocated': str(available), 'document_id': payment.document_id, 'doc_id': payment.document.doc_id if payment.document else None, 'issue': 'Allocation exceeds the saved cash budget; review source data.' if available < 0 else 'Cash remains unallocated. Review the contact and document before assigning it.'})
    return {'count': count, 'page_size': size, 'results': rows}
