# accounting/services.py
import re
from decimal import Decimal
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from datetime import date as date_type
from .models import Document, FinancialTransaction
from shared.models import PaymentAccount, Settings, Contact
from django.template.loader import render_to_string
from django.core.cache import cache
from django.conf import settings


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _build_media_url(request, relative_path):
    if not relative_path:
        return None
    if request:
        return request.build_absolute_uri(f"/media/{relative_path}")
    return f"{settings.MEDIA_BASE_URL.rstrip('/')}/media/{relative_path}"


def _contact_display(contact):
    if not contact:
        return None
    all_phones = contact.additional_contacts or []
    if contact.phone:
        all_phones = all_phones + [{'name': contact.contact_name, 'number': contact.phone, 'role': 'primary'}]
    return {
        'name':       contact.company_name or contact.contact_name,
        'phone':      contact.phone,
        'gstin':      contact.gstin,
        'address':    contact.address,
        'all_phones': all_phones,
    }


# ─── PDF Generation ───────────────────────────────────────────────────────────

def generate_document_pdf(document, request=None):
    """Single document PDF — cached for 10 min keyed on (pk, updated_at)."""
    cache_key = f"pdf_{document.pk}_{document.updated_at.timestamp()}"
    cached    = cache.get(cache_key)
    if cached:
        return cached

    app_settings = Settings.get()
    context      = _build_document_context(document, app_settings, request)
    html_string  = render_to_string('accounting/document_print.html', context)
    pdf_bytes    = _render_playwright_pdf(html_string)
    filename     = f"{document.type.upper()}_{document.doc_id}_{document.date}.pdf"
    result       = (pdf_bytes, filename)
    cache.set(cache_key, result, timeout=600)
    return result


def generate_bulk_documents_pdf(documents, request=None):
    """
    Merged PDF of multiple documents — one per page (DV-04).
    Each document's HTML is separated by a CSS page-break wrapper.
    """
    app_settings = Settings.get()
    html_parts   = []

    for doc in documents:
        context = _build_document_context(doc, app_settings, request, is_bulk=True)
        html_parts.append(render_to_string('accounting/document_print.html', context))

    # Wrap each part in a page-break container
    combined_html = '\n'.join(
        f'<div class="page-wrapper">{part}</div>' for part in html_parts
    )
    # Outer shell with global page-break CSS
    full_html = f"""
    <!DOCTYPE html><html><head><meta charset="UTF-8">
    <style>
      .page-wrapper {{ page-break-after: always; }}
      .page-wrapper:last-child {{ page-break-after: avoid; }}
    </style>
    </head><body>{combined_html}</body></html>
    """
    pdf_bytes = _render_playwright_pdf(full_html)
    filename  = f"Documents_Bulk_{timezone.now().date()}.pdf"
    return (pdf_bytes, filename)


def _build_document_context(document, app_settings, request=None, is_bulk=False):
    """Shared context builder for single and bulk document PDFs."""
    line_items   = document.line_items or []
    charges      = document.charges or []
    taxes        = document.taxes or []

    subtotal     = sum(Decimal(str(i.get('amount', 0))) for i in line_items)
    charges_sum  = sum(Decimal(str(c.get('amount', 0))) for c in charges)
    discount     = document.discount or Decimal('0')
    taxable_base = subtotal + charges_sum - discount
    tax_breakdown, tax_total = [], Decimal('0')

    for tax in taxes:
        pct = Decimal(str(tax.get('percentage', 0)))
        amt = taxable_base * pct / 100
        tax_total += amt
        tax_breakdown.append({
            'name':       tax.get('name', ''),
            'percentage': str(pct),
            'amount':     str(amt.quantize(Decimal('0.01'))),
        })

    grand_total = taxable_base + tax_total

    # BF-09: Pass explicit boolean flags instead of relying on template `in` substring check
    is_simple_line_type = document.type in {
        'interest', 'expense', 'cash_payment_voucher', 'cash_receipt_voucher'
    }
    is_vendor_doc = document.type in {'bill', 'po', 'dn'}

    return {
        'document':          document,
        'settings':          app_settings,
        'header_image':      _build_media_url(request, app_settings.header_image),
        'sign_image':        _build_media_url(request, app_settings.sign_image),
        'contact':           _contact_display(document.contact),
        'consignee':         _contact_display(document.consignee),
        'line_items':        line_items,
        'charges':           charges,
        'discount':          str(document.discount),
        'taxes':             tax_breakdown,
        'tax_total':         str(tax_total.quantize(Decimal('0.01'))),
        'grand_total':       str(grand_total.quantize(Decimal('0.01'))),
        'doc_type_label':    document.get_type_display(),
        'is_simple_line_type': is_simple_line_type,  # BF-09: no qty/rate/hsn cols
        'is_vendor_doc':     is_vendor_doc,           # BF-09: "Vendor" vs "Bill To"
        'is_bulk':           is_bulk,
    }

