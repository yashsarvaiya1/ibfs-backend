# accounting/services.py
from pathlib import Path
import re
import hashlib
from decimal import Decimal
from django.db import transaction
from django.db.models import Sum, F, Q
from django.utils import timezone
from datetime import date as date_type
from .models import Document, FinancialTransaction
from shared.models import PaymentAccount, Settings, Contact
from django.template.loader import render_to_string
from django.core.cache import cache
from django.conf import settings as django_settings
from .calculations import document_totals
from .printing import document_context, media_data_url
from .workflows import FINANCIAL_SIGNS, STOCK_SIGNS, NON_POSTING_TYPES, stock_mode


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _build_media_url(request, relative_path):
    from .printing import media_data_url
    return media_data_url(relative_path)


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
    app_settings = Settings.get()
    contacts = tuple(c.updated_at.isoformat() if c else '' for c in (document.contact, document.consignee))
    revision = f'{document.pk}_{document.updated_at.isoformat()}_{app_settings.updated_at.isoformat()}_{contacts}'
    cache_key = 'pdf_v4_' + hashlib.sha256(revision.encode()).hexdigest()
    result = cache.get(cache_key)
    if result:
        return result
    context = _build_document_context(document, app_settings, request)
    html_string = render_to_string('accounting/document_print.html', {
        'documents': [context], 'print_settings': app_settings,
        'page_letterhead': context['header_image'] if app_settings.letterhead_mode == 'page' else None,
    })
    pdf_bytes = _render_playwright_pdf(html_string, context['header_image'] if app_settings.letterhead_mode == 'page' else None)
    safe_id = re.sub(r'[^A-Za-z0-9._-]', '_', document.doc_id)
    result = (pdf_bytes, f'{document.type.upper()}_{safe_id}_{document.date}.pdf')
    cache.set(cache_key, result, timeout=600)
    return result


def generate_bulk_documents_pdf(documents, request=None):
    app_settings = Settings.get()
    contexts = [_build_document_context(doc, app_settings, request) for doc in documents]
    html_string = render_to_string('accounting/document_print.html', {
        'documents': contexts, 'print_settings': app_settings,
        'page_letterhead': media_data_url(app_settings.header_image) if app_settings.letterhead_mode == 'page' else None,
    })
    return _render_playwright_pdf(html_string, media_data_url(app_settings.header_image) if app_settings.letterhead_mode == 'page' else None), f'Documents_Bulk_{timezone.localdate()}.pdf'


def _build_document_context(document, app_settings, request=None, is_bulk=False):
    return document_context(document, app_settings, _contact_display)


def compute_opening_balance_for_print(contact, date_from=None) -> Decimal:
    base = Decimal(str(contact.opening_balance or 0))
    if date_from is None:
        return base

    delta = FinancialTransaction.objects.filter(contact=contact, date__lt=date_from).exclude(
        document__type='expense').exclude(type='contra').aggregate(total=Sum('amount'))['total'] or Decimal('0')
    return base + delta


