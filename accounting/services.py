# accounting/services.py
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from datetime import date as date_type
from .models import Document, FinancialTransaction
from shared.models import PaymentAccount, Settings


# ─── Helpers ──────────────────────────────────────────────────────────────────

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


def _recalculate_mcd(contact, date):
    """
    Recalculates MCD for all txns in the same month/year for a contact.
    Expense txns always keep MCD = 0 — they never affect contact CF.
    Contra txns have no contact so they never reach here.
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


def _create_ftxn(
    type, amount, contact=None, account=None,
    document=None, date=None, notes=None,
    force_mcd_zero=False,
):
    date = _parse_date(date)
    ftxn = FinancialTransaction.objects.create(
        type=type,
        amount=amount,
        contact=contact,
        payment_account=account,
        document=document,
        date=date,
        notes=notes,
        monthly_cumulative_delta=Decimal('0'),
    )
    if force_mcd_zero:
        # Expense: MCD stays 0, still update account balance
        if account:
            account.current_balance += amount
            account.save(update_fields=['current_balance'])
        return ftxn

    _recalculate_mcd(contact, date)
    if account:
        account.current_balance += amount
        account.save(update_fields=['current_balance'])
    return ftxn


def _create_stxn(type, quantity, product, document=None, date=None, rate=None, notes=None):
    from inventory.models import StockTransaction
    date = _parse_date(date)
    stxn = StockTransaction.objects.create(
        type=type,
        quantity=quantity,
        product=product,
        document=document,
        date=date,
        rate=rate,
        notes=notes,
    )
    if type == 'actual':
        product.current_stock += quantity
        product.save(update_fields=['current_stock'])
    return stxn


def _resolve_total(data):
    """Compute total_amount from line_items + charges - discount + taxes if not provided."""
    line_items = data.get('line_items', [])
    subtotal   = sum(Decimal(str(i.get('amount', 0))) for i in line_items)
    charges    = sum(Decimal(str(c.get('amount', 0))) for c in data.get('charges', []))
    discount   = Decimal(str(data.get('discount', 0)))
    tax_amount = Decimal('0')
    for tax in data.get('taxes', []):
        tax_amount += (subtotal + charges - discount) * Decimal(str(tax['percentage'])) / 100
    return subtotal + charges - discount + tax_amount


# ─── Document Signs ────────────────────────────────────────────────────────────

# f.txn record sign per doc type
FTXN_RECORD_SIGN = {
    'bill': Decimal('1'), 'invoice': Decimal('-1'),
    'cn':   Decimal('1'), 'dn':      Decimal('-1'),
}

# s.txn sign per doc type (challan derives from reference, handled separately)
STXN_SIGN = {
    'bill': Decimal('1'), 'invoice': Decimal('-1'),
    'cn':   Decimal('1'), 'dn':      Decimal('-1'),
}

# Challan s.txn sign derives from its referenced document type
CHALLAN_STXN_SIGN = {
    'bill': Decimal('1'), 'invoice': Decimal('-1'),
    'cn':   Decimal('1'), 'dn':      Decimal('-1'),
}

# These doc types generate ZERO f.txns and ZERO s.txns
NO_TXN_TYPES = {'po', 'pi', 'quotation'}


# ─── Document Create ───────────────────────────────────────────────────────────

@transaction.atomic
def process_document_create(doc_type, data, contact=None):
    settings    = Settings.get()
    date        = _parse_date(data.get('date'))
    line_items  = data.get('line_items', [])
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

    # ── No txns for PO / PI / Quotation ───────────────────────────────────────
    if doc_type in NO_TXN_TYPES:
        return doc

    # ── Challan — s.txn only, sign from reference doc ─────────────────────────
    if doc_type == 'challan':
        ref = doc.reference
        if ref and ref.type in CHALLAN_STXN_SIGN:
            sign = CHALLAN_STXN_SIGN[ref.type]
            _handle_stxns(doc, line_items, sign, settings, date)
        return doc

    # ── Financial txns for bill / invoice / cn / dn ───────────────────────────
    if doc_type in FTXN_RECORD_SIGN and total_amount:
        record_amount = FTXN_RECORD_SIGN[doc_type] * Decimal(str(total_amount))
        account_id    = data.get('payment_account')
        account       = PaymentAccount.objects.get(pk=account_id) if account_id else None

        if settings.auto_transaction:
            # Always create record first (preserves MCD history), then actual
            _create_ftxn('record', record_amount, contact, None, doc, date)
            if account:
                _create_ftxn('actual', -record_amount, contact, account, doc, date)
        else:
            _create_ftxn('record', record_amount, contact, None, doc, date)

    # ── Stock txns — skipped entirely if challan is enabled ───────────────────
    if doc_type in STXN_SIGN and not settings.enable_challan:
        sign = STXN_SIGN[doc_type]
        _handle_stxns(doc, line_items, sign, settings, date)

    return doc


def _handle_stxns(doc, line_items, sign, settings, date):
    """Creates record or actual s.txns for all line_items with a product_id."""
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
        txn_type = 'actual' if settings.auto_stock else 'record'
        _create_stxn(txn_type, qty, product, doc, date, item.get('rate'))


# ─── Send / Receive ────────────────────────────────────────────────────────────

@transaction.atomic
def process_send_receive(contact, data, direction):
    settings      = Settings.get()
    amount_raw    = Decimal(str(data['amount']))
    actual_amount = amount_raw if direction == 'receive' else -amount_raw
    account_id    = data.get('payment_account')
    account       = PaymentAccount.objects.get(pk=account_id) if account_id else None
    date          = _parse_date(data.get('date'))
    doc_ref_id    = data.get('document')
    doc_ref       = Document.objects.get(pk=doc_ref_id) if doc_ref_id else None
    is_expense    = data.get('is_expense', False)
    interest_lines = data.get('interest_lines', [])
    result        = {}

    # ── Expense ────────────────────────────────────────────────────────────────
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
            expense_doc, date, force_mcd_zero=True,
        )
        result['expense_doc'] = expense_doc.pk
        result['ftxn']        = ftxn.pk
        return result

    # ── Cash Voucher doc created first ─────────────────────────────────────────
    voucher_doc = None
    if settings.enable_vouchers and account and account.type == 'cash':
        v_type      = 'cash_payment_voucher' if direction == 'send' else 'cash_receipt_voucher'
        voucher_doc = Document.objects.create(
            type         = v_type,
            doc_id       = _next_doc_id(v_type),
            contact      = contact,
            line_items   = data.get('line_items', []),
            total_amount = abs(actual_amount),
            date         = date,
        )

    # ── Interest record FIRST (lower created_at → appears before actual in ledger)
    if interest_lines:
        net = sum(
            Decimal(str(l['amount'])) if l.get('type') == 'charge'
            else -Decimal(str(l['amount']))
            for l in interest_lines
        )
        interest_record_amount = net * (
            Decimal('-1') if direction == 'receive' else Decimal('1')
        )
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

    # ── Main actual SECOND ─────────────────────────────────────────────────────
    main_ftxn      = _create_ftxn(
        'actual', actual_amount, contact, account,
        voucher_doc or doc_ref, date, data.get('notes'),
    )
    result['ftxn'] = main_ftxn.pk
    return result


# ─── Transfer ──────────────────────────────────────────────────────────────────

@transaction.atomic
def process_transfer(data):
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
    amount = Decimal(str(data['amount']))
    date   = _parse_date(data.get('date'))
    ftxn   = _create_ftxn('actual', amount, None, account, None, date, data.get('notes'))
    return {'ftxn': ftxn.pk, 'new_balance': str(account.current_balance)}


# ─── Move Stock ────────────────────────────────────────────────────────────────

@transaction.atomic
def process_move_stock(document, data):
    """
    Moves stock for a document. Sign is derived from document type.
    For challan: sign comes from its reference doc type.
    Overshoot protection: actual can never exceed record for same doc+product.
    """
    from inventory.models import StockTransaction

    date  = _parse_date(data.get('date'))
    items = data.get('items', [])

    # Derive sign
    if document.type == 'challan' and document.reference:
        sign = CHALLAN_STXN_SIGN.get(document.reference.type, Decimal('1'))
    else:
        sign = STXN_SIGN.get(document.type, Decimal('1'))

    created = []
    for item in items:
        from inventory.models import Product
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
    from inventory.models import StockTransaction

    # Record f.txns are always hard deleted
    document.transactions.filter(type='record').delete()

    actual_ftxns = document.transactions.filter(type='actual')
    actual_stxns = StockTransaction.objects.filter(document=document, type='actual')

    if strategy == 'revert':
        for ftxn in actual_ftxns:
            if ftxn.payment_account:
                ftxn.payment_account.current_balance -= ftxn.amount
                ftxn.payment_account.save(update_fields=['current_balance'])
            _recalculate_mcd(ftxn.contact, ftxn.date)
            ftxn.delete()
        for stxn in actual_stxns:
            stxn.product.current_stock -= stxn.quantity
            stxn.product.save(update_fields=['current_stock'])
            stxn.delete()

    elif strategy == 'manual':
        # Untether from document — becomes standalone advance payment / stock entry
        actual_ftxns.update(document=None, is_doc_deleted=True)
        actual_stxns.update(document=None, is_doc_deleted=True)

    elif strategy == 'orphan':
        # Flag only — no balance or stock reversal
        actual_ftxns.update(is_doc_deleted=True)
        actual_stxns.update(is_doc_deleted=True)

    document.is_active = False
    document.save(update_fields=['is_active'])
    return {'status': 'deleted', 'strategy': strategy}
