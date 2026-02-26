# accounting/services.py
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from datetime import date as date_type
from .models import Document, FinancialTransaction
from shared.models import PaymentAccount, Settings


# ─── Helpers ─────────────────────────────────────────────────────────────────

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
    prefix   = prefix_map.get(doc_type, 'DOC')
    existing = Document.objects.filter(type=doc_type).values_list('doc_id', flat=True)
    max_num  = 0
    for did in existing:
        try:
            num = int(did.split('-')[-1])
            if num > max_num:
                max_num = num
        except (ValueError, IndexError):
            pass
    return f"{prefix}-{max_num + 1:04d}"


def _recalculate_mcd(contact, date):
    """
    Recalculate MCD for all txns in same month/year for contact.

    RULES:
    - Expense txns (document__type='expense') always keep MCD = 0 — they
      don't affect contact CF. We include them in the loop but force MCD=0.
    - All other txn types contribute normally to the running total.
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


def calculate_current_cf(contact):
    """
    Calculates the true running CF via MCD.
    Rule: Opening Balance + SUM(Last MCD of every month)
    Used in Contact serializers to safely get running balance.
    """
    if not contact:
        return Decimal('0')

    # Fetch all transactions for this contact EXCEPT expenses. 
    # If we include expenses, an expense at the very end of the month 
    # would overwrite the valid running MCD with 0.
    txns = FinancialTransaction.objects.filter(
        contact=contact
    ).exclude(
        document__type='expense'
    ).order_by('date', 'created_at')

    mcds = {}
    for t in txns:
        key = (t.date.year, t.date.month)
        mcds[key] = t.monthly_cumulative_delta

    opening = contact.opening_balance if contact.opening_balance else Decimal('0')
    return opening + sum(mcds.values())


def _create_ftxn(
    type, amount, contact=None, account=None,
    document=None, date=None, notes=None,
    force_mcd_zero=False
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


# ─── Challan Stock Sign Helper ────────────────────────────────────────────────

def _get_challan_sign(document):
    """
    Derive stock direction for a Challan from its referenced document.
    Bill / CN  → +1 (Stock IN)
    Invoice / DN → -1 (Stock OUT)
    Falls back to +1 if no valid reference found.
    """
    ref_sign_map = {
        'bill':    Decimal('1'),
        'invoice': Decimal('-1'),
        'cn':      Decimal('1'),
        'dn':      Decimal('-1'),
    }
    if document.reference and document.reference.type in ref_sign_map:
        return ref_sign_map[document.reference.type]
    return Decimal('1')


# ─── Document Create ──────────────────────────────────────────────────────────

@transaction.atomic
def process_document_create(doc_type, data, contact=None):
    settings     = Settings.get()
    date         = _parse_date(data.get('date'))
    line_items   = data.get('line_items', [])
    total_amount = data.get('total_amount')

    if not total_amount and line_items:
        subtotal   = sum(Decimal(str(i.get('amount', 0))) for i in line_items)
        charges    = sum(Decimal(str(c.get('amount', 0))) for c in data.get('charges', []))
        discount   = Decimal(str(data.get('discount', 0)))
        tax_amount = Decimal('0')
        for tax in data.get('taxes', []):
            tax_amount += (subtotal + charges - discount) * Decimal(str(tax['percentage'])) / 100
        total_amount = subtotal + charges - discount + tax_amount

    reference_id  = data.get('reference')
    reference_doc = Document.objects.get(pk=reference_id) if reference_id else None

    doc = Document.objects.create(
        type=doc_type,
        doc_id=data.get('doc_id') or _next_doc_id(doc_type),
        contact=contact,
        consignee_id=data.get('consignee'),
        reference=reference_doc,
        line_items=line_items,
        total_amount=total_amount,
        discount=data.get('discount', 0),
        charges=data.get('charges', []),
        taxes=data.get('taxes', []),
        date=date,
        due_date=_parse_date(data.get('due_date')) if data.get('due_date') else None,
        payment_terms=data.get('payment_terms'),
        attachment_urls=data.get('attachment_urls', []),
        notes=data.get('notes'),
    )

    # ── Financial Transactions (Bill, Invoice, CN, DN only) ───────────────────
    ftxn_signs = {
        'bill':    Decimal('1'),
        'invoice': Decimal('-1'),
        'cn':      Decimal('1'),
        'dn':      Decimal('-1'),
    }

    if doc_type in ftxn_signs and total_amount:
        ftxn_sign     = ftxn_signs[doc_type]
        record_amount = ftxn_sign * Decimal(str(total_amount))
        account_id    = data.get('payment_account')
        account       = PaymentAccount.objects.get(pk=account_id) if account_id else None

        if settings.auto_transaction:
            # Record first always — preserves MCD history
            _create_ftxn('record', record_amount, contact, None, doc, date)
            if account:
                # Actual is opposite sign — they cancel out → CF net 0
                _create_ftxn('actual', -record_amount, contact, account, doc, date)
        else:
            _create_ftxn('record', record_amount, contact, None, doc, date)

    # ── Stock Transactions ────────────────────────────────────────────────────
    stxn_signs = {
        'bill':    Decimal('1'),
        'invoice': Decimal('-1'),
        'cn':      Decimal('1'),
        'dn':      Decimal('-1'),
    }

    from inventory.models import Product

    if doc_type in stxn_signs and not settings.enable_challan:
        stxn_sign = stxn_signs[doc_type]
        for item in line_items:
            pid = item.get('product_id')
            if not pid:
                continue
            try:
                product = Product.objects.get(pk=pid)
            except Product.DoesNotExist:
                continue
            qty  = stxn_sign * Decimal(str(item.get('quantity', 0)))
            rate = item.get('rate')
            if settings.auto_stock:
                _create_stxn('actual', qty, product, doc, date, rate)
            else:
                _create_stxn('record', qty, product, doc, date, rate)

    # ── Challan stock (direction from reference) ───────────────────────────────
    elif doc_type == 'challan':
        challan_sign = _get_challan_sign(doc)
        for item in line_items:
            pid = item.get('product_id')
            if not pid:
                continue
            try:
                product = Product.objects.get(pk=pid)
            except Product.DoesNotExist:
                continue
            qty = challan_sign * Decimal(str(item.get('quantity', 0)))
            if settings.auto_stock:
                _create_stxn('actual', qty, product, doc, date, None)
            else:
                _create_stxn('record', qty, product, doc, date, None)

    return doc


# ─── Expense (Standalone / Quick Action — Contact Optional) ───────────────────

@transaction.atomic
def process_expense(data):
    """
    Creates Expense doc + actual f.txn (MCD forced to 0).
    Contact is optional — null for global Quick Action expenses.
    """
    from shared.models import Contact as ContactModel
    contact_id   = data.get('contact')
    contact      = ContactModel.objects.get(pk=contact_id) if contact_id else None
    account_id   = data.get('payment_account')
    account      = PaymentAccount.objects.get(pk=account_id) if account_id else None
    date         = _parse_date(data.get('date'))
    line_items   = data.get('line_items', [])
    total_amount = (
        sum(Decimal(str(i.get('amount', 0))) for i in line_items)
        if line_items else Decimal(str(data.get('total_amount', 0)))
    )
    notes = data.get('notes')

    expense_doc = Document.objects.create(
        type='expense',
        doc_id=_next_doc_id('expense'),
        contact=contact,
        line_items=line_items,
        total_amount=total_amount,
        date=date,
        notes=notes,
        attachment_urls=data.get('attachment_urls', []),
    )
    # Always negative (money going out). MCD = 0 — never affects contact CF.
    ftxn = _create_ftxn(
        'actual', -total_amount, contact, account,
        expense_doc, date, notes,
        force_mcd_zero=True,
    )
    return {'expense_doc': expense_doc.pk, 'ftxn': ftxn.pk}


# ─── Standalone Interest (Quick Action Path B) ────────────────────────────────

@transaction.atomic
def process_standalone_interest(data):
    """
    Quick Action → Interest (Path B).
    Creates Interest doc + ONE record f.txn only. No actual. No s.txn.

    action='charge' → they owe us more → record is negative
    action='credit' → we owe them    → record is positive
    """
    from shared.models import Contact as ContactModel
    contact    = ContactModel.objects.get(pk=data['contact'])
    date       = _parse_date(data.get('date'))
    line_items = data.get('line_items', [])
    action     = data.get('action', 'charge')  # 'charge' or 'credit'
    net        = sum(Decimal(str(i.get('amount', 0))) for i in line_items)
    amount     = -net if action == 'charge' else net

    doc = Document.objects.create(
        type='interest',
        doc_id=_next_doc_id('interest'),
        contact=contact,
        line_items=line_items,
        total_amount=net,
        date=date,
        notes=data.get('notes'),
        attachment_urls=data.get('attachment_urls', []),
    )
    ftxn = _create_ftxn('record', amount, contact, None, doc, date)
    return {'interest_doc': doc.pk, 'ftxn': ftxn.pk}


# ─── Send / Receive ───────────────────────────────────────────────────────────

@transaction.atomic
def process_send_receive(contact, data, direction):
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

    result = {}

    # ── Contact-specific Expense ──────────────────────────────────────────────
    if is_expense:
        return process_expense({
            'contact':          contact.pk,
            'payment_account':  account_id,
            'date':             date,
            'line_items':       data.get('line_items', []),
            'total_amount':     str(abs(actual_amount)),
            'notes':            data.get('notes'),
            'attachment_urls':  data.get('attachment_urls', []),
        })

    # ── Cash Voucher doc (created first — no f.txn yet) ───────────────────────
    voucher_doc = None
    if settings.enable_vouchers and account and account.type == 'cash':
        v_type      = 'cash_payment_voucher' if direction == 'send' else 'cash_receipt_voucher'
        voucher_doc = Document.objects.create(
            type=v_type,
            doc_id=_next_doc_id(v_type),
            contact=contact,
            line_items=data.get('line_items', []),
            total_amount=abs(actual_amount),
            date=date,
            attachment_urls=data.get('attachment_urls', []),
        )

    # ── Interest record FIRST (order: interest_doc → record → actual) ─────────
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
            type='interest',
            doc_id=_next_doc_id('interest'),
            contact=contact,
            line_items=interest_lines,
            total_amount=abs(net),
            date=date,
            reference=doc_ref,
            attachment_urls=data.get('attachment_urls', []),
        )
        interest_ftxn = _create_ftxn(
            'record', interest_record_amount, contact, None, interest_doc, date
        )
        result['interest_doc']  = interest_doc.pk
        result['interest_ftxn'] = interest_ftxn.pk

    # ── Main actual SECOND ────────────────────────────────────────────────────
    main_ftxn      = _create_ftxn(
        'actual', actual_amount, contact, account,
        voucher_doc or doc_ref, date, data.get('notes')
    )
    result['ftxn'] = main_ftxn.pk
    return result


# ─── Transfer ─────────────────────────────────────────────────────────────────

@transaction.atomic
def process_transfer(data):
    from_id  = data['from_account']
    to_id    = data['to_account']
    amount   = Decimal(str(data['amount']))
    date     = _parse_date(data.get('date'))
    from_acc = PaymentAccount.objects.get(pk=from_id)
    to_acc   = PaymentAccount.objects.get(pk=to_id)
    _create_ftxn('contra', -amount, None, from_acc, None, date)
    _create_ftxn('contra',  amount, None, to_acc,   None, date)
    return {'from': from_id, 'to': to_id, 'amount': str(amount)}


# ─── Adjust Balance ───────────────────────────────────────────────────────────

@transaction.atomic
def process_adjust_balance(account, data):
    amount = Decimal(str(data['amount']))
    date   = _parse_date(data.get('date'))
    notes  = data.get('notes')
    ftxn   = _create_ftxn('actual', amount, None, account, None, date, notes)
    return {'ftxn': ftxn.pk, 'new_balance': str(account.current_balance)}


# ─── Move Stock ───────────────────────────────────────────────────────────────

@transaction.atomic
def process_move_stock(document, data):
    from inventory.models import Product, StockTransaction
    date  = _parse_date(data.get('date'))
    items = data.get('items', [])

    base_sign_map = {
        'bill':    Decimal('1'),
        'invoice': Decimal('-1'),
        'cn':      Decimal('1'),
        'dn':      Decimal('-1'),
    }
    # Challan inherits sign from its referenced document
    doc_sign = (
        _get_challan_sign(document)
        if document.type == 'challan'
        else base_sign_map.get(document.type, Decimal('1'))
    )

    created = []
    for item in items:
        pid           = item['product_id']
        requested_qty = Decimal(str(item['quantity']))
        product       = Product.objects.get(pk=pid)

        record_qty = sum(
            abs(t.quantity) for t in StockTransaction.objects.filter(
                document=document, product=product, type='record'
            )
        )
        actual_qty = sum(
            abs(t.quantity) for t in StockTransaction.objects.filter(
                document=document, product=product, type='actual'
            )
        )
        remaining   = record_qty - actual_qty
        qty_to_move = min(requested_qty, remaining)
        if qty_to_move <= 0:
            continue
        stxn = _create_stxn('actual', doc_sign * qty_to_move, product, document, date)
        created.append({'product': pid, 'quantity': str(qty_to_move), 'stxn': stxn.pk})

    return {'moved': created}


# ─── Document Delete ──────────────────────────────────────────────────────────

@transaction.atomic
def process_document_delete(document, strategy):
    from inventory.models import StockTransaction

    contact        = document.contact
    affected_dates = set()

    for ftxn in document.transactions.filter(type='record'):
        affected_dates.add(ftxn.date)
        ftxn.delete()

    actual_ftxns = list(document.transactions.filter(type='actual'))
    actual_stxns = list(StockTransaction.objects.filter(document=document, type='actual'))

    if strategy == 'revert':
        for ftxn in actual_ftxns:
            affected_dates.add(ftxn.date)
            if ftxn.payment_account:
                ftxn.payment_account.current_balance -= ftxn.amount
                ftxn.payment_account.save(update_fields=['current_balance'])
            ftxn.delete()
        for stxn in actual_stxns:
            stxn.product.current_stock -= stxn.quantity
            stxn.product.save(update_fields=['current_stock'])
            stxn.delete()

    elif strategy == 'manual':
        for ftxn in actual_ftxns:
            affected_dates.add(ftxn.date)
        document.transactions.filter(type='actual').update(document=None, is_doc_deleted=True)
        StockTransaction.objects.filter(document=document, type='actual').update(
            document=None, is_doc_deleted=True
        )

    elif strategy == 'orphan':
        for ftxn in actual_ftxns:
            affected_dates.add(ftxn.date)
        document.transactions.filter(type='actual').update(is_doc_deleted=True)
        StockTransaction.objects.filter(document=document, type='actual').update(
            is_doc_deleted=True
        )

    document.is_active = False
    document.save(update_fields=['is_active'])

    # Recalculate MCD for all affected months after deletion
    for d in affected_dates:
        _recalculate_mcd(contact, d)

    return {'status': 'deleted', 'strategy': strategy}