def generate_transactions_pdf(
    transactions,
    contact=None,
    request=None,
    opening_balance_at=None,
    is_ledger_view=False,
    report_title=None,
    account=None,
    balance_before_period=None,
    date_from=None,
    date_to=None,
):
    app_settings = Settings.get()

    if opening_balance_at is not None:
        running_cf = Decimal(str(opening_balance_at))
    elif contact:
        running_cf = Decimal(str(contact.opening_balance or 0))
    else:
        running_cf = None

    transactions = sorted(transactions, key=lambda txn: (txn.date, txn.created_at, txn.pk)) if is_ledger_view else transactions
    rows = []
    for txn in transactions:
        # Mirror frontend ContactLedger logic exactly
        is_expense = txn.document is not None and txn.document.type == 'expense'
        is_contra  = txn.type == 'contra'
        affects_cf = bool(account) or (not is_expense and not is_contra)

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
            'debit':      affects_cf and (txn.amount > 0 if account else txn.amount < 0),
            'credit':     affects_cf and (txn.amount < 0 if account else txn.amount > 0),
        }

        if is_ledger_view and running_cf is not None:
            # Only advance running_cf for CF-affecting txns (matches frontend)
            authoritative = getattr(txn, 'running_balance' if account else 'running_cf', None)
            if authoritative is not None:
                running_cf = authoritative
            elif affects_cf:
                running_cf += txn.amount
            # Always emit balance (expense rows show unchanged balance, same as frontend)
            row['running_cf']          = str(abs(running_cf).quantize(Decimal('0.01')))
            row['running_cf_positive'] = running_cf > 0
            row['running_cf_zero']     = running_cf == 0
            row['balance_debit']       = running_cf > 0 if account else running_cf < 0

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
        'opening_balance_debit': (ob_pos if account else not ob_pos) if not ob_zero else False,
        'date_from': date_from,
        'date_to': date_to,
        'closing_balance': str(abs(running_cf).quantize(Decimal('0.01'))) if running_cf is not None else None,
        'closing_balance_zero': running_cf == 0,
        'closing_balance_debit': (running_cf > 0 if account else running_cf < 0) if running_cf is not None else False,
        'balance_before_period': (
            str(abs(Decimal(str(balance_before_period))).quantize(Decimal('0.01')))
            if balance_before_period is not None else None
        ),
        'balance_before_period_debit': balance_before_period is not None and Decimal(str(balance_before_period)) > 0,
        'balance_before_period_zero': balance_before_period is not None and Decimal(str(balance_before_period)) == 0,
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



def _render_playwright_pdf(html_string, letterhead=None):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(django_settings.PLAYWRIGHT_PDF_TIMEOUT)
            page.set_content(html_string, wait_until='load')
            page.evaluate("""async () => {
                await document.fonts.ready;
                await Promise.all([...document.images].map(image => image.decode().catch(() => {})));
            }""")
            document_layout = page.locator('[data-document-layout]').count() > 0
            if document_layout:
                page.evaluate(Path(__file__).with_name('document_pagination.js').read_text())
                # Pagination creates new image elements; decode the clones too.
                page.evaluate('async () => { await Promise.all([...document.images].map(image => image.decode().catch(() => {}))); }')
            pdf_bytes = page.pdf(
                format=django_settings.PLAYWRIGHT_PDF_FORMAT,
                print_background=True, prefer_css_page_size=True,
                display_header_footer=not document_layout, header_template='<span></span>',
                footer_template='<div style="font:9px Arial;width:100%;text-align:center;color:#64748b;">Page <span class="pageNumber"></span> of <span class="totalPages"></span></div>',
            )
            if letterhead:
                import io
                from pypdf import PdfReader, PdfWriter
                background = browser.new_page()
                background.set_content('<style>@page{size:A4;margin:0}body{margin:0}img{width:210mm;height:297mm;object-fit:contain;display:block}</style><img src="' + letterhead + '">', wait_until='load')
                background.evaluate('async () => { await document.images[0].decode(); }')
                backdrop = background.pdf(print_background=True, prefer_css_page_size=True)
                reader = PdfReader(io.BytesIO(pdf_bytes))
                writer = PdfWriter()
                for content in reader.pages:
                    sheet = PdfReader(io.BytesIO(backdrop)).pages[0]
                    sheet.merge_page(content)
                    writer.add_page(sheet)
                output = io.BytesIO()
                writer.write(output)
                pdf_bytes = output.getvalue()
            return pdf_bytes
        finally:
            browser.close()


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

    # A stable singleton lock also protects the very first document of a type.
    Settings.objects.select_for_update().get(pk=Settings.get().pk)

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