def compute_opening_balance_for_print(contact, date_from=None) -> Decimal:
    base = Decimal(str(contact.opening_balance or 0))
    if date_from is None:
        return base

    month_start = date_from.replace(day=1)

    # Cross-month: last MCD per month strictly before date_from's month
    txns_before = (
        FinancialTransaction.objects
        .filter(contact=contact, date__lt=month_start)
        .order_by('date', 'created_at')
        .values('date', 'monthly_cumulative_delta')
    )
    month_last_mcd: dict = {}
    for txn in txns_before:
        key = (txn['date'].year, txn['date'].month)
        month_last_mcd[key] = txn['monthly_cumulative_delta']

    cross_month_base = base + sum(month_last_mcd.values(), Decimal('0'))

    # Intra-month: CF-affecting txns in SAME month strictly before date_from
    # This is the missing piece — old code stopped here and returned cross_month_base
    same_month_before = (
        FinancialTransaction.objects
        .filter(contact=contact, date__gte=month_start, date__lt=date_from)
        .select_related('document')
        .order_by('date', 'created_at')
    )
    intra_month_sum = Decimal('0')
    for txn in same_month_before:
        is_expense = txn.document is not None and txn.document.type == 'expense'
        is_contra  = txn.type == 'contra'
        if not is_expense and not is_contra:
            intra_month_sum += txn.amount

    return cross_month_base + intra_month_sum
def generate_transactions_pdf(
    transactions,
    contact=None,
    request=None,
    opening_balance_at=None,
    is_ledger_view=False,
    report_title=None,
    account=None,
    balance_before_period=None,
):
    app_settings = Settings.get()

    if opening_balance_at is not None:
        running_cf = Decimal(str(opening_balance_at))
    elif contact:
        running_cf = Decimal(str(contact.opening_balance or 0))
    else:
        running_cf = None

    rows = []
    for txn in transactions:
        # Mirror frontend ContactLedger logic exactly
        is_expense = txn.document is not None and txn.document.type == 'expense'
        is_contra  = txn.type == 'contra'
        affects_cf = not is_expense and not is_contra

        row = {
            'date':       txn.date,
            'type':       txn.get_type_display(),
            'type_raw':   txn.type,
            'doc_id':     txn.document.doc_id if txn.document else '—',
            'doc_type':   txn.document.get_type_display() if txn.document else '—',
            'notes':      txn.notes or '—',
            'amount':     str(txn.amount.quantize(Decimal('0.01'))),          # ← add back
            'amount_abs': str(abs(txn.amount).quantize(Decimal('0.01'))),
            'amount_pos': txn.amount >= 0,
            'account':    txn.payment_account.name if txn.payment_account else '—',
            'is_expense': is_expense,
            'is_contra':  is_contra,
        }

        if is_ledger_view and running_cf is not None:
            # Only advance running_cf for CF-affecting txns (matches frontend)
            if affects_cf:
                running_cf += txn.amount
            # Always emit balance (expense rows show unchanged balance, same as frontend)
            row['running_cf']          = str(abs(running_cf).quantize(Decimal('0.01')))
            row['running_cf_positive'] = running_cf > 0
            row['running_cf_zero']     = running_cf == 0

        rows.append(row)

    # Opening balance — absolute value + direction flags, no string slicing in template
    ob_val  = None
    ob_pos  = False
    ob_zero = False
    if opening_balance_at is not None:
        ob_dec  = Decimal(str(opening_balance_at))
        ob_val  = str(abs(ob_dec).quantize(Decimal('0.01')))
        ob_pos  = ob_dec > 0
        ob_zero = ob_dec == 0

    if not report_title:
        if contact:
            report_title = f"Ledger — {contact.company_name or contact.contact_name}"
        elif account:
            report_title = f"Account Statement — {account.name}"
        else:
            report_title = "Financial Transactions"

    context = {
        'settings':              app_settings,
        'header_image':          _build_media_url(request, app_settings.header_image),
        'contact':               contact,
        'account':               account,
        'transactions':          rows,
        'is_ledger':             is_ledger_view,
        'report_title':          report_title,
        # Clean flags — no raw signed strings sent to template
        'opening_balance_val':   ob_val,
        'opening_balance_pos':   ob_pos,
        'opening_balance_zero':  ob_zero,
        'balance_before_period': (
            str(Decimal(str(balance_before_period)).quantize(Decimal('0.01')))
            if balance_before_period is not None else None
        ),
    }

    html_string = render_to_string('accounting/transactions_print.html', context)
    pdf_bytes   = _render_playwright_pdf(html_string)

    if contact:
        safe_name = (contact.company_name or contact.contact_name).replace(' ', '_')
        filename  = f"Ledger_{safe_name}_{timezone.now().date()}.pdf"
    elif account:
        safe_name = account.name.replace(' ', '_')
        filename  = f"Statement_{safe_name}_{timezone.now().date()}.pdf"
    else:
        filename = f"Transactions_{timezone.now().date()}.pdf"

    return (pdf_bytes, filename)



