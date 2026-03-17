from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from datetime import date as date_type
from .models import Document, FinancialTransaction
from shared.models import PaymentAccount, Settings
from django.template.loader import render_to_string
from django.core.cache import cache
import io
from django.conf import settings
from shared.models import Settings, Contact


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
    return {
        'name':       contact.company_name or contact.contact_name,
        'phone':      contact.phone,
        'gstin':      contact.gstin,
        'address':    contact.address,
        'all_phones': (contact.additional_contacts or []) + [
            {'name': contact.contact_name, 'number': contact.phone, 'role': 'primary'}
        ] if contact.phone else (contact.additional_contacts or []),
    }


def generate_document_pdf(document, request=None):
    cache_key = f"pdf_{document.pk}_{document.updated_at.timestamp()}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    app_settings = Settings.get()

    line_items  = document.line_items or []
    charges     = document.charges or []
    taxes       = document.taxes or []

    subtotal     = sum(Decimal(str(i.get('amount', 0))) for i in line_items)
    charges_sum  = sum(Decimal(str(c.get('amount', 0))) for c in charges)
    discount     = document.discount or Decimal('0')
    taxable_base = subtotal + charges_sum - discount
    tax_breakdown = []
    tax_total    = Decimal('0')
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

    context = {
        'document':       document,
        'settings':       app_settings,
        'header_image':   _build_media_url(request, app_settings.header_image),
        'sign_image':     _build_media_url(request, app_settings.sign_image),
        'contact':        _contact_display(document.contact),
        'consignee':      _contact_display(document.consignee),
        'line_items':     line_items,
        'charges':        charges,
        'discount':       str(document.discount),
        'taxes':          tax_breakdown,
        'tax_total':      str(tax_total.quantize(Decimal('0.01'))),
        'grand_total':    str(grand_total.quantize(Decimal('0.01'))),
        'doc_type_label': document.get_type_display(),
    }

    html_string = render_to_string('accounting/document_print.html', context)
    pdf_bytes   = _render_playwright_pdf(html_string, request=request)
    filename    = f"{document.type.upper()}_{document.doc_id}_{document.date}.pdf"
    result      = (pdf_bytes, filename)
    cache.set(cache_key, result, timeout=600)
    return result


def generate_transactions_pdf(transactions, contact=None, request=None):
    app_settings = Settings.get()

    rows       = []
    running_cf = contact.opening_balance if contact else None

    for txn in transactions:
        row = {
            'date':     txn.date,
            'type':     txn.get_type_display(),
            'doc_id':   txn.document.doc_id if txn.document else '—',
            'doc_type': txn.document.get_type_display() if txn.document else '—',
            'notes':    txn.notes or '—',
            'amount':   str(txn.amount),
            'account':  str(txn.payment_account) if txn.payment_account else '—',
        }
        if running_cf is not None:
            running_cf += txn.amount
            row['running_cf'] = str(running_cf.quantize(Decimal('0.01')))
        rows.append(row)

    context = {
        'settings':     app_settings,
        'header_image': _build_media_url(request, app_settings.header_image),
        'contact':      contact,
        'transactions': rows,
        'is_ledger':    contact is not None,
        'report_title': (
            f"Ledger — {contact.company_name or contact.contact_name}"
            if contact else "Financial Transactions"
        ),
    }

    html_string = render_to_string('accounting/transactions_print.html', context)
    pdf_bytes   = _render_playwright_pdf(html_string, request=request)

    if contact:
        safe_name = (contact.company_name or contact.contact_name).replace(' ', '_')
        filename  = f"Ledger_{safe_name}_{timezone.now().date()}.pdf"
    else:
        filename = f"Transactions_{timezone.now().date()}.pdf"

    return (pdf_bytes, filename)


def _render_playwright_pdf(html_string, request=None):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page    = browser.new_page()
        page.set_content(html_string, wait_until='networkidle')
        pdf_bytes = page.pdf(
            format=settings.PLAYWRIGHT_PDF_FORMAT,
            print_background=True,
            prefer_css_page_size=True,
        )
        browser.close()
    return pdf_bytes


