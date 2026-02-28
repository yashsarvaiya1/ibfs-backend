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
    Recalculates MCD for all f.txns in the same month/year for a contact.
    Per spec Part 11:
    - expense f.txns → MCD forced to 0 (never affect contact CF)
    - contra f.txns  → no contact, never reach here
    - all others     → running cumulative sum within the month, ordered by date + created_at
    Called after any f.txn create/edit/delete that affects a contact.
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
    """
    Creates a FinancialTransaction and handles all side effects:
    - Updates PaymentAccount.current_balance (for actual/contra with account)
    - Recalculates MCD for the contact's month (unless force_mcd_zero)
    force_mcd_zero=True is used for expense f.txns — account still updated,
    but MCD stays 0 so contact CF is never affected.
    """
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
        # Expense: MCD stays 0, account balance still updated
        if account:
            account.current_balance += amount
            account.save(update_fields=['current_balance', 'updated_at'])
        return ftxn

    # Normal path: recalculate MCD, then update account balance
    _recalculate_mcd(contact, date)
    if account:
        account.current_balance += amount
        account.save(update_fields=['current_balance', 'updated_at'])
    return ftxn


def _create_stxn(type, quantity, product, document=None, date=None, rate=None, notes=None):
    """
    Creates a StockTransaction.
    For actual s.txns: updates product.current_stock immediately.
    For record s.txns: current_stock is unchanged (moved later via Move Stock).
    """
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