def generate_stock_transactions_pdf(stock_txns, request=None, report_title=None):
    """
    Stock transactions report PDF (ST-01).
    Accepts any filtered queryset — respects all active filters.
    """
    app_settings = Settings.get()

    rows = []
    for stxn in stock_txns:
        rows.append({
            'date':     stxn.date,
            'type':     stxn.get_type_display(),
            'product':  stxn.product.name if stxn.product else '—',
            'quantity': str(stxn.quantity),
            'doc_id':   stxn.document.doc_id if stxn.document else '—',
            'doc_type': stxn.document.get_type_display() if stxn.document else '—',
            'rate':     str(stxn.rate) if stxn.rate else '—',
            'notes':    stxn.notes or '—',
        })

    context = {
        'settings':     app_settings,
        'header_image': _build_media_url(request, app_settings.header_image),
        'transactions': rows,
        'report_title': report_title or 'Stock Transactions',
        'printed_on':   timezone.now().date(),
        'total_rows':   len(rows),
    }

    html_string = render_to_string('inventory/stock_transactions_print.html', context)
    pdf_bytes   = _render_playwright_pdf(html_string)
    filename    = f"StockTransactions_{timezone.now().date()}.pdf"
    return (pdf_bytes, filename)


def _render_playwright_pdf(html_string):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser   = p.chromium.launch(headless=True)
        page      = browser.new_page()
        page.set_content(html_string, wait_until='networkidle')
        pdf_bytes = page.pdf(
            format=settings.PLAYWRIGHT_PDF_FORMAT,
            print_background=True,
            prefer_css_page_size=True,
        )
        browser.close()
    return pdf_bytes


# ─── Core Helpers ─────────────────────────────────────────────────────────────

def _parse_date(val):
    if not val:
        return timezone.localdate()
    if isinstance(val, str):
        return date_type.fromisoformat(val)
    return val


def _next_doc_id(doc_type):
    """
    BF-05 + DI-02:
      - select_for_update() prevents concurrent creates from getting the same number
      - Uses max numeric suffix + 1 instead of count + 1
        → handles gaps from soft-deleted docs correctly
        → user-editable doc_id preserves correct auto-increment
    Must be called inside a @transaction.atomic block (all callers are).
    """
    prefix_map = {
        'bill':                'BILL',
        'invoice':             'INV',
        'po':                  'PO',
        'pi':                  'PI',
        'quotation':           'QUO',
        'challan':             'CHL',
        'cn':                  'CN',
        'dn':                  'DN',
        'cash_payment_voucher':'CPV',
        'cash_receipt_voucher':'CRV',
        'interest':            'INT',
        'expense':             'EXP',
    }
    prefix = prefix_map.get(doc_type, 'DOC')

    # Lock all doc_id rows for this type to prevent concurrent duplicates (BF-05)
    existing_ids = (
        Document.objects
        .select_for_update()
        .filter(type=doc_type, doc_id__startswith=prefix)
        .values_list('doc_id', flat=True)
    )

    # Extract max numeric suffix (DI-02: handles deletion gaps correctly)
    max_num = 0
    for doc_id in existing_ids:
        match = re.search(r'(\d+)$', doc_id)
        if match:
            max_num = max(max_num, int(match.group(1)))

    return f"{prefix}-{max_num + 1:04d}"


# ─── MCD Recalculation ────────────────────────────────────────────────────────