def _parse_date(val):
    if not val:
        return timezone.localdate()
    if isinstance(val, str):
        return date_type.fromisoformat(val)
    return val


def _next_doc_id(doc_type):
    prefix_map = {
        'bill': 'BILL', 'invoice': 'INV', 'po': 'PO', 'pi': 'PI',
        'quotation': 'QUO', 'challan': 'CHL', 'cn': 'CN', 'dn': 'DN',
        'cash_payment_voucher': 'CPV', 'cash_receipt_voucher': 'CRV',
        'interest': 'INT', 'expense': 'EXP',
    }
    prefix = prefix_map.get(doc_type, 'DOC')
    count  = Document.objects.filter(type=doc_type).count() + 1
    return f"{prefix}-{count:04d}"


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
    txns = FinancialTransaction.objects.filter(
        contact=contact,
        date__year=date.year,
        date__month=date.month,
    ).order_by('date', 'created_at').select_related('document')

    running = Decimal('0')
    for t in txns:
        # ✅ expense f.txns: MCD always 0 — never affect contact CF
        is_expense = t.document is not None and t.document.type == 'expense'
        if is_expense:
            if t.monthly_cumulative_delta != Decimal('0'):
                t.monthly_cumulative_delta = Decimal('0')
                t.save(update_fields=['monthly_cumulative_delta'])
            # ✅ Do NOT add to running — expense skipped from CF entirely
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

    force_mcd_zero=True  → expense path:
      - MCD stays 0 (contact CF never affected)
      - PaymentAccount.current_balance still updated normally
      - _recalculate_mcd NOT called

    Normal path:
      - _recalculate_mcd called first (sets correct MCD on the new txn)
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
        monthly_cumulative_delta=Decimal('0'),  # placeholder; set correctly below
    )

    if force_mcd_zero:
        # ✅ Expense: MCD stays 0, account still updated
        if account:
            account.current_balance += amount
            account.save(update_fields=['current_balance', 'updated_at'])
        return ftxn

    # ✅ Normal path: recalculate MCD (this will set ftxn.monthly_cumulative_delta correctly)
    if contact:
        _recalculate_mcd(contact, date)

    # Update account balance after MCD is correct
    if account:
        account.current_balance += amount
        account.save(update_fields=['current_balance', 'updated_at'])

    return ftxn


