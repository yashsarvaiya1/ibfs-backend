from decimal import Decimal
from django.db import models as django_models
from django.http import HttpResponse
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
    generate_document_pdf, generate_transactions_pdf,
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
        qs     = Document.objects.filter(is_active=True).prefetch_related(
            'transactions', 'transactions__document'
        )
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

    def get_serializer_context(self):
        return {'request': self.request}

    def create(self, request, *args, **kwargs):
        doc_type = request.data.get('type')
        BLOCKED_DIRECT = {'cash_payment_voucher', 'cash_receipt_voucher'}
        if doc_type in BLOCKED_DIRECT:
            return Response(
                {'error': f'{doc_type} can only be created via Send/Receive flow.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        contact_id = request.data.get('contact')
        contact    = Contact.objects.get(pk=contact_id) if contact_id else None
        doc        = process_document_create(doc_type, request.data, contact)
        return Response(
            DocumentSerializer(doc, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )

    def update(self, request, *args, **kwargs):
        doc = self.get_object()

        old_total_amount = Decimal(str(doc.total_amount)) if doc.total_amount is not None else None
        old_date         = doc.date

        # ── Simple scalar fields ───────────────────────────────────────────────
        simple_fields = [
            'notes', 'payment_terms', 'attachment_urls',
            'charges', 'taxes', 'discount', 'total_amount',
        ]
        for field in simple_fields:
            if field in request.data:
                setattr(doc, field, request.data[field])

        # ── FK fields — must use _id suffix, never assign raw int directly ────
        if 'consignee' in request.data:
            doc.consignee_id = request.data['consignee']   # None or int → both valid
        if 'reference' in request.data:
            doc.reference_id = request.data['reference']   # None or int → both valid

        # ── Date fields — always parse to avoid string/date type mismatch ─────
        if 'date' in request.data:
            doc.date = _parse_date(request.data['date'])
        if 'due_date' in request.data:
            raw_due = request.data['due_date']
            doc.due_date = _parse_date(raw_due) if raw_due else None

        # ── Line items → sync record s.txns ───────────────────────────────────
        new_line_items = request.data.get('line_items')
        if new_line_items is not None:
            doc.line_items = new_line_items
            _sync_record_stxns(doc, new_line_items)

        doc.save()

        _sync_record_ftxns(doc, old_total_amount, old_date)

        return Response(DocumentSerializer(doc, context={'request': request}).data)

    # ── Record Payment ─────────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def record_payment(self, request, pk=None):
        """
        Records an actual f.txn against a document (Path B in spec).
        Supports optional interest_lines.
        Per spec: always available for bill/invoice/cn/dn regardless of auto_transaction setting.
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

        outgoing   = {'bill', 'dn', 'cash_payment_voucher'}
        direction  = 'send' if doc.type in outgoing else 'receive'
        actual_amt = -amount_raw if direction == 'send' else amount_raw
        result     = {}

        # ── Interest record FIRST ──────────────────────────────────────────────
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

        # ── Main actual SECOND ─────────────────────────────────────────────────
        ftxn           = _create_ftxn('actual', actual_amt, doc.contact, account, doc, date, notes)
        result['ftxn'] = ftxn.pk
        return Response(result, status=status.HTTP_201_CREATED)

    # ── Move Stock ─────────────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def move_stock(self, request, pk=None):
        """
        Creates actual s.txns for a document.
        Per spec 6.1 — from Document Page Move Stock button.
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
        """
        Per spec 6.1: Stock Preview Panel — only product_id items shown.
        Manual/service items (product_id=null) completely hidden.
        All quantities returned as positive absolute values — sign is
        irrelevant for display (direction is implicit from doc type).
        """
        doc     = self.get_object()
        records = StockTransaction.objects.filter(
            document=doc, type='record'
        ).select_related('product')

        preview = []
        for r in records:
            record_qty = abs(r.quantity)                          # ← abs() here
            moved      = abs(sum(
                t.quantity for t in StockTransaction.objects.filter(
                    document=doc, product=r.product, type='actual'
                )
            ))                                                    # ← abs() here
            remaining  = record_qty - moved

            preview.append({
                'product_id':    r.product_id,
                'product_name':  r.product.name,
                'record_qty':    str(record_qty),
                'moved_qty':     str(moved),
                'remaining_qty': str(max(remaining, 0)),          # ← floor at 0, never negative
            })
        return Response(preview)


    # ── Add Details (Fast Bill / Fast Invoice) ─────────────────────────────────
    @action(detail=True, methods=['post'])
    def add_details(self, request, pk=None):
        """
        Per spec G3: Adds line_items to a fast-created doc.
        Silently creates record s.txns for product_id items — makes Move Stock appear.
        Guard: skips products that already have s.txns on this doc (idempotent).
        """
        doc        = self.get_object()
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
            qty = sign * Decimal(str(item.get('quantity', 0)))
            _create_stxn('record', qty, product, doc, doc.date, item.get('rate'))

        return Response(DocumentSerializer(doc, context={'request': request}).data)

    # ── Reference Data ─────────────────────────────────────────────────────────
    @action(detail=True, methods=['get'])
    def reference_data(self, request, pk=None):
        """
        Returns fields that get auto-copied when this doc is selected as reference.
        Per spec Part 2: line_items, charges, taxes, consignee, discount, payment_terms, notes.
        contact is NOT returned — user selects independently.
        """
        doc = self.get_object()
        return Response({
            'line_items':    doc.line_items,
            'charges':       doc.charges,
            'taxes':         doc.taxes,
            'consignee':     doc.consignee_id,
            'discount':      str(doc.discount),
            'payment_terms': doc.payment_terms,
            'notes':         doc.notes,
        })

    # ── Delete Document ────────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def delete_document(self, request, pk=None):
        """
        Per spec Part 5 — exactly 2 strategies:
          'revert' → hard delete all txns, reverse balances and stock
          'manual' → keep actual txns intact with FK pointing to soft-deleted doc
        No third option exists.
        """
        doc      = self.get_object()
        strategy = request.data.get('strategy', 'revert')
        if strategy not in ('revert', 'manual'):
            return Response(
                {'error': "strategy must be 'revert' or 'manual'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        result = process_document_delete(doc, strategy)
        return Response(result)

    # ── Standalone Interest (Path C) ───────────────────────────────────────────
    @action(detail=False, methods=['post'])
    def standalone_interest(self, request):
        """
        Quick Action → Interest (Path C).
        Requires enable_interest = True.
        Creates Interest Document + record f.txn only. No actual. No s.txn.
        Charge → − (they owe us more). Credit → + (we owe them / waiving debt).
        """
        settings = Settings.get()
        if not settings.enable_interest:
            return Response(
                {'error': 'enable_interest is disabled.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        contact_id     = request.data.get('contact')
        contact        = Contact.objects.get(pk=contact_id) if contact_id else None
        date           = _parse_date(request.data.get('date'))
        interest_lines = request.data.get('line_items', [])
        toggle         = request.data.get('toggle', 'charge')  # 'charge' or 'credit'

        net           = sum(Decimal(str(l['amount'])) for l in interest_lines)
        record_amount = -net if toggle == 'charge' else net

        interest_doc = Document.objects.create(
            type         = 'interest',
            doc_id       = _next_doc_id('interest'),
            contact      = contact,
            line_items   = interest_lines,
            total_amount = net,
            date         = date,
        )
        ftxn = _create_ftxn('record', record_amount, contact, None, interest_doc, date)
        return Response({
            'interest_doc': interest_doc.pk,
            'ftxn':         ftxn.pk,
        }, status=status.HTTP_201_CREATED)

    # ── Print Document PDF ─────────────────────────────────────────────────────
    @action(detail=True, methods=['get'])
    def print(self, request, pk=None):
        """
        GET /api/documents/{id}/print/
        Generates document PDF on-the-fly, cached 10 min per spec Part 7.
        Returns application/pdf with Content-Disposition: attachment.
        File naming: {DOC_TYPE}_{doc_id}_{date}.pdf
        """
        doc = self.get_object()
        try:
            pdf_bytes, filename = generate_document_pdf(doc, request)
        except Exception as e:
            return Response(
                {'error': f'PDF generation failed: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


# ─── _sync_record_stxns (module-level helper) ─────────────────────────────────


def _sync_record_stxns(doc, new_line_items):
    """
    When line_items are edited on a doc, sync the record s.txns to match.
    - Deletes record s.txns for products removed from line_items
    - Creates record s.txns for newly added products
    - Updates quantity on existing record s.txns if quantity changed
    - NEVER touches actual s.txns
    """
    if doc.type == 'challan' and doc.reference:
        sign = CHALLAN_STXN_SIGN.get(doc.reference.type, Decimal('1'))
    else:
        sign = STXN_SIGN.get(doc.type, Decimal('1'))

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
                stxn.save(update_fields=['quantity', 'updated_at'])
        else:
            try:
                product = Product.objects.get(pk=pid)
            except Product.DoesNotExist:
                continue
            _create_stxn('record', signed_qty, product, doc, doc.date)


# ─── _sync_record_ftxns (module-level helper) ─────────────────────────────────


def _sync_record_ftxns(doc, old_total_amount, old_date):
    """
    When total_amount or date changes on a document, sync the record f.txns.
    - Preserves sign on amount change (absolute value only updated)
    - Updates date and triggers MCD recalculation for affected month(s)
    - NEVER touches actual f.txns

    Preconditions (enforced by caller):
      old_total_amount → Decimal or None  (normalized before calling)
      old_date         → date object      (captured before mutation)
      doc.date         → date object      (parsed via _parse_date in update())
      doc.total_amount → raw value from setattr (Decimal-cast safely below)
    """
    try:
        new_total_amount = Decimal(str(doc.total_amount)) if doc.total_amount is not None else None
    except Exception:
        new_total_amount = None

    amount_changed = (
        old_total_amount is not None
        and new_total_amount is not None
        and old_total_amount != new_total_amount
    )
    date_changed = (old_date != doc.date)

    if not (amount_changed or date_changed):
        return

    record_ftxns = FinancialTransaction.objects.filter(document=doc, type='record')

    for ftxn in record_ftxns:
        update_fields = ['updated_at']

        if amount_changed and new_total_amount is not None:
            # Preserve sign — only change absolute value
            sign            = Decimal('1') if ftxn.amount >= 0 else Decimal('-1')
            new_ftxn_amount = sign * new_total_amount
            if ftxn.amount != new_ftxn_amount:
                ftxn.amount = new_ftxn_amount
                update_fields.append('amount')

        if date_changed:
            ftxn.date = doc.date
            update_fields.append('date')

        if len(update_fields) > 1:
            ftxn.save(update_fields=update_fields)

    # MCD recalculation — always recalculate new month
    if doc.contact_id:
        _recalculate_mcd(doc.contact, doc.date)
        # Also recalculate old month if it crossed a calendar month boundary
        if date_changed and (
            old_date.month != doc.date.month
            or old_date.year != doc.date.year
        ):
            _recalculate_mcd(doc.contact, old_date)


# ─── FinancialTransaction ViewSet ──────────────────────────────────────────────


class FinancialTransactionViewSet(viewsets.ModelViewSet):
    serializer_class = FinancialTransactionSerializer
    search_fields    = ['contact__contact_name', 'contact__company_name', 'notes']
    ordering_fields  = ['date', 'amount', 'created_at']
    ordering         = ['date', 'created_at']

    def get_serializer_context(self):
        return {'request': self.request}

    def get_queryset(self):
        qs       = FinancialTransaction.objects.select_related(
            'document', 'contact', 'payment_account'
        ).all()
        params   = self.request.query_params
        settings = Settings.get()

        # Auto-mode ON → hide record txns from normal listing
        # Pass ?include_records=true to see them (admin/debug)
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

        is_doc_deleted = params.get('is_document_deleted')
        if is_doc_deleted is not None:
            if is_doc_deleted.lower() == 'true':
                qs = qs.filter(document__isnull=False, document__is_active=False)
            else:
                qs = qs.filter(
                    django_models.Q(document__isnull=True) |
                    django_models.Q(document__is_active=True)
                )

        return qs

    def update(self, request, *args, **kwargs):
        """
        Only actual type transactions can be edited directly.
        Record txns → managed via document edit (_sync_record_ftxns).
        Contra txns → managed via transfer endpoint.
        On amount change: reverses old account balance, applies new.
        On date change: recalculates MCD for both old and new month if they differ.
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
                ftxn.payment_account.save(update_fields=['current_balance', 'updated_at'])
            ftxn.amount = new_amt

        if 'date' in request.data:
            ftxn.date = _parse_date(request.data['date'])

        if 'payment_account' in request.data:
            acct_id = request.data['payment_account']
            if ftxn.payment_account:
                ftxn.payment_account.current_balance -= ftxn.amount
                ftxn.payment_account.save(update_fields=['current_balance', 'updated_at'])
            new_acct = PaymentAccount.objects.get(pk=acct_id) if acct_id else None
            if new_acct:
                new_acct.current_balance += ftxn.amount
                new_acct.save(update_fields=['current_balance', 'updated_at'])
            ftxn.payment_account = new_acct

        if 'notes' in request.data:
            ftxn.notes = request.data['notes']

        ftxn.save()

        _recalculate_mcd(ftxn.contact, ftxn.date)
        if old_date.month != ftxn.date.month or old_date.year != ftxn.date.year:
            _recalculate_mcd(ftxn.contact, old_date)

        return Response(FinancialTransactionSerializer(ftxn, context={'request': request}).data)

    def destroy(self, request, *args, **kwargs):
        """
        Only actual transactions can be deleted directly.
        Record txns → deleted via document deletion flow only.
        Contra txns → managed via transfer operations.
        Reverses PaymentAccount balance and recalculates MCD.
        """
        ftxn = self.get_object()
        if ftxn.type == 'record':
            return Response(
                {'error': 'Record transactions are managed via document deletion.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if ftxn.type == 'contra':
            return Response(
                {'error': 'Contra transactions are managed via transfer operations.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        date    = ftxn.date
        contact = ftxn.contact

        if ftxn.payment_account:
            ftxn.payment_account.current_balance -= ftxn.amount
            ftxn.payment_account.save(update_fields=['current_balance', 'updated_at'])

        ftxn.delete()
        _recalculate_mcd(contact, date)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'])
    def link_document(self, request, pk=None):
        """Allows user to manually link/unlink a document reference on a transaction."""
        ftxn             = self.get_object()
        ftxn.document_id = request.data.get('document')
        ftxn.save(update_fields=['document', 'updated_at'])
        return Response(FinancialTransactionSerializer(ftxn, context={'request': request}).data)

    # ── Print Transactions PDF ─────────────────────────────────────────────────
    @action(detail=False, methods=['get'])
    def print(self, request):
        """
        GET /api/transactions/print/
        Supports all same filters as list view:
            ?contact={id}        → prints as Ledger for that contact (with running CF)
            ?date_from / ?date_to
            ?account={id}
            ?type=actual|record|contra
            ?document={id}
        Returns application/pdf with Content-Disposition: attachment.
        Per spec Part 7: Print button on ledger & list view.
        """
        qs = self.filter_queryset(self.get_queryset())

        contact    = None
        contact_id = request.query_params.get('contact')
        if contact_id:
            try:
                contact = Contact.objects.get(pk=contact_id)
            except Contact.DoesNotExist:
                pass

        try:
            pdf_bytes, filename = generate_transactions_pdf(qs, contact, request)
        except Exception as e:
            return Response(
                {'error': f'PDF generation failed: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