def _recalculate_mcd(contact, date):
    """
    Recalculates MCD for all f.txns in the same month/year for a contact.
    Per spec Part 11:
      - expense f.txns  → MCD forced to 0 (never affect contact CF)
      - contra f.txns   → no contact, never reach here
      - all others      → running cumulative sum within the month
    """
    if not contact:
        return
    date = _parse_date(date)
    txns = (
        FinancialTransaction.objects
        .filter(contact=contact, date__year=date.year, date__month=date.month)
        .order_by('date', 'created_at')
        .select_related('document')
    )

    running = Decimal('0')
    for t in txns:
        is_expense = t.document is not None and t.document.type == 'expense'
        if is_expense:
            if t.monthly_cumulative_delta != Decimal('0'):
                t.monthly_cumulative_delta = Decimal('0')
                t.save(update_fields=['monthly_cumulative_delta'])
        else:
            running += t.amount
            if t.monthly_cumulative_delta != running:
                t.monthly_cumulative_delta = running
                t.save(update_fields=['monthly_cumulative_delta'])


# ─── Core f.txn / s.txn creators ─────────────────────────────────────────────

def _create_ftxn(
    type_, amount, contact=None, account=None,
    document=None, date=None, notes=None,
    force_mcd_zero=False,
):
    """
    Creates a FinancialTransaction and handles all side effects.

    force_mcd_zero=True → expense path:
      - MCD stays 0 (contact CF never affected)
      - PaymentAccount.current_balance still updated normally
      - _recalculate_mcd NOT called

    Normal path:
      - _recalculate_mcd called (sets correct MCD on this and all later txns in month)
      - PaymentAccount.current_balance updated after
    """
    date = _parse_date(date)

    ftxn = FinancialTransaction.objects.create(
        type=type_,
        amount=amount,
        contact=contact,
        payment_account=account,
        document=document,
        date=date,
        notes=notes,
        monthly_cumulative_delta=Decimal('0'),
    )

    if force_mcd_zero:
        if account:
            account.current_balance += amount
            account.save(update_fields=['current_balance', 'updated_at'])
        return ftxn

    if contact:
        _recalculate_mcd(contact, date)

    if account:
        account.current_balance += amount
        account.save(update_fields=['current_balance', 'updated_at'])

    return ftxn


def _create_stxn(type_, quantity, product, document=None, date=None, rate=None, notes=None):
    """
    Creates a StockTransaction.
    actual → updates product.current_stock immediately.
    record → current_stock unchanged (moved later via Move Stock).
    """
    from inventory.models import StockTransaction
    date = _parse_date(date)
    stxn = StockTransaction.objects.create(
        type=type_, quantity=quantity, product=product,
        document=document, date=date, rate=rate, notes=notes,
    )
    if type_ == 'actual':
        product.current_stock += quantity
        product.save(update_fields=['current_stock', 'updated_at'])
    return stxn


def _resolve_total(data):
    """Computes total_amount from line_items + charges − discount + taxes."""
    line_items = data.get('line_items', [])
    subtotal   = sum(Decimal(str(i.get('amount', 0))) for i in line_items)
    charges    = sum(Decimal(str(c.get('amount', 0))) for c in data.get('charges', []))
    discount   = Decimal(str(data.get('discount', 0)))
    tax_amount = Decimal('0')
    for tax in data.get('taxes', []):
        tax_amount += (subtotal + charges - discount) * Decimal(str(tax['percentage'])) / 100
    return subtotal + charges - discount + tax_amount


def _handle_stxns(doc, line_items, sign, app_settings, date):
    """
    Creates record or actual s.txns for line_items with a product_id.
    product_id=null items are completely ignored (manual/service items).
    """
    from inventory.models import Product
    for item in line_items:
        pid = item.get('product_id')
        if not pid:
            continue
        try:
            product = Product.objects.get(pk=pid)
        except Product.DoesNotExist:
            continue
        qty      = sign * Decimal(str(item.get('quantity', 0)))
        txn_type = 'actual' if app_settings.auto_stock else 'record'
        _create_stxn(txn_type, qty, product, doc, date, item.get('rate'))


# ─── Document Signs ───────────────────────────────────────────────────────────

FTXN_RECORD_SIGN = {
    'bill':    Decimal('1'),   # we owe them  → +ve
    'invoice': Decimal('-1'),  # they owe us  → -ve
    'cn':      Decimal('1'),   # we owe refund → +ve
    'dn':      Decimal('-1'),  # they owe us   → -ve
}

STXN_SIGN = {
    'bill':    Decimal('1'),   # stock IN
    'invoice': Decimal('-1'),  # stock OUT
    'cn':      Decimal('1'),   # return IN
    'dn':      Decimal('-1'),  # return OUT
}

CHALLAN_STXN_SIGN = {
    'bill':    Decimal('1'),
    'invoice': Decimal('-1'),
    'cn':      Decimal('1'),
    'dn':      Decimal('-1'),
}

NO_TXN_TYPES = {'po', 'pi', 'quotation'}


# ─── Document Create ──────────────────────────────────────────────────────────