def _create_stxn(type_, quantity, product, document=None, date=None, rate=None, notes=None):
    """
    Creates a StockTransaction.
    actual  → updates product.current_stock immediately.
    record  → current_stock unchanged (moved later via Move Stock).
    """
    from inventory.models import StockTransaction
    date = _parse_date(date)
    stxn = StockTransaction.objects.create(
        type=type_,
        quantity=quantity,
        product=product,
        document=document,
        date=date,
        rate=rate,
        notes=notes,
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


# ─── Document Signs ────────────────────────────────────────────────────────────


FTXN_RECORD_SIGN = {
    'bill':    Decimal('1'),   # they give us goods, we owe them  → +ve (we owe them)
    'invoice': Decimal('-1'),  # we give goods, they owe us       → -ve (they owe us)
    'cn':      Decimal('1'),   # return of sale, we owe refund    → +ve
    'dn':      Decimal('-1'),  # return of purchase, they owe us  → -ve
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


# ─── Document Create ───────────────────────────────────────────────────────────


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

    # ── Types with no transactions at all ────────────────────────────────────
    NO_DIRECT_TXN_TYPES = NO_TXN_TYPES | {'cash_payment_voucher', 'cash_receipt_voucher'}
    if doc_type in NO_DIRECT_TXN_TYPES:
        return doc

    # ── Challan: stock only, no f.txn ────────────────────────────────────────
    if doc_type == 'challan':
        ref = doc.reference
        if ref and ref.type in CHALLAN_STXN_SIGN:
            _handle_stxns(doc, line_items, CHALLAN_STXN_SIGN[ref.type], app_settings, date)
        return doc

    # ── Expense: actual f.txn only, MCD=0, NO record f.txn ──────────────────
    if doc_type == 'expense':
        account_id = data.get('payment_account')
        account    = PaymentAccount.objects.get(pk=account_id) if account_id else None
        if total_amount:
            # ✅ Always create actual f.txn for expense
            # ✅ account can be None (spec: contact optional, account optional)
            # ✅ force_mcd_zero=True ensures contact CF is NEVER affected
            _create_ftxn(
                'actual',
                -Decimal(str(total_amount)),  # outgoing → negative
                contact,
                account,
                doc,
                date,
                force_mcd_zero=True,
            )
        return doc

    # ── Interest: no f.txn on direct create ──────────────────────────────────
    # Interest docs are ONLY created via:
    #   - process_send_receive (with interest_lines)
    #   - standalone_interest action (Path C)
    #   - record_payment action (with interest_lines)
    # Direct create of type='interest' via this path should not create any f.txn
    if doc_type == 'interest':
        return doc

    # ── Bill, Invoice, CN, DN ────────────────────────────────────────────────
    if doc_type in FTXN_RECORD_SIGN and total_amount:
        record_amount = FTXN_RECORD_SIGN[doc_type] * Decimal(str(total_amount))
        account_id    = data.get('payment_account')
        account       = PaymentAccount.objects.get(pk=account_id) if account_id else None

        if app_settings.auto_transaction and account:
            # ✅ Both record + actual created; they net to 0 CF impact
            _create_ftxn('record', record_amount, contact, None, doc, date)
            _create_ftxn('actual', -record_amount, contact, account, doc, date)
        else:
            # ✅ Record only — actual created later via Record Payment
            _create_ftxn('record', record_amount, contact, None, doc, date)

    # ── Stock txns for Bill, Invoice, CN, DN (when challan not overriding) ───
    if doc_type in STXN_SIGN and not app_settings.enable_challan:
        _handle_stxns(doc, line_items, STXN_SIGN[doc_type], app_settings, date)

    return doc


# ─── Send / Receive ────────────────────────────────────────────────────────────


@transaction.atomic
def process_send_receive(contact, data, direction):
    """
    Handles Send / Receive from a contact's ledger page.

    Flows:
      plain send/receive   → actual f.txn only
      with interest_lines  → interest doc + record f.txn (opposite sign) + actual
      with is_expense      → expense doc + actual f.txn (MCD=0, CF unaffected)
      cash + vouchers ON   → voucher doc created first, actual f.txn linked to it

    Sign rules (spec Part 4):
      receive (+) → interest record = −net_interest
      send    (−) → interest record = +net_interest
    """
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

    # ── Expense flow ─────────────────────────────────────────────────────────
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

    # ── Voucher creation (cash account + vouchers enabled) ───────────────────
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

    # ── Interest lines ────────────────────────────────────────────────────────
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

    # ── Main actual f.txn ─────────────────────────────────────────────────────
    main_ftxn = _create_ftxn(
        'actual', actual_amount, contact, account,
        voucher_doc or doc_ref, date, data.get('notes'),
    )
    result['ftxn'] = main_ftxn.pk
    return result


# ─── Transfer ──────────────────────────────────────────────────────────────────


@transaction.atomic
def process_transfer(data):
    """
    Contra transfer between two payment accounts.
    Spec B2: No contact. No document. Two contra f.txns.
    """
    amount   = Decimal(str(data['amount']))
    date     = _parse_date(data.get('date'))
    from_acc = PaymentAccount.objects.get(pk=data['from_account'])
    to_acc   = PaymentAccount.objects.get(pk=data['to_account'])
    _create_ftxn('contra', -amount, None, from_acc, None, date)
    _create_ftxn('contra',  amount, None, to_acc,   None, date)
    return {'from': data['from_account'], 'to': data['to_account'], 'amount': str(amount)}


# ─── Adjust Balance ────────────────────────────────────────────────────────────


@transaction.atomic
def process_adjust_balance(account, data):
    """
    Actual f.txn with no contact and no document.
    Spec B3: bank interest credits, corrections, etc.
    No contact → no MCD recalculation.
    """
    amount = Decimal(str(data['amount']))
    date   = _parse_date(data.get('date'))
    ftxn   = _create_ftxn('actual', amount, None, account, None, date, data.get('notes'))
    return {'ftxn': ftxn.pk, 'new_balance': str(account.current_balance)}


# ─── Move Stock ────────────────────────────────────────────────────────────────


@transaction.atomic
def process_move_stock(document, data):
    """
    Creates actual s.txns for a document's pending record s.txns.
    Spec 6.1 / 6.2:
      - Sign from document type (or challan's reference doc type)
      - Overshoot protection: qty hard-capped at remaining (record − actuals)
      - Partial moves supported
    """
    from inventory.models import StockTransaction, Product

    date  = _parse_date(data.get('date'))
    items = data.get('items', [])

    if document.type == 'challan' and document.reference:
        sign = CHALLAN_STXN_SIGN.get(document.reference.type, Decimal('1'))
    else:
        sign = STXN_SIGN.get(document.type, Decimal('1'))

    created = []
    for item in items:
        pid           = item['product_id']
        requested_qty = Decimal(str(item['quantity']))
        product       = Product.objects.get(pk=pid)

        record_qty = abs(sum(
            t.quantity for t in StockTransaction.objects.filter(
                document=document, product=product, type='record'
            )
        ))
        actual_qty = abs(sum(
            t.quantity for t in StockTransaction.objects.filter(
                document=document, product=product, type='actual'
            )
        ))
        remaining   = record_qty - actual_qty
        qty_to_move = min(requested_qty, remaining)
        if qty_to_move <= 0:
            continue

        stxn = _create_stxn('actual', sign * qty_to_move, product, document, date)
        created.append({
            'product':  pid,
            'quantity': str(qty_to_move),
            'stxn':     stxn.pk,
        })

    return {'moved': created}


# ─── Document Delete ───────────────────────────────────────────────────────────


@transaction.atomic
def process_document_delete(document, strategy):
    """
    Spec Part 5 — EXACTLY 2 options:

    'revert':
      - All record f.txns  → hard deleted
      - All actual f.txns  → hard deleted + PaymentAccount balance reversed
      - All record s.txns  → hard deleted
      - All actual s.txns  → hard deleted + Product.current_stock reversed
      - Document: is_active = False

    'manual':
      - All record f.txns  → hard deleted
      - All actual f.txns  → remain intact (FK stays, UI shows ⚠️ badge)
      - All record s.txns  → hard deleted
      - All actual s.txns  → remain intact
      - Document: is_active = False

    CRITICAL: System NEVER auto-nulls any FK on any transaction.
    """
    from inventory.models import StockTransaction

    # ── Always delete record f.txns first ────────────────────────────────────
    # Collect contacts/dates before deletion for MCD recalc
    record_ftxns = list(document.transactions.filter(type='record'))
    record_contact_dates = [(f.contact, f.date) for f in record_ftxns]
    document.transactions.filter(type='record').delete()

    actual_ftxns = list(document.transactions.filter(type='actual'))
    actual_stxns = list(StockTransaction.objects.filter(document=document, type='actual'))

    if strategy == 'revert':
        for ftxn in actual_ftxns:
            if ftxn.payment_account:
                ftxn.payment_account.current_balance -= ftxn.amount
                ftxn.payment_account.save(update_fields=['current_balance', 'updated_at'])
            contact = ftxn.contact
            date    = ftxn.date
            ftxn.delete()
            # ✅ Skip MCD recalc for expense docs — CF was never affected
            if document.type != 'expense':
                _recalculate_mcd(contact, date)

        for stxn in actual_stxns:
            stxn.product.current_stock -= stxn.quantity
            stxn.product.save(update_fields=['current_stock', 'updated_at'])
            stxn.delete()

    elif strategy == 'manual':
        # ✅ Recalculate MCD after removing record f.txns (they affected MCD)
        # Actual f.txns stay intact — no balance reversal needed
        for contact, date in record_contact_dates:
            if document.type != 'expense' and contact:
                _recalculate_mcd(contact, date)

    document.is_active = False
    document.save(update_fields=['is_active', 'updated_at'])
    return {'status': 'deleted', 'strategy': strategy}
