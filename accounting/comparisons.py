"""Read-only comparisons of user-provided CSVs; never import cash or claim ITC."""
import csv
from collections import Counter, defaultdict
from datetime import date
from io import StringIO
from decimal import Decimal
from rest_framework import serializers
from .calculations import decimal_value, money
from .models import Document, FinancialTransaction
from .reports import gst_document_row

BANK_COLUMNS = ('date', 'amount', 'reference')
PURCHASE_COLUMNS = ('supplier_gstin', 'supplier_invoice_number', 'date', 'taxable_amount', 'cgst', 'sgst_utgst', 'igst', 'cess')


def read_csv(upload, columns):
    if not upload or upload.size > 5 * 1024 * 1024:
        raise serializers.ValidationError({'file': 'Choose a UTF-8 CSV up to 5 MB.'})
    try:
        content = upload.read(5 * 1024 * 1024 + 1).decode('utf-8-sig')
        reader = csv.DictReader(StringIO(content), strict=True)
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)) or not set(columns).issubset(reader.fieldnames):
            raise serializers.ValidationError({'file': 'Use the template headers: ' + ', '.join(columns)})
        rows = []
        for number, row in enumerate(reader, 2):
            if number > 5001:
                raise serializers.ValidationError({'file': 'Choose at most 5,000 rows at a time.'})
            if None in row or any(row.get(column) is None for column in columns):
                raise serializers.ValidationError({'file': f'Row {number} has inconsistent CSV columns.'})
            rows.append({key: str(value or '').strip() for key, value in row.items()})
        return rows
    except (UnicodeError, csv.Error) as exc:
        raise serializers.ValidationError({'file': 'Choose a well-formed UTF-8 CSV using the template.'}) from exc


def csv_amount(value):
    if not value or len(value) > 32:
        raise ValueError('Enter an explicit amount.')
    number = decimal_value(value)
    if abs(number) > Decimal('9999999999999.99') or money(number) != number:
        raise ValueError('Use at most two decimal places and the supported amount range.')
    return money(number)


def csv_date(value):
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError('Use ISO date YYYY-MM-DD.')
    return parsed


def bank_comparison(rows, account, start, end):
    candidates = defaultdict(list)
    for payment in FinancialTransaction.objects.filter(payment_account=account, type__in=('actual', 'contra'), date__range=(start, end)).only('id', 'date', 'amount').order_by('date', 'pk').iterator(chunk_size=1000):
        # Original reversed transfers have their matching inverse legs in the ledger.
        candidates[(payment.date.isoformat(), money(payment.amount))].append(payment)
    used = set()
    keys = []
    for row in rows:
        try:
            keys.append((csv_date(row['date']).isoformat(), csv_amount(row['amount'])))
        except (ValueError, TypeError, ArithmeticError): keys.append(None)
    duplicates = Counter(key for key in keys if key is not None)
    results = []
    for index, (row, key) in enumerate(zip(rows, keys), 2):
        matches = candidates.get(key, [])
        if key is None:
            status, detail = 'invalid', 'Use ISO date YYYY-MM-DD and a finite signed amount.'
        elif not start <= date.fromisoformat(key[0]) <= end:
            status, detail = 'outside_period', 'Statement date lies outside the selected book period.'
        elif duplicates[key] > 1 or len(matches) > 1:
            status, detail = 'ambiguous', 'Several statement rows or book entries have this date/amount. Review references manually.'
        elif not matches:
            status, detail = 'unmatched', 'No cash entry has this date and signed amount in the selected account.'
        else:
            status, detail = 'candidate', 'Date and signed amount agree. Verify the statement reference before reconciling.'
            used.add(matches[0].pk)
        results.append({'row': index, 'date': row['date'], 'number': row['reference'], 'status': status, 'detail': detail, 'amount': row['amount'], 'matches': [{'id': txn.pk, 'kind': 'transaction', 'label': f'Payment #{txn.pk}'} for txn in matches]})
    unmatched_books = [{'id': txn.pk, 'kind': 'transaction', 'label': f'Payment #{txn.pk}', 'date': txn.date.isoformat(), 'amount': str(txn.amount)} for values in candidates.values() for txn in values if txn.pk not in used]
    return {'basis': 'Read-only suggestions by selected account, book date and signed cash amount. Incoming is positive; outgoing is negative. References require your review. No payment is created or changed.', 'results': results, 'unmatched_books': unmatched_books}