@transaction.atomic
def process_document_create(doc_type, data, contact=None):
    app_settings = Settings.get()
    date         = _parse_date(data.get('date'))
    line_items   = data.get('line_items', [])
    total_amount = data.get('total_amount')

    if not total_amount and line_items:
        total_amount = _resolve_total(data)

    doc = Document.objects.create(
        type            = doc_type,
        doc_id          = data.get('doc_id') or _next_doc_id(doc_type),
        contact         = contact,
        consignee_id    = data.get('consignee'),
        reference_id    = data.get('reference'),
        line_items      = line_items,
        total_amount    = total_amount,
        discount        = data.get('discount', 0),
        charges         = data.get('charges', []),
        taxes           = data.get('taxes', []),
        date            = date,
        due_date        = _parse_date(data.get('due_date')) if data.get('due_date') else None,
        payment_terms   = data.get('payment_terms'),
        attachment_urls = data.get('attachment_urls', []),
        notes           = data.get('notes'),
    )

    NO_DIRECT_TXN_TYPES = NO_TXN_TYPES | {'cash_payment_voucher', 'cash_receipt_voucher'}
    if doc_type in NO_DIRECT_TXN_TYPES:
        return doc

    if doc_type == 'challan':
        ref = doc.reference
        if ref and ref.type in CHALLAN_STXN_SIGN:
            _handle_stxns(doc, line_items, CHALLAN_STXN_SIGN[ref.type], app_settings, date)
        return doc

    if doc_type == 'expense':
        account_id = data.get('payment_account')
        account    = PaymentAccount.objects.get(pk=account_id) if account_id else None
        if total_amount:
            _create_ftxn(
                'actual', -Decimal(str(total_amount)),
                contact, account, doc, date,
                force_mcd_zero=True,
            )
        return doc

    if doc_type == 'interest':
        return doc

    if doc_type in FTXN_RECORD_SIGN and total_amount:
        record_amount = FTXN_RECORD_SIGN[doc_type] * Decimal(str(total_amount))
        account_id    = data.get('payment_account')
        account       = PaymentAccount.objects.get(pk=account_id) if account_id else None

        if app_settings.auto_transaction and account:
            _create_ftxn('record', record_amount, contact, None, doc, date)
            _create_ftxn('actual', -record_amount, contact, account, doc, date)
        else:
            _create_ftxn('record', record_amount, contact, None, doc, date)

    if doc_type in STXN_SIGN and not app_settings.enable_challan:
        _handle_stxns(doc, line_items, STXN_SIGN[doc_type], app_settings, date)

    return doc


# ─── Send / Receive ───────────────────────────────────────────────────────────

@transaction.atomic
def process_send_receive(contact, data, direction):
    app_settings   = Settings.get()
    amount_raw     = Decimal(str(data['amount']))
    actual_amount  = amount_raw if direction == 'receive' else -amount_raw
    account_id     = data.get('payment_account')
    account        = PaymentAccount.objects.get(pk=account_id) if account_id else None
    date           = _parse_date(data.get('date'))
    doc_ref_id     = data.get('document')
    doc_ref        = Document.objects.get(pk=doc_ref_id) if doc_ref_id else None
    is_expense     = data.get('is_expense', False)
    interest_lines = data.get('interest_lines', [])
    result         = {}

    if is_expense:
        expense_doc = Document.objects.create(
            type         = 'expense',
            doc_id       = _next_doc_id('expense'),
            contact      = contact,
            line_items   = data.get('line_items', []),
            total_amount = abs(actual_amount),
            date         = date,
        )
        ftxn = _create_ftxn(
            'actual', actual_amount, contact, account,
            expense_doc, date, data.get('notes'),
            force_mcd_zero=True,
        )
        result['expense_doc'] = expense_doc.pk
        result['ftxn']        = ftxn.pk
        return result

    voucher_doc = None
    if app_settings.enable_vouchers and account and account.type == 'cash':
        v_type      = 'cash_payment_voucher' if direction == 'send' else 'cash_receipt_voucher'
        voucher_doc = Document.objects.create(
            type         = v_type,
            doc_id       = _next_doc_id(v_type),
            contact      = contact,
            line_items   = data.get('line_items', []),
            total_amount = abs(actual_amount),
            date         = date,
        )

    if interest_lines:
        net = sum(
            Decimal(str(l['amount'])) if l.get('type') == 'charge'
            else -Decimal(str(l['amount']))
            for l in interest_lines
        )
        interest_record_amount = -net if direction == 'receive' else net
        interest_doc = Document.objects.create(
            type         = 'interest',
            doc_id       = _next_doc_id('interest'),
            contact      = contact,
            line_items   = interest_lines,
            total_amount = abs(net),
            date         = date,
            reference    = doc_ref,
        )
        interest_ftxn = _create_ftxn(
            'record', interest_record_amount, contact, None, interest_doc, date,
        )
        result['interest_doc']  = interest_doc.pk
        result['interest_ftxn'] = interest_ftxn.pk

    main_ftxn      = _create_ftxn(
        'actual', actual_amount, contact, account,
        voucher_doc or doc_ref, date, data.get('notes'),
    )
    result['ftxn'] = main_ftxn.pk
    return result