def _handle_stxns(doc, line_items, sign, settings, date):
    """
    Creates record or actual s.txns for all line_items that have a product_id.
    product_id=null items are completely ignored — they never generate s.txns.
    Per spec: "product_id is a plain integer snapshot (not a FK), null for manual/service items"
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
        txn_type = 'actual' if settings.auto_stock else 'record'
        _create_stxn(txn_type, qty, product, doc, date, item.get('rate'))


# ─── Document Signs ────────────────────────────────────────────────────────────

# f.txn record sign — per spec Part 2 sign convention:
# + positive = we owe them (money coming IN to them / obligation on us)
# − negative = they owe us
FTXN_RECORD_SIGN = {
    'bill':    Decimal('1'),   # we owe vendor (+ we owe them)
    'invoice': Decimal('-1'),  # customer owes us (− they owe us)
    'cn':      Decimal('1'),   # we owe customer refund
    'dn':      Decimal('-1'),  # vendor owes us refund
}

# s.txn sign — per spec Part 2:
# + positive = stock IN
# − negative = stock OUT
STXN_SIGN = {
    'bill':    Decimal('1'),   # stock IN from purchase
    'invoice': Decimal('-1'),  # stock OUT from sale
    'cn':      Decimal('1'),   # stock IN from return of sale
    'dn':      Decimal('-1'),  # stock OUT from return of purchase
}

# Challan s.txn sign derives from referenced document type
CHALLAN_STXN_SIGN = {
    'bill':    Decimal('1'),   # Bill → stock IN
    'invoice': Decimal('-1'),  # Invoice → stock OUT
    'cn':      Decimal('1'),   # CN → stock IN (return of sale)
    'dn':      Decimal('-1'),  # DN → stock OUT (return of purchase)
}

# Doc types that generate zero f.txns and zero s.txns
NO_TXN_TYPES = {'po', 'pi', 'quotation'}


# ─── Document Create ───────────────────────────────────────────────────────────

@transaction.atomic
def process_document_create(doc_type, data, contact=None):
    settings     = Settings.get()
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

    # ── PO / PI / Quotation / Cash Vouchers — handled by send_receive flow ─────
    # Cash vouchers are ONLY created via process_send_receive, never directly.
    # Per spec: voucher doc is always created inside the Send/Receive flow.
    NO_DIRECT_TXN_TYPES = NO_TXN_TYPES | {'cash_payment_voucher', 'cash_receipt_voucher'}
    if doc_type in NO_DIRECT_TXN_TYPES:
        return doc

    # ── Challan — s.txn only, sign from reference doc type ────────────────────
    if doc_type == 'challan':
        ref = doc.reference
        if ref and ref.type in CHALLAN_STXN_SIGN:
            _handle_stxns(doc, line_items, CHALLAN_STXN_SIGN[ref.type], settings, date)
        return doc

    # ── Expense — actual f.txn only, MCD forced to 0 ──────────────────────────
    if doc_type == 'expense':
        account_id = data.get('payment_account')
        account    = PaymentAccount.objects.get(pk=account_id) if account_id else None
        if total_amount and account:
            _create_ftxn(
                'actual', -Decimal(str(total_amount)),
                contact, account, doc, date,
                force_mcd_zero=True,
            )
        return doc

    # ── Financial txns — bill / invoice / cn / dn ─────────────────────────────
    if doc_type in FTXN_RECORD_SIGN and total_amount:
        record_amount = FTXN_RECORD_SIGN[doc_type] * Decimal(str(total_amount))
        account_id    = data.get('payment_account')
        account       = PaymentAccount.objects.get(pk=account_id) if account_id else None

        if settings.auto_transaction:
            if account:
                _create_ftxn('record', record_amount, contact, None, doc, date)
                _create_ftxn('actual', -record_amount, contact, account, doc, date)
            else:
                # Auto ON + no account → record only
                _create_ftxn('record', record_amount, contact, None, doc, date)
        else:
            # Auto OFF → record only
            _create_ftxn('record', record_amount, contact, None, doc, date)

    # ── Stock txns ─────────────────────────────────────────────────────────────
    if doc_type in STXN_SIGN and not settings.enable_challan:
        _handle_stxns(doc, line_items, STXN_SIGN[doc_type], settings, date)

    return doc



# ─── Send / Receive ────────────────────────────────────────────────────────────

@transaction.atomic
def process_send_receive(contact, data, direction):
    """
    Handles Send / Receive actions from a contact's ledger page.

    Flows:
      - Plain send/receive          → actual f.txn only
      - With interest_lines         → interest doc + record f.txn (opposite sign), then actual
      - With is_expense flag        → expense doc + actual f.txn (MCD=0)
      - With cash + enable_vouchers → voucher doc created first, actual f.txn linked to it

    Per spec Part 4:
      Receiving (+) → interest record = −net_interest
      Sending   (−) → interest record = +net_interest
    """
    settings       = Settings.get()
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

    # ── Expense flow ───────────────────────────────────────────────────────────
    # Per spec: expense f.txn MCD=0, contact CF never affected
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

    # ── Cash voucher doc created BEFORE any f.txn ─────────────────────────────
    # Per spec: "Voucher document created first → then f.txn created."
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

    # ── Interest record FIRST (before actual) ─────────────────────────────────
    # Per spec Part 4: "Interest record sign is always the OPPOSITE of the main actual payment sign."
    # Creating record first ensures lower created_at → correct MCD ordering.
    if interest_lines:
        net = sum(
            Decimal(str(l['amount'])) if l.get('type') == 'charge'
            else -Decimal(str(l['amount']))
            for l in interest_lines
        )
        # Receiving (+actual) → interest record = −net
        # Sending   (−actual) → interest record = +net
        interest_record_amount = -net if direction == 'receive' else net
        interest_doc = Document.objects.create(
            type         = 'interest',
            doc_id       = _next_doc_id('interest'),
            contact      = contact,
            line_items   = interest_lines,
            total_amount = abs(net),
            date         = date,
            reference    = doc_ref,  # inherits same reference as payment doc per spec
        )
        interest_ftxn = _create_ftxn(
            'record', interest_record_amount, contact, None, interest_doc, date,
        )
        result['interest_doc']  = interest_doc.pk
        result['interest_ftxn'] = interest_ftxn.pk

    # ── Main actual SECOND ────────────────────────────────────────────────────
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
    Per spec B2: No contact. No document. Two contra f.txns.
    contra f.txns never affect any contact → MCD = 0 (no contact to recalculate).
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
    Per spec B3: used for bank interest credits, corrections, etc.
    No contact → no MCD recalculation needed.
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
    Per spec 6.1 / 6.2:
    - Sign from document type (or challan's reference doc type)
    - Overshoot protection: qty_to_move hard-capped at remaining (record − actuals)
      Stock actual can NEVER exceed record quantity for a given document.
    - Partial moves supported — remaining auto-recalculated each call
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
        qty_to_move = min(requested_qty, remaining)  # overshoot cap
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
    Per spec Part 5 — EXACTLY 2 options, no third option exists:

    Option 1 — 'revert':
      - All record f.txns: hard deleted
      - All actual f.txns: hard deleted + PaymentAccount.current_balance reversed
      - All record s.txns: hard deleted
      - All actual s.txns: hard deleted + Product.current_stock reversed
      - Document: is_active = False
      - MCD recalculated for affected contacts/months

    Option 2 — 'manual':
      - All record f.txns: hard deleted
      - All actual f.txns: remain intact — FK document reference STAYS as-is
        (system NEVER auto-nulls FK — is_active=False on doc drives ⚠️ UI warning)
      - All record s.txns: hard deleted
      - All actual s.txns: remain intact — same FK behavior
      - Document: is_active = False

    ⛔ CRITICAL: The system NEVER auto-nulls any FK reference on any transaction.
    """
    from inventory.models import StockTransaction

    # ── Step 1: Always hard-delete all record f.txns ──────────────────────────
    document.transactions.filter(type='record').delete()

    actual_ftxns = list(document.transactions.filter(type='actual'))
    actual_stxns = list(StockTransaction.objects.filter(document=document, type='actual'))

    if strategy == 'revert':
        # Hard delete actuals + reverse all side effects
        for ftxn in actual_ftxns:
            if ftxn.payment_account:
                ftxn.payment_account.current_balance -= ftxn.amount
                ftxn.payment_account.save(update_fields=['current_balance', 'updated_at'])
            contact = ftxn.contact
            date    = ftxn.date
            ftxn.delete()
            # Recalculate MCD after deletion so running sums are correct
            _recalculate_mcd(contact, date)

        for stxn in actual_stxns:
            stxn.product.current_stock -= stxn.quantity
            stxn.product.save(update_fields=['current_stock', 'updated_at'])
            stxn.delete()

    elif strategy == 'manual':
        # Keep actual f.txns and s.txns completely intact — FK stays pointing to this doc
        # Per spec: "FK document reference stays as-is. Since document.is_active = false,
        # the UI renders a ⚠️ 'Document Deleted' warning badge."
        # Do absolutely nothing to actuals — they stay as-is with their document FK.
        pass

    # ── Final: soft-delete the document ───────────────────────────────────────
    document.is_active = False
    document.save(update_fields=['is_active', 'updated_at'])
    return {'status': 'deleted', 'strategy': strategy}
