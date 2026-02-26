# accounting/views.py
from decimal import Decimal
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Document, FinancialTransaction
from .serializers import (
    DocumentSerializer, DocumentListSerializer,
    FinancialTransactionSerializer,
)
from .services import (
    _create_ftxn, _create_stxn, _next_doc_id,
    _parse_date, _recalculate_mcd,
    process_document_create, process_document_delete, process_move_stock,
    STXN_SIGN, CHALLAN_STXN_SIGN,
)
from shared.models import Contact, PaymentAccount, Settings
from inventory.models import StockTransaction, Product


# ─── Document ViewSet ──────────────────────────────────────────────────────────

class DocumentViewSet(viewsets.ModelViewSet):
    search_fields   = ['doc_id', 'contact__contact_name', 'contact__company_name']
    ordering_fields = ['date', 'created_at', 'total_amount']
    ordering        = ['-date']

    def get_queryset(self):
        qs     = Document.objects.filter(is_active=True).prefetch_related('transactions')
        params = self.request.query_params
        if params.get('type'):
            qs = qs.filter(type=params['type'])
        if params.get('contact'):
            qs = qs.filter(contact_id=params['contact'])
        if params.get('date_from'):
            qs = qs.filter(date__gte=params['date_from'])
        if params.get('date_to'):
            qs = qs.filter(date__lte=params['date_to'])
        if params.get('reference'):
            qs = qs.filter(reference_id=params['reference'])
        return qs

    def get_serializer_class(self):
        return DocumentListSerializer if self.action == 'list' else DocumentSerializer

    def create(self, request, *args, **kwargs):
        doc_type   = request.data.get('type')
        contact_id = request.data.get('contact')
        contact    = Contact.objects.get(pk=contact_id) if contact_id else None
        doc        = process_document_create(doc_type, request.data, contact)
        return Response(DocumentSerializer(doc).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        """
        Document edit:
        - Updates doc fields (notes, date, attachment_urls, etc.)
        - If line_items changed → updates existing record s.txns to match
          (does NOT touch actual s.txns — those are already moved)
        - total_amount change does NOT auto-adjust f.txns (user records payment
          separately — remaining is computed from record vs actual)
        """
        doc        = self.get_object()
        safe_fields = [
            'notes', 'date', 'due_date', 'payment_terms',
            'attachment_urls', 'charges', 'taxes', 'discount',
            'total_amount', 'consignee', 'reference',
        ]
        for field in safe_fields:
            if field in request.data:
                setattr(doc, field, request.data[field])

        new_line_items = request.data.get('line_items')
        if new_line_items is not None:
            doc.line_items = new_line_items
            _sync_record_stxns(doc, new_line_items)

        doc.save()
        return Response(DocumentSerializer(doc).data)

    # ── Record Payment ─────────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def record_payment(self, request, pk=None):
        """
        Records an actual f.txn against a document.
        Blocked for: challan, po, pi, quotation, interest, expense.
        Supports optional interest_lines.
        """
        doc = self.get_object()
        BLOCKED = {'challan', 'po', 'pi', 'quotation', 'interest', 'expense'}
        if doc.type in BLOCKED:
            return Response(
                {'error': f'record_payment not allowed for {doc.type}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        amount_raw     = Decimal(str(request.data['amount']))
        account_id     = request.data.get('payment_account')
        account        = PaymentAccount.objects.get(pk=account_id) if account_id else None
        date           = _parse_date(request.data.get('date'))
        notes          = request.data.get('notes')
        interest_lines = request.data.get('interest_lines', [])

        outgoing   = {'bill', 'cn', 'cash_payment_voucher'}
        direction  = 'send' if doc.type in outgoing else 'receive'
        actual_amt = -amount_raw if direction == 'send' else amount_raw
        result     = {}

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
                contact      = doc.contact,
                line_items   = interest_lines,
                total_amount = abs(net),
                date         = date,
                reference    = doc,
            )
            int_ftxn = _create_ftxn(
                'record', interest_record_amount,
                doc.contact, None, interest_doc, date,
            )
            result['interest_doc']  = interest_doc.pk
            result['interest_ftxn'] = int_ftxn.pk

        ftxn = _create_ftxn('actual', actual_amt, doc.contact, account, doc, date, notes)
        result['ftxn'] = ftxn.pk
        return Response(result, status=status.HTTP_201_CREATED)

    # ── Move Stock ─────────────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def move_stock(self, request, pk=None):
        """
        Only valid for: bill, invoice, cn, dn, challan.
        """
        doc = self.get_object()
        ALLOWED = {'bill', 'invoice', 'cn', 'dn', 'challan'}
        if doc.type not in ALLOWED:
            return Response(
                {'error': f'move_stock not allowed for {doc.type}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        result = process_move_stock(doc, request.data)
        return Response(result, status=status.HTTP_201_CREATED)

    # ── Stock Preview ──────────────────────────────────────────────────────────
    @action(detail=True, methods=['get'])
    def stock_preview(self, request, pk=None):
        doc     = self.get_object()
        records = StockTransaction.objects.filter(
            document=doc, type='record'
        ).select_related('product')

        preview = []
        for r in records:
            moved = sum(
                t.quantity for t in StockTransaction.objects.filter(
                    document=doc, product=r.product, type='actual'
                )
            )
            preview.append({
                'product_id':    r.product_id,
                'product_name':  r.product.name,
                'record_qty':    str(r.quantity),
                'moved_qty':     str(moved),
                'remaining_qty': str(r.quantity - moved),
            })
        return Response(preview)

    # ── Add Details (Fast Bill) ────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def add_details(self, request, pk=None):
        """
        Adds line_items to a fast-created doc.
        Guard: skips products that already have s.txns on this doc.
        """
        doc        = self.get_object()
        settings   = Settings.get()
        line_items = request.data.get('line_items', [])

        doc.line_items = line_items
        if not doc.total_amount:
            doc.total_amount = sum(Decimal(str(i.get('amount', 0))) for i in line_items)
        doc.save(update_fields=['line_items', 'total_amount', 'updated_at'])

        if doc.type == 'challan' and doc.reference:
            sign = CHALLAN_STXN_SIGN.get(doc.reference.type, Decimal('1'))
        else:
            sign = STXN_SIGN.get(doc.type, Decimal('1'))

        for item in line_items:
            pid = item.get('product_id')
            if not pid:
                continue
            try:
                product = Product.objects.get(pk=pid)
            except Product.DoesNotExist:
                continue
            if StockTransaction.objects.filter(document=doc, product=product).exists():
                continue
            qty      = sign * Decimal(str(item.get('quantity', 0)))
            txn_type = 'actual' if settings.auto_stock else 'record'
            _create_stxn(txn_type, qty, product, doc, doc.date, item.get('rate'))

        return Response(DocumentSerializer(doc).data)

    # ── Delete Document ────────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def delete_document(self, request, pk=None):
        doc      = self.get_object()
        strategy = request.data.get('strategy', 'orphan')
        result   = process_document_delete(doc, strategy)
        return Response(result)


def _sync_record_stxns(doc, new_line_items):
    """
    When line_items are edited on a doc, sync the record s.txns.
    - Deletes record s.txns for products removed from line_items
    - Creates record s.txns for newly added products
    - Updates quantity on existing record s.txns
    - Never touches actual s.txns
    """
    if doc.type == 'challan' and doc.reference:
        sign = CHALLAN_STXN_SIGN.get(doc.reference.type, Decimal('1'))
    else:
        sign = STXN_SIGN.get(doc.type, Decimal('1'))

    settings = Settings.get()

    # Build map of product_id → qty from new line_items
    new_map = {}
    for item in new_line_items:
        pid = item.get('product_id')
        if pid:
            new_map[int(pid)] = Decimal(str(item.get('quantity', 0)))

    existing_records = StockTransaction.objects.filter(document=doc, type='record')
    existing_map     = {r.product_id: r for r in existing_records}

    # Remove records for products no longer in line_items
    for pid, stxn in existing_map.items():
        if pid not in new_map:
            stxn.delete()

    # Add or update
    for pid, qty in new_map.items():
        signed_qty = sign * qty
        if pid in existing_map:
            stxn = existing_map[pid]
            if stxn.quantity != signed_qty:
                stxn.quantity = signed_qty
                stxn.save(update_fields=['quantity'])
        else:
            try:
                product = Product.objects.get(pk=pid)
            except Product.DoesNotExist:
                continue
            _create_stxn('record', signed_qty, product, doc, doc.date)


# ─── FinancialTransaction ViewSet ──────────────────────────────────────────────

class FinancialTransactionViewSet(viewsets.ModelViewSet):
    serializer_class = FinancialTransactionSerializer
    search_fields    = ['contact__contact_name', 'contact__company_name', 'notes']
    ordering_fields  = ['date', 'amount', 'created_at']
    ordering         = ['date', 'created_at']

    def get_queryset(self):
        qs     = FinancialTransaction.objects.all()
        params = self.request.query_params
        settings = Settings.get()

        # Auto-mode ON → hide record txns (user never sees them)
        if settings.auto_transaction and not params.get('include_records'):
            qs = qs.exclude(type='record')

        if params.get('contact'):
            qs = qs.filter(contact_id=params['contact'])
        if params.get('account'):
            qs = qs.filter(payment_account_id=params['account'])
        if params.get('type'):
            qs = qs.filter(type=params['type'])
        if params.get('document'):
            qs = qs.filter(document_id=params['document'])
        if params.get('date_from'):
            qs = qs.filter(date__gte=params['date_from'])
        if params.get('date_to'):
            qs = qs.filter(date__lte=params['date_to'])
        if params.get('is_doc_deleted') is not None:
            qs = qs.filter(is_doc_deleted=params['is_doc_deleted'].lower() == 'true')
        return qs

    def update(self, request, *args, **kwargs):
        """
        Only actual type transactions can be edited.
        Record txns are managed via document edit.
        Contra txns are managed via transfer endpoint.
        """
        ftxn = self.get_object()
        if ftxn.type != 'actual':
            return Response(
                {'error': 'Only actual transactions can be edited directly.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        old_date = ftxn.date

        if 'amount' in request.data:
            new_amt = Decimal(str(request.data['amount']))
            if ftxn.payment_account:
                ftxn.payment_account.current_balance -= ftxn.amount
                ftxn.payment_account.current_balance += new_amt
                ftxn.payment_account.save(update_fields=['current_balance'])
            ftxn.amount = new_amt

        if 'date' in request.data:
            ftxn.date = _parse_date(request.data['date'])

        if 'payment_account' in request.data:
            acct_id = request.data['payment_account']
            if ftxn.payment_account:
                ftxn.payment_account.current_balance -= ftxn.amount
                ftxn.payment_account.save(update_fields=['current_balance'])
            new_acct = PaymentAccount.objects.get(pk=acct_id) if acct_id else None
            if new_acct:
                new_acct.current_balance += ftxn.amount
                new_acct.save(update_fields=['current_balance'])
            ftxn.payment_account = new_acct

        if 'notes' in request.data:
            ftxn.notes = request.data['notes']

        ftxn.save()
        _recalculate_mcd(ftxn.contact, ftxn.date)
        if old_date.month != ftxn.date.month or old_date.year != ftxn.date.year:
            _recalculate_mcd(ftxn.contact, old_date)

        return Response(FinancialTransactionSerializer(ftxn).data)

    def destroy(self, request, *args, **kwargs):
        """
        Only actual transactions can be deleted directly.
        Record txns are deleted via document deletion flow.
        """
        ftxn = self.get_object()
        if ftxn.type == 'record':
            return Response(
                {'error': 'Record transactions are managed via document deletion.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        date    = ftxn.date
        contact = ftxn.contact

        if ftxn.type == 'actual' and ftxn.payment_account:
            ftxn.payment_account.current_balance -= ftxn.amount
            ftxn.payment_account.save(update_fields=['current_balance'])

        ftxn.delete()
        _recalculate_mcd(contact, date)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'])
    def link_document(self, request, pk=None):
        ftxn             = self.get_object()
        ftxn.document_id = request.data.get('document')
        ftxn.save(update_fields=['document'])
        return Response(FinancialTransactionSerializer(ftxn).data)