# ─── Transfer ─────────────────────────────────────────────────────────────────

@transaction.atomic
def process_transfer(data):
    """Spec B2: Contra transfer between two payment accounts."""
    amount   = Decimal(str(data['amount']))
    date     = _parse_date(data.get('date'))
    from_acc = PaymentAccount.objects.get(pk=data['from_account'])
    to_acc   = PaymentAccount.objects.get(pk=data['to_account'])
    _create_ftxn('contra', -amount, None, from_acc, None, date)
    _create_ftxn('contra',  amount, None, to_acc,   None, date)
    return {'from': data['from_account'], 'to': data['to_account'], 'amount': str(amount)}


# ─── Adjust Balance ───────────────────────────────────────────────────────────

@transaction.atomic
def process_adjust_balance(account, data):
    """Spec B3: Actual f.txn with no contact and no document."""
    amount = Decimal(str(data['amount']))
    date   = _parse_date(data.get('date'))
    ftxn   = _create_ftxn('actual', amount, None, account, None, date, data.get('notes'))
    return {'ftxn': ftxn.pk, 'new_balance': str(account.current_balance)}


# ─── Move Stock ───────────────────────────────────────────────────────────────

@transaction.atomic
def process_move_stock(document, data):
    """
    Creates actual s.txns for a document's pending record s.txns.
    BF-06 fix: replaced per-product loop queries with batch aggregations.
    Overshoot protection: qty hard-capped at remaining (record − actuals).
    """
    from inventory.models import StockTransaction, Product

    date        = _parse_date(data.get('date'))
    items       = data.get('items', [])
    product_ids = [int(item['product_id']) for item in items]

    if document.type == 'challan' and document.reference:
        sign = CHALLAN_STXN_SIGN.get(document.reference.type, Decimal('1'))
    else:
        sign = STXN_SIGN.get(document.type, Decimal('1'))

    # Single aggregation per type — no per-product queries (BF-06)
    record_map = {
        r['product_id']: abs(r['total'] or Decimal('0'))
        for r in StockTransaction.objects.filter(
            document=document, type='record', product_id__in=product_ids
        ).values('product_id').annotate(total=Sum('quantity'))
    }
    actual_map = {
        a['product_id']: abs(a['total'] or Decimal('0'))
        for a in StockTransaction.objects.filter(
            document=document, type='actual', product_id__in=product_ids
        ).values('product_id').annotate(total=Sum('quantity'))
    }
    products = {p.pk: p for p in Product.objects.filter(pk__in=product_ids)}

    created = []
    for item in items:
        pid           = int(item['product_id'])
        requested_qty = Decimal(str(item['quantity']))
        product       = products.get(pid)
        if not product:
            continue

        record_qty  = record_map.get(pid, Decimal('0'))
        actual_qty  = actual_map.get(pid, Decimal('0'))
        remaining   = record_qty - actual_qty
        qty_to_move = min(requested_qty, remaining)
        if qty_to_move <= 0:
            continue

        stxn = _create_stxn('actual', sign * qty_to_move, product, document, date)
        created.append({'product': pid, 'quantity': str(qty_to_move), 'stxn': stxn.pk})

    return {'moved': created}


# ─── Document Delete ──────────────────────────────────────────────────────────