@transaction.atomic
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
    Contact.objects.select_for_update().get(pk=contact.pk)
    date = _parse_date(date)
    txns = (
        FinancialTransaction.objects
        .filter(contact=contact, date__year=date.year, date__month=date.month)
        .order_by('date', 'created_at', 'pk')
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

@transaction.atomic
def _create_ftxn(
    type_, amount, contact=None, account=None,
    document=None, date=None, notes=None,
    force_mcd_zero=False, auto_allocate=True,
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
    if contact:
        Contact.objects.select_for_update().get(pk=contact.pk)
    amount = Decimal(str(amount))

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

    if type_ == 'actual' and auto_allocate:
        from .payments import allocate_payment
        allocate_payment(ftxn, document)

    if force_mcd_zero:
        if account:
            PaymentAccount.objects.filter(pk=account.pk).update(current_balance=F('current_balance') + amount, updated_at=timezone.now())
            account.refresh_from_db(fields=['current_balance'])
        return ftxn

    if contact:
        _recalculate_mcd(contact, date)

    if account:
        PaymentAccount.objects.filter(pk=account.pk).update(current_balance=F('current_balance') + amount, updated_at=timezone.now())
        account.refresh_from_db(fields=['current_balance'])

    return ftxn


@transaction.atomic
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
        type(product).objects.filter(pk=product.pk).update(current_stock=F('current_stock') + quantity, updated_at=timezone.now())
        product.refresh_from_db(fields=['current_stock'])
    return stxn


def _resolve_total(data):
    return document_totals(data, data.get('type'))['total']


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
        txn_type = doc.stock_mode or ('actual' if app_settings.auto_stock else 'record')
        if txn_type == 'none':
            continue
        _create_stxn(txn_type, qty, product, doc, date, item.get('rate'))


# ─── Document Signs ───────────────────────────────────────────────────────────

FTXN_RECORD_SIGN = FINANCIAL_SIGNS
STXN_SIGN = STOCK_SIGNS
CHALLAN_STXN_SIGN = STOCK_SIGNS
NO_TXN_TYPES = NON_POSTING_TYPES


# ─── Document Create ──────────────────────────────────────────────────────────

@transaction.atomic
def process_document_create(doc_type, data, contact=None):
    app_settings = Settings.get()
    date         = _parse_date(data.get('date'))
    line_items   = data.get('line_items', [])
    total_amount = data.get('total_amount')

    if line_items and doc_type != 'challan':
        totals = document_totals(data, doc_type)
        total_amount = totals['total']
        data = {**data, 'discount': totals['discount']}

    reference = Document.objects.filter(pk=data.get('reference')).first() if data.get('reference') else None
    doc = Document.objects.create(
        stock_mode      = stock_mode(doc_type, app_settings, reference.type if reference else None),
        type            = doc_type,
        doc_id          = data.get('doc_id') or _next_doc_id(doc_type),
        contact         = contact,
        consignee_id    = data.get('consignee'),
        reference_id    = data.get('reference'),
        line_items      = line_items,
        total_amount    = total_amount,
        discount        = data.get('discount', 0),
        discount_percentage = data.get('discount_percentage'),
        charges         = data.get('charges', []),
        taxes           = data.get('taxes', []),
        tax_mode = data.get('tax_mode', 'document'),
        supply_category = data.get('supply_category'),
        supplier_invoice_number = data.get('supplier_invoice_number'),
        date            = date,
        due_date        = _parse_date(data.get('due_date')) if data.get('due_date') else None,
        payment_terms   = data.get('payment_terms'),
        place_of_supply = data.get('place_of_supply'),
        reverse_charge = data.get('reverse_charge'),
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
    from .commands import PaymentCommandSerializer, command_data
    from rest_framework.exceptions import ValidationError
    serializer = PaymentCommandSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    data = command_data(serializer)
    if data.get('document'):
        linked = Document.objects.get(pk=data['document'])
        if linked.contact_id and (not contact or linked.contact_id != contact.pk):
            raise ValidationError({'document':'Choose a document belonging to this contact.'})
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
            reference    = doc_ref,
        )

    interest_doc = None
    net = Decimal('0')
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
        voucher_doc or doc_ref, date, data.get('notes'), auto_allocate=False,
    )
    from .payments import allocate_payment
    if doc_ref:
        principal = max(amount_raw - net, Decimal('0'))
        sign = FTXN_RECORD_SIGN.get(doc_ref.type)
        if sign is not None:
            compatible = (direction == 'send') == (sign > 0)
            allocate_payment(main_ftxn, doc_ref, principal if compatible else -principal)
    if interest_doc:
        allocate_payment(main_ftxn, interest_doc, min(net, amount_raw))
    result['ftxn'] = main_ftxn.pk
    return result


# ─── Transfer ─────────────────────────────────────────────────────────────────

@transaction.atomic
def process_transfer(data):
    """Spec B2: Contra transfer between two payment accounts."""
    from .commands import TransferCommandSerializer
    serializer = TransferCommandSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    data = dict(serializer.validated_data)
    data['from_account'] = data['from_account'].pk
    data['to_account'] = data['to_account'].pk
    list(PaymentAccount.objects.select_for_update().filter(pk__in=[data['from_account'],data['to_account']]).order_by('pk'))
    amount   = Decimal(str(data['amount']))
    date     = _parse_date(data.get('date'))
    from_acc = PaymentAccount.objects.get(pk=data['from_account'])
    to_acc   = PaymentAccount.objects.get(pk=data['to_account'])
    import uuid
    group=uuid.uuid4()
    outgoing=_create_ftxn('contra', -amount, None, from_acc, None, date)
    incoming=_create_ftxn('contra', amount, None, to_acc, None, date)
    FinancialTransaction.objects.filter(pk__in=[outgoing.pk,incoming.pk]).update(transfer_group=group)
    return {'from': data['from_account'], 'to': data['to_account'], 'amount': str(amount)}


# ─── Adjust Balance ───────────────────────────────────────────────────────────

@transaction.atomic
def process_adjust_balance(account, data):
    """Spec B3: Actual f.txn with no contact and no document."""
    from rest_framework import serializers
    amount = serializers.DecimalField(max_digits=15, decimal_places=2).run_validation(data.get('amount'))
    date = serializers.DateField().run_validation(data['date']) if data.get('date') else timezone.localdate()
    ftxn = _create_ftxn('actual', amount, None, account, None, date, data.get('notes'))
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

    from rest_framework.exceptions import ValidationError
    from .calculations import decimal_value
    from .stock_status import document_stock_status
    snapshot = Document.objects.get(pk=document.pk)
    contact_ids = set(snapshot.transactions.values_list('contact_id',flat=True))
    contact_ids.update(FinancialTransaction.objects.filter(allocations__document=snapshot).values_list('contact_id',flat=True))
    contact_ids.add(snapshot.contact_id)
    list(Contact.objects.select_for_update().filter(pk__in=[pk for pk in contact_ids if pk]).order_by('pk'))
    document = Document.objects.select_for_update().get(pk=document.pk)
    if document.updated_at != snapshot.updated_at:
        from .commands import EditConflict
        raise EditConflict()
    if not document.is_active:
        raise ValidationError({'document':'This document has been deleted.'})
    date = _parse_date(data.get('date'))
    quantities = {}
    for item in data.get('items', []):
        try:
            pid = int(item['product_id'])
            quantity = decimal_value(item['quantity'])
        except (ValueError, TypeError, KeyError) as exc:
            raise ValidationError({'items':'Choose a product and a valid quantity.'}) from exc
        if quantity <= 0:
            raise ValidationError({'items':'Quantity must be greater than zero.'})
        quantities[pid] = quantities.get(pid, Decimal('0')) + quantity
    statuses = {row['product_id']:row for row in document_stock_status(document)}
    if set(quantities) - statuses.keys():
        raise ValidationError({'items':'Choose products with expected stock on this document.'})
    products = {p.pk:p for p in Product.objects.filter(pk__in=quantities)}
    created = []
    for pid in sorted(quantities):
        row = statuses[pid]
        quantity = min(quantities[pid], Decimal(row['remaining_qty']))
        if quantity <= 0:
            continue
        sign = Decimal('1') if row['direction'] == 'in' else Decimal('-1')
        stxn = _create_stxn('actual', sign*quantity, products[pid], document, date)
        created.append({'product':pid, 'quantity':str(quantity), 'stxn':stxn.pk})
    return {'moved':created}


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

    if strategy not in {'revert', 'manual'}:
        from rest_framework.exceptions import ValidationError
        raise ValidationError({'strategy': 'Choose revert or manual.'})
    snapshot = Document.objects.get(pk=document.pk)
    contact_ids = set(snapshot.transactions.values_list('contact_id',flat=True))
    contact_ids.update(FinancialTransaction.objects.filter(allocations__document=snapshot).values_list('contact_id',flat=True))
    contact_ids.add(snapshot.contact_id)
    list(Contact.objects.select_for_update().filter(pk__in=[pk for pk in contact_ids if pk]).order_by('pk'))
    document = Document.objects.select_for_update().get(pk=document.pk)
    if document.updated_at != snapshot.updated_at:
        from .commands import EditConflict
        raise EditConflict()
    if not document.is_active:
        return {'status': 'deleted', 'strategy': strategy}
    allocated_payments = FinancialTransaction.objects.filter(
        Q(document=document) | Q(allocations__document=document), type='actual').distinct()
    if strategy == 'revert':
        for payment in allocated_payments:
            if payment.allocations.exclude(document=document).exists():
                from rest_framework.exceptions import ValidationError
                raise ValidationError({'strategy':'A payment also settles charges or another document. Keep transactions or adjust its allocation first.'})
    StockTransaction.objects.filter(document=document, type='record').delete()

    # Collect record contacts/dates before deletion
    record_ftxns         = list(document.transactions.filter(type='record').select_related('contact'))
    record_contact_dates = [(f.contact, f.date) for f in record_ftxns]
    document.transactions.filter(type='record').delete()

    actual_ftxns = list(allocated_payments.select_related('contact', 'payment_account', 'document'))
    actual_stxns = list(StockTransaction.objects.filter(document=document, type='actual').select_related('product'))

    if strategy == 'revert':
        # BF-07: Recalculate MCD for record txn months after their deletion
        for contact, date in record_contact_dates:
            if document.type != 'expense' and contact:
                _recalculate_mcd(contact, date)

        for ftxn in actual_ftxns:
            if ftxn.payment_account:
                PaymentAccount.objects.filter(pk=ftxn.payment_account_id).update(current_balance=F('current_balance') - ftxn.amount, updated_at=timezone.now())
            contact = ftxn.contact
            date    = ftxn.date
            if ftxn.document and ftxn.document_id != document.pk and ftxn.document.type in {'cash_payment_voucher','cash_receipt_voucher'}:
                ftxn.document.is_active = False
                ftxn.document.save(update_fields=['is_active','updated_at'])
            ftxn.delete()
            if document.type != 'expense':
                _recalculate_mcd(contact, date)

        for stxn in actual_stxns:
            type(stxn.product).objects.filter(pk=stxn.product_id).update(current_stock=F('current_stock') - stxn.quantity, updated_at=timezone.now())
            stxn.delete()

    elif strategy == 'manual':
        # Actual f.txns stay intact — only recalculate for record txn months
        for contact, date in record_contact_dates:
            if document.type != 'expense' and contact:
                _recalculate_mcd(contact, date)

    document.payment_allocations.all().delete()
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


# ── Stock history PDF ─────────────────────────────────────────────────────────
def _build_stock_history_context(stock_txns, product=None, date_from=None, date_to=None, request=None, report_title=None):
    from inventory.models import StockTransaction
    from django.db.models import Sum

    opening = Decimal('0')
    closing = None
    balances = {}
    if product:
        actuals = StockTransaction.objects.filter(product=product, type='actual')
        if date_from:
            actuals = actuals.filter(date__gte=date_from)
        opening = Decimal(str(product.current_stock)) - (actuals.aggregate(total=Sum('quantity'))['total'] or Decimal('0'))
        if date_to:
            actuals = actuals.filter(date__lte=date_to)
        running = opening
        # Physical balances include all movements, even when the displayed list
        # is filtered by type, document or search.
        for movement in actuals.order_by('date', 'created_at', 'pk').values('id', 'quantity'):
            running += movement['quantity']
            balances[movement['id']] = str(running.quantize(Decimal('0.01')))
        closing = str(running.quantize(Decimal('0.01')))

    rows = []
    for txn in sorted(stock_txns, key=lambda t: (t.date, t.created_at, t.pk)):
        qty = Decimal(str(txn.quantity))
        rows.append({
            'date': txn.date, 'type': 'Expected' if txn.type == 'record' else 'Moved',
            'type_raw': txn.type, 'is_expected': txn.type == 'record',
            'product_name': txn.product.name, 'unit': txn.product.unit,
            'quantity': str(abs(qty).quantize(Decimal('0.01'))),
            'qty_in': qty > 0, 'qty_out': qty < 0,
            'doc_id': txn.document.doc_id if txn.document else '—',
            'doc_type': txn.document.get_type_display() if txn.document else '—',
            'rate': str(txn.rate) if txn.rate is not None else '—',
            'notes': txn.notes or '—',
            'running_stock': balances.get(txn.pk) if product and txn.type == 'actual' else None,
        })
    app_settings = Settings.get()
    return {
        'settings': app_settings, 'header_image': _build_media_url(request, app_settings.header_image),
        'transactions': rows, 'product': product,
        'report_title': report_title or (f'Stock History — {product.name}' if product else 'Stock Transactions'),
        'date_from': date_from, 'date_to': date_to, 'total_rows': len(rows),
        'show_opening': product is not None, 'show_closing': product is not None,
        'opening_stock': str(opening.quantize(Decimal('0.01'))) if product else None,
        'closing_stock': closing,
    }


def generate_stock_transactions_pdf(stock_txns, request=None, product=None, date_from=None, date_to=None, report_title=None):
    context = _build_stock_history_context(stock_txns, product, date_from, date_to, request, report_title)
    html = render_to_string('inventory/stock_transactions_print.html', context)
    filename = f"Stock_{product.name.replace(' ', '_')}_{timezone.localdate()}.pdf" if product else f'StockTransactions_{timezone.localdate()}.pdf'
    return _render_playwright_pdf(html), filename


def generate_stock_txn_list_pdf(stock_txns, request=None, report_title=None):
    return generate_stock_transactions_pdf(stock_txns, request=request, report_title=report_title)


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


@transaction.atomic
def reverse_transfer(payment):
    from rest_framework.exceptions import ValidationError
    import uuid
    if payment.type!='contra' or not payment.transfer_group:
        raise ValidationError({'transfer':'This historical entry has no stored transfer pair. Use a balance reconciliation after reviewing both accounts.'})
    snapshot=list(FinancialTransaction.objects.filter(transfer_group=payment.transfer_group))
    list(PaymentAccount.objects.select_for_update().filter(pk__in=[row.payment_account_id for row in snapshot]).order_by('pk'))
    rows=list(FinancialTransaction.objects.select_for_update(of=('self',)).filter(transfer_group=payment.transfer_group).select_related('payment_account'))
    if len(rows)!=2 or any(row.is_reversed or row.type!='contra' or not row.payment_account for row in rows) or sum(row.amount for row in rows)!=0:
        raise ValidationError({'transfer':'This transfer is already reversed or its pair is incomplete.'})
    group=uuid.uuid4()
    for row in rows:
        reversal=_create_ftxn('contra',-row.amount,None,row.payment_account,None,timezone.localdate(),notes='Transfer reversal')
        # A reversal is retained as history; it cannot itself be reversed repeatedly.
        reversal.transfer_group=group
        reversal.is_reversed=True
        reversal.save(update_fields=['transfer_group','is_reversed','updated_at'])
    FinancialTransaction.objects.filter(pk__in=[row.pk for row in rows]).update(is_reversed=True,updated_at=timezone.now())
    return {'status':'reversed','transfer_group':str(payment.transfer_group)}