def purchase_comparison(rows, start, end):
    # Match the supplier's original invoice number, never infer it from IBFS's ID.
    books = defaultdict(list)
    query = Document.objects.filter(type='bill', is_active=True, date__range=(start, end)).order_by('date', 'pk')
    for row in query.values('id', 'doc_id', 'date', 'total_amount', 'supplier_invoice_number', 'contact__gstin').iterator(chunk_size=1000):
        if row['contact__gstin'] and row['supplier_invoice_number']:
            key = (row['contact__gstin'].strip().upper(), row['supplier_invoice_number'].strip().casefold(), row['date'].isoformat())
        else:
            key = ('', f"ibfs:{row['id']}", row['date'].isoformat())
        books[key].append(row)
    keys = [(row['supplier_gstin'].upper(), row['supplier_invoice_number'].casefold(), row['date']) for row in rows]
    duplicates = Counter(keys)
    candidate_ids = {doc['id'] for key in keys for doc in books.get(key, [])}
    details = Document.objects.filter(pk__in=candidate_ids).select_related('contact', 'reference').in_bulk()
    used = set(); results = []
    for index, (row, key) in enumerate(zip(rows, keys), 2):
        matches = books.get(key, [])
        try:
            day = csv_date(row['date'])
            if not key[0] or not key[1]: raise ValueError()
            imported = {field: csv_amount(row[field]) for field in PURCHASE_COLUMNS[3:]}
            if any(value < 0 for value in imported.values()): raise ValueError()
        except (ValueError, TypeError, ArithmeticError):
            status, detail = 'invalid', 'Invoice rows need supplier GSTIN/number, ISO date and nonnegative taxable/component amounts. Notes and amendments require separate CA review.'
        else:
            if not start <= day <= end:
                status, detail = 'outside_period', 'Invoice date is outside the selected book period; portal reporting periods can differ.'
            elif duplicates[key] > 1 or len(matches) > 1:
                status, detail = 'ambiguous', 'Duplicate supplier/GSTIN/invoice/date combination; review the source rows.'
            elif not matches:
                status, detail = 'unmatched', 'No bill with this supplier GSTIN, supplier invoice number and date. Check missing source particulars.'
            else:
                doc = details[matches[0]['id']]; book = gst_document_row(doc); amounts = book['amounts']; used.add(doc.pk)
                expected = {'taxable_amount': Decimal(amounts['taxable_amount']), 'cgst': Decimal(amounts['cgst']), 'sgst_utgst': Decimal(amounts['sgst']) + Decimal(amounts['utgst']), 'igst': Decimal(amounts['igst']), 'cess': Decimal(amounts['cess'])}
                differences = [field for field in expected if expected[field] != imported[field]]
                if book['bucket'] == 'review' or amounts['unsplit_gst'] != '0.00' or amounts['other_tax'] != '0.00':
                    status, detail = 'review', 'Book tax calculation needs review: ' + ' | '.join(book['issues'])
                elif differences:
                    status, detail = 'mismatch', 'Values differ: ' + ', '.join(differences)
                else:
                    status, detail = 'values_agree', 'Saved book values agree. This does not establish ITC eligibility or RCM payment.'
        results.append({'row': index, 'date': row['date'], 'number': row['supplier_invoice_number'], 'status': status, 'detail': detail, 'amount': row['taxable_amount'], 'matches': [{'id': doc['id'], 'kind': 'document', 'label': doc['doc_id']} for doc in matches]})
    unmatched_books = [{'id': doc['id'], 'kind': 'document', 'label': doc['doc_id'], 'date': doc['date'].isoformat(), 'amount': str(doc['total_amount']) if doc['total_amount'] is not None else ''} for docs in books.values() for doc in docs if doc['id'] not in used]
    return {'basis': 'Compare CA-prepared purchase invoice CSVs (for example from GSTR-2B) with saved bills. Invoice date and supplier number are used; no portal access, credit decision, notes/amendment matching or filing occurs.', 'results': results, 'unmatched_books': unmatched_books}