@transaction.atomic
def process_document_delete(document, strategy):
    """
    Spec Part 5 — EXACTLY 2 options: 'revert' or 'manual'.

    BF-07 fix: In revert path, MCD was only recalculated for actual txn dates,
    not for record txn dates. If record and actual span different months (valid
    scenario), the record's month MCD stayed stale after deletion.
    Fix: explicitly recalculate MCD for record_contact_dates in revert path too.
    """
    from inventory.models import StockTransaction

    # Collect record contacts/dates before deletion
    record_ftxns         = list(document.transactions.filter(type='record').select_related('contact'))
    record_contact_dates = [(f.contact, f.date) for f in record_ftxns]
    document.transactions.filter(type='record').delete()

    actual_ftxns = list(document.transactions.filter(type='actual').select_related('contact', 'payment_account'))
    actual_stxns = list(StockTransaction.objects.filter(document=document, type='actual').select_related('product'))

    if strategy == 'revert':
        # BF-07: Recalculate MCD for record txn months after their deletion
        for contact, date in record_contact_dates:
            if document.type != 'expense' and contact:
                _recalculate_mcd(contact, date)

        for ftxn in actual_ftxns:
            if ftxn.payment_account:
                ftxn.payment_account.current_balance -= ftxn.amount
                ftxn.payment_account.save(update_fields=['current_balance', 'updated_at'])
            contact = ftxn.contact
            date    = ftxn.date
            ftxn.delete()
            if document.type != 'expense':
                _recalculate_mcd(contact, date)

        for stxn in actual_stxns:
            stxn.product.current_stock -= stxn.quantity
            stxn.product.save(update_fields=['current_stock', 'updated_at'])
            stxn.delete()

    elif strategy == 'manual':
        # Actual f.txns stay intact — only recalculate for record txn months
        for contact, date in record_contact_dates:
            if document.type != 'expense' and contact:
                _recalculate_mcd(contact, date)

    document.is_active = False
    document.save(update_fields=['is_active', 'updated_at'])
    return {'status': 'deleted', 'strategy': strategy}


# ── Stock List PDF (Inventory page) ───────────────────────────────────────────
def generate_stock_list_pdf(products, request=None, low_stock_only=False):
    app_settings = Settings.get()
    rows = []
    low_count = 0
    for p in products:
        is_low = Decimal(str(p.current_stock)) <= Decimal(str(p.min_stock))
        if is_low:
            low_count += 1
        rows.append({
            'name':          p.name,
            'description':   p.description or '',
            'hsn_code':      p.hsn_code or '—',
            'unit':          p.unit,
            'rate':          str(p.rate),
            'current_stock': str(p.current_stock),
            'min_stock':     str(p.min_stock),
            'is_low':        is_low,
            'image_url':     _build_media_url(request, p.image_url) if p.image_url else None,
        })

    context = {
        'settings':        app_settings,
        'header_image':    _build_media_url(request, app_settings.header_image),
        'products':        rows,
        'report_title':    'Low Stock Report' if low_stock_only else 'Inventory Stock Report',
        'total_products':  len(rows),
        'low_stock_count': low_count,
        'low_stock_only':  low_stock_only,
    }
    html_string = render_to_string('inventory/stock_list_print.html', context)
    pdf_bytes   = _render_playwright_pdf(html_string)
    return (pdf_bytes, f"Inventory_{timezone.now().date()}.pdf")


# ── Stock Transactions PDF (Product detail page) ───────────────────────────────
def generate_stock_transactions_pdf(stock_txns, request=None, product=None, date_from=None, date_to=None):
    from inventory.models import StockTransaction as StockTxnModel
    from django.db.models import Sum

    app_settings  = Settings.get()

    # Opening stock = current_stock minus all actuals on/after date_from
    opening_stock = Decimal('0')
    if product:
        if date_from:
            after_sum = (
                StockTxnModel.objects
                .filter(product=product, type='actual', date__gte=date_from)
                .aggregate(total=Sum('quantity'))['total'] or Decimal('0')
            )
            opening_stock = Decimal(str(product.current_stock)) - Decimal(str(after_sum))
        else:
            all_sum = (
                StockTxnModel.objects
                .filter(product=product, type='actual')
                .aggregate(total=Sum('quantity'))['total'] or Decimal('0')
            )
            opening_stock = Decimal(str(product.current_stock)) - Decimal(str(all_sum))

    # Sort ascending for running balance
    sorted_txns = sorted(stock_txns, key=lambda t: (str(t.date), str(t.created_at)))
    running = Decimal(str(opening_stock))
    rows    = []

    for stxn in sorted_txns:
        qty       = Decimal(str(stxn.quantity))
        is_actual = stxn.type == 'actual'
        if is_actual:
            running += qty
        rows.append({
            'date':          stxn.date,
            'type':          stxn.get_type_display(),
            'type_raw':      stxn.type,
            'quantity':      str(abs(qty).quantize(Decimal('0.01'))),
            'qty_in':        qty > 0,
            'qty_out':       qty < 0,
            'doc_id':        stxn.document.doc_id if stxn.document else '—',
            'doc_type':      stxn.document.get_type_display() if stxn.document else '—',
            'rate':          str(stxn.rate) if stxn.rate else '—',
            'notes':         stxn.notes or '—',
            'running_stock': str(running.quantize(Decimal('0.01'))) if is_actual else None,
            'running_pos':   running > 0,
        })

    show_opening  = date_from is not None and opening_stock != Decimal('0')
    opening_str   = str(opening_stock.quantize(Decimal('0.01'))) if show_opening else None

    context = {
        'settings':          app_settings,
        'header_image':      _build_media_url(request, app_settings.header_image),
        'transactions':      rows,
        'product':           product,
        'report_title':      f"Stock History — {product.name}" if product else "Stock Transactions",
        'date_from':         date_from,
        'date_to':           date_to,
        'total_rows':        len(rows),
        'show_opening':      show_opening,
        'opening_stock':     opening_str,
        'opening_stock_pos': opening_stock > Decimal('0'),
    }
    html_string = render_to_string('inventory/stock_transactions_print.html', context)
    pdf_bytes   = _render_playwright_pdf(html_string)
    filename    = (
        f"Stock_{product.name.replace(' ', '_')}_{timezone.now().date()}.pdf"
        if product else f"StockTransactions_{timezone.now().date()}.pdf"
    )
    return (pdf_bytes, filename)

def generate_stock_txn_list_pdf(stock_txns, request=None, report_title=None):
    """
    Stock transactions report PDF (ST-01) — flat list, no product context.
    Used from the global Stock Transactions page if needed.
    """
    app_settings = Settings.get()

    rows = []
    for stxn in stock_txns:
        rows.append({
            'date':     stxn.date,
            'type':     stxn.get_type_display(),
            'product':  stxn.product.name if stxn.product else '—',
            'quantity': str(stxn.quantity),
            'doc_id':   stxn.document.doc_id if stxn.document else '—',
            'doc_type': stxn.document.get_type_display() if stxn.document else '—',
            'rate':     str(stxn.rate) if stxn.rate else '—',
            'notes':    stxn.notes or '—',
        })

    context = {
        'settings':     app_settings,
        'header_image': _build_media_url(request, app_settings.header_image),
        'transactions': rows,
        'report_title': report_title or 'Stock Transactions',
        'printed_on':   timezone.now().date(),
        'total_rows':   len(rows),
    }

    html_string = render_to_string('inventory/stock_transactions_print.html', context)
    pdf_bytes   = _render_playwright_pdf(html_string)
    filename    = f"StockTransactions_{timezone.now().date()}.pdf"
    return (pdf_bytes, filename)

@transaction.atomic
def process_standalone_interest(contact, data):
    date           = _parse_date(data.get('date'))
    interest_lines = data.get('line_items', [])
    toggle         = data.get('toggle', 'charge')  # 'charge' = we receive, 'credit' = we pay

    # ✅ Respect per-line type, same formula as process_send_receive
    net = sum(
        Decimal(str(l['amount'])) if l.get('type') != 'discount'
        else -Decimal(str(l['amount']))
        for l in interest_lines
    )

    record_amount = net if toggle == 'we_pay' else -net

    interest_doc = Document.objects.create(
        type         = 'interest',
        doc_id       = _next_doc_id('interest'),
        contact      = contact,
        line_items   = interest_lines,
        total_amount = abs(net),
        date         = date,
        reference_id = data.get('reference'),   # ← also wire up the linked doc
    )
    ftxn = _create_ftxn('record', record_amount, contact, None, interest_doc, date)
    return {'interest_doc': interest_doc.pk, 'ftxn': ftxn.pk}

def _sync_ftxn_contact(doc, old_contact):
    """
    Called after doc.save() when contact may have changed.
    - Bulk-updates contact on all linked FinancialTransactions.
    - Recalculates MCD for old contact (those txns no longer belong to it).
    - Recalculates MCD for new contact (those txns now belong to it).
    If contact didn't change, exits immediately — zero DB cost.
    """
    new_contact = doc.contact

    # pk-safe comparison — handles None on both sides
    old_pk = old_contact.pk if old_contact else None
    new_pk = new_contact.pk if new_contact else None
    if old_pk == new_pk:
        return

    ftxns = list(FinancialTransaction.objects.filter(document=doc))
    if not ftxns:
        return

    # Unique dates affected (usually just one, but cover multi-date edge cases)
    affected_dates = list({f.date for f in ftxns})

    # Single bulk UPDATE — no per-row save needed
    FinancialTransaction.objects.filter(document=doc).update(contact=new_contact)

    # Old contact loses these txns → recalculate its MCD
    if old_contact:
        for date in affected_dates:
            _recalculate_mcd(old_contact, date)

    # New contact gains these txns → recalculate its MCD
    if new_contact:
        for date in affected_dates:
            _recalculate_mcd(new_contact, date)
