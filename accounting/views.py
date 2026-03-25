# accounting/views.py
from decimal import Decimal
from django.db import models as django_models
from django.db.models import Q, Sum
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
    _parse_date, _recalculate_mcd, compute_opening_balance_for_print,
    process_document_create, process_document_delete, process_move_stock,
    generate_document_pdf, generate_bulk_documents_pdf,
    generate_transactions_pdf,
    STXN_SIGN, CHALLAN_STXN_SIGN,process_standalone_interest,_sync_ftxn_contact, 
)
from shared.models import Contact, PaymentAccount, Settings
from inventory.models import StockTransaction, Product
from django.db import transaction

HAS_BALANCE_TYPES = {'bill', 'invoice', 'cn', 'dn'}


# ─── Document ViewSet ─────────────────────────────────────────────────────────


class DocumentViewSet(viewsets.ModelViewSet):
    search_fields   = ['doc_id', 'contact__contact_name', 'contact__company_name']
    ordering_fields = ['date', 'created_at', 'total_amount']
    ordering        = ['-date', '-created_at']


    def get_queryset(self):
        qs = (
            Document.objects
            .select_related('contact')          # ← remove .filter(is_active=True)
            .prefetch_related('transactions')
        )
        params = self.request.query_params

        # ── is_active — deleted filter ────────────────────────────────────────
        raw_active = params.get('is_active')
        if raw_active not in (None, ''):
            qs = qs.filter(is_active=raw_active.lower() == 'true')
        else:
            qs = qs.filter(is_active=True)      # default: hide deleted

        # ── Multi-type: ?type=bill,invoice OR ?type=bill ──────────────────────
        if params.get('type') not in (None, ''):
            raw   = params['type']
            types = [t.strip() for t in raw.split(',') if t.strip()]
            if len(types) == 1:
                qs = qs.filter(type=types[0])
            else:
                qs = qs.filter(type__in=types)

        if params.get('contact') not in (None, ''):
            qs = qs.filter(contact_id=params['contact'])
        if params.get('date_from') not in (None, ''):
            qs = qs.filter(date__gte=params['date_from'])
        if params.get('date_to') not in (None, ''):
            qs = qs.filter(date__lte=params['date_to'])
        if params.get('reference') not in (None, ''):
            qs = qs.filter(reference_id=params['reference'])

        # ── BF-08: is_paid — restrict to balance-carrying types only ─────────
        if params.get('is_paid') not in (None, ''):
            want_paid = params['is_paid'].lower() == 'true'
            qs = qs.filter(type__in=HAS_BALANCE_TYPES)

            from decimal import Decimal

            def _is_txn_settled(doc) -> bool:
                txns   = doc.transactions.all()
                record = abs(sum(t.amount for t in txns if t.type == 'record'))
                paid   = abs(sum(t.amount for t in txns if t.type == 'actual'))
                return record > 0 and paid >= record

            if want_paid:
                matched_ids = [doc.pk for doc in qs if doc.is_paid or _is_txn_settled(doc)]
            else:
                matched_ids = [doc.pk for doc in qs if not doc.is_paid and not _is_txn_settled(doc)]

            qs = Document.objects.filter(pk__in=matched_ids)

        if params.get('is_due') not in (None, ''):
            if params['is_due'].lower() == 'true':
                from django.utils import timezone
                today = timezone.localdate()
                qs = qs.filter(
                    type__in=HAS_BALANCE_TYPES,
                    due_date__isnull=False,
                    due_date__lt=today,
                    is_paid=False,
                )

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


    @transaction.atomic
    def update(self, request, *args, **kwargs):
        doc              = self.get_object()
        old_total_amount = Decimal(str(doc.total_amount)) if doc.total_amount is not None else None
        old_date         = doc.date
        old_contact      = doc.contact      # ← capture before any mutation

        # DI-01: editable doc_id — validate uniqueness before saving
        if 'doc_id' in request.data:
            new_doc_id = request.data['doc_id']
            if new_doc_id != doc.doc_id:
                if Document.objects.filter(doc_id=new_doc_id).exclude(pk=doc.pk).exists():
                    return Response(
                        {
                            'error':    f'Document ID "{new_doc_id}" already exists.',
                            'conflict': True,
                        },
                        status=status.HTTP_409_CONFLICT,
                    )
                doc.doc_id = new_doc_id

        simple_fields = [
            'notes', 'payment_terms', 'attachment_urls',
            'charges', 'taxes', 'discount', 'total_amount',
        ]
        for field in simple_fields:
            if field in request.data:
                setattr(doc, field, request.data[field])

        # ← contact change: resolve to object (or None if cleared)
        if 'contact' in request.data:
            contact_id  = request.data['contact']
            doc.contact = Contact.objects.get(pk=contact_id) if contact_id else None

        if 'consignee' in request.data:
            doc.consignee_id = request.data['consignee']
        if 'reference' in request.data:
            doc.reference_id = request.data['reference']

        if 'date' in request.data:
            doc.date = _parse_date(request.data['date'])
        if 'due_date' in request.data:
            raw_due      = request.data['due_date']
            doc.due_date = _parse_date(raw_due) if raw_due else None

        new_line_items = request.data.get('line_items')
        if new_line_items is not None:
            doc.line_items = new_line_items
            _sync_record_stxns(doc, new_line_items)

        doc.save()
        _sync_record_ftxns(doc, old_total_amount, old_date)
        _sync_ftxn_contact(doc, old_contact)    # ← propagates contact change + MCD

        return Response(DocumentSerializer(doc, context={'request': request}).data)


    # ── Mark Paid ─────────────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def mark_paid(self, request, pk=None):
        doc     = self.get_object()
        ALLOWED = {'bill', 'invoice', 'cn', 'dn'}
        if doc.type not in ALLOWED:
            return Response(
                {'error': f'mark_paid not supported for {doc.type}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        doc.is_paid = bool(request.data['is_paid']) if 'is_paid' in request.data else not doc.is_paid
        doc.save(update_fields=['is_paid', 'updated_at'])
        return Response(DocumentSerializer(doc, context={'request': request}).data)


    # ── Record Payment ────────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def record_payment(self, request, pk=None):
        doc     = self.get_object()
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

        with transaction.atomic():
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

            ftxn           = _create_ftxn('actual', actual_amt, doc.contact, account, doc, date, notes)
            result['ftxn'] = ftxn.pk

            # BF-04: auto mark paid when actuals >= records
            MARK_PAID_TYPES = {'bill', 'invoice', 'cn', 'dn'}
            if doc.type in MARK_PAID_TYPES and not doc.is_paid:
                agg    = doc.transactions.aggregate(
                    record_total=Sum('amount', filter=django_models.Q(type='record')),
                    actual_total=Sum('amount', filter=django_models.Q(type='actual')),
                )
                record = abs(agg['record_total'] or Decimal('0'))
                paid   = abs(agg['actual_total'] or Decimal('0'))
                if record > 0 and paid >= record:
                    doc.is_paid = True
                    doc.save(update_fields=['is_paid', 'updated_at'])
                    result['is_paid'] = True

        return Response(result, status=status.HTTP_201_CREATED)


        # BF-04
        MARK_PAID_TYPES = {'bill', 'invoice', 'cn', 'dn'}
        if doc.type in MARK_PAID_TYPES and not doc.is_paid:
            agg    = doc.transactions.aggregate(
                record_total=Sum('amount', filter=django_models.Q(type='record')),
                actual_total=Sum('amount', filter=django_models.Q(type='actual')),
            )
            record = abs(agg['record_total'] or Decimal('0'))
            paid   = abs(agg['actual_total'] or Decimal('0'))
            if record > 0 and paid >= record:
                doc.is_paid = True
                doc.save(update_fields=['is_paid', 'updated_at'])
                result['is_paid'] = True

        return Response(result, status=status.HTTP_201_CREATED)


    # ── Move Stock ────────────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def move_stock(self, request, pk=None):
        doc     = self.get_object()
        ALLOWED = {'bill', 'invoice', 'cn', 'dn', 'challan'}
        if doc.type not in ALLOWED:
            return Response(
                {'error': f'move_stock not allowed for {doc.type}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        result = process_move_stock(doc, request.data)
        return Response(result, status=status.HTTP_201_CREATED)


    # ── Stock Preview ─────────────────────────────────────────────────────────
    @action(detail=True, methods=['get'])
    def stock_preview(self, request, pk=None):
        doc     = self.get_object()
        records = StockTransaction.objects.filter(
            document=doc, type='record'
        ).select_related('product')

        if not records.exists():
            return Response([])

        actuals_map = {
            a['product_id']: abs(a['total'] or Decimal('0'))
            for a in StockTransaction.objects.filter(
                document=doc, type='actual'
            ).values('product_id').annotate(total=Sum('quantity'))
        }

        preview = []
        for r in records:
            record_qty = abs(r.quantity)
            moved      = actuals_map.get(r.product_id, Decimal('0'))
            remaining  = record_qty - moved
            preview.append({
                'product_id':    r.product_id,
                'product_name':  r.product.name,
                'record_qty':    str(record_qty),
                'moved_qty':     str(moved),
                'remaining_qty': str(max(remaining, Decimal('0'))),
            })
        return Response(preview)


    # ── Add Details ───────────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def add_details(self, request, pk=None):
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


    # ── Reference Data ────────────────────────────────────────────────────────
    @action(detail=True, methods=['get'])
    def reference_data(self, request, pk=None):
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


    # ── Delete Document ───────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def delete_document(self, request, pk=None):
        doc      = self.get_object()
        strategy = request.data.get('strategy', 'revert')
        if strategy not in ('revert', 'manual'):
            return Response(
                {'error': "strategy must be 'revert' or 'manual'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        result = process_document_delete(doc, strategy)
        return Response(result)


    # ── Standalone Interest (Path C) ──────────────────────────────────────────
    @action(detail=False, methods=['post'])
    def standalone_interest(self, request):
        app_settings = Settings.get()
        if not app_settings.enable_interest:
            return Response(
                {'error': 'enable_interest is disabled.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        contact_id = request.data.get('contact')
        contact    = Contact.objects.get(pk=contact_id) if contact_id else None

        result = process_standalone_interest(contact, request.data)
        return Response(result, status=status.HTTP_201_CREATED)


    # ── Print Single Document PDF ─────────────────────────────────────────────
    @action(detail=True, methods=['get'])
    def print(self, request, pk=None):
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


    # ── Bulk Print ────────────────────────────────────────────────────────────
    @action(detail=False, methods=['post'])
    def bulk_print(self, request):
        ids = request.data.get('ids', [])

        if ids:
            docs = (
                Document.objects
                .filter(pk__in=ids, is_active=True)
                .select_related('contact', 'consignee')
                .order_by('-date', '-created_at')
            )
        else:
            docs = (
                self.filter_queryset(self.get_queryset())
                .select_related('contact', 'consignee')
            )

        if not docs.exists():
            return Response(
                {'error': 'No documents found for the given selection.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            pdf_bytes, filename = generate_bulk_documents_pdf(list(docs), request)
        except Exception as e:
            return Response(
                {'error': f'Bulk PDF generation failed: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


# ─── _sync_record_stxns ───────────────────────────────────────────────────────


def _sync_record_stxns(doc, new_line_items):
    if doc.type == 'challan' and doc.reference:
        sign = CHALLAN_STXN_SIGN.get(doc.reference.type, Decimal('1'))
    else:
        sign = STXN_SIGN.get(doc.type, Decimal('1'))

    new_map = {
        int(item['product_id']): Decimal(str(item.get('quantity', 0)))
        for item in new_line_items
        if item.get('product_id')
    }

    existing_records = StockTransaction.objects.filter(document=doc, type='record')
    existing_map     = {r.product_id: r for r in existing_records}

    for pid, stxn in existing_map.items():
        if pid not in new_map:
            stxn.delete()

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


# ─── _sync_record_ftxns ───────────────────────────────────────────────────────


def _sync_record_ftxns(doc, old_total_amount, old_date):
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

    if doc.contact_id:
        _recalculate_mcd(doc.contact, doc.date)
        if date_changed and (
            old_date.month != doc.date.month
            or old_date.year != doc.date.year
        ):
            _recalculate_mcd(doc.contact, old_date)


# ─── FinancialTransaction ViewSet ─────────────────────────────────────────────


class FinancialTransactionViewSet(viewsets.ModelViewSet):
    serializer_class = FinancialTransactionSerializer
    search_fields    = ['contact__contact_name', 'contact__company_name', 'notes']
    ordering_fields  = ['date', 'amount', 'created_at']
    ordering         = ['-date', '-created_at']


    def get_serializer_context(self):
        return {'request': self.request}


    def get_queryset(self):
        qs = (
            FinancialTransaction.objects
            .select_related('document', 'contact', 'payment_account')
            .all()
        )
        params       = self.request.query_params
        app_settings = Settings.get()

        if app_settings.auto_transaction and not params.get('include_records'):
            qs = qs.exclude(type='record')

        if params.get('contact') not in (None, ''):
            qs = qs.filter(contact_id=params['contact'])
        if params.get('account') not in (None, ''):
            qs = qs.filter(payment_account_id=params['account'])
        if params.get('document') not in (None, ''):
            qs = qs.filter(document_id=params['document'])
        if params.get('doc_type') not in (None, ''):
            qs = qs.filter(document__type=params['doc_type'])
        if params.get('date_from') not in (None, ''):
            qs = qs.filter(date__gte=params['date_from'])
        if params.get('date_to') not in (None, ''):
            qs = qs.filter(date__lte=params['date_to'])
        if params.get('document_type') not in (None, ''):
            qs = qs.filter(document__type=params['document_type'])

        is_doc_deleted = params.get('is_document_deleted')
        if is_doc_deleted not in (None, ''):
            if is_doc_deleted.lower() == 'true':
                qs = qs.filter(document__isnull=False, document__is_active=False)
            else:
                qs = qs.filter(
                    Q(document__isnull=True) | Q(document__is_active=True)
                )

        # ── Multi-type OR filter ───────────────────────────────────────────────
        # ?types=actual,expense,contra  (UI sends this for multi-select)
        # 'expense' = virtual type: actual txns where document__type='expense'
        # 'actual'  = settled payments excluding expense-linked ones
        # 'record'  = document-linked expected txns
        # 'contra'  = account transfers
        raw_types = params.get('types')
        if raw_types not in (None, ''):
            tokens = [t.strip() for t in raw_types.split(',') if t.strip()]
            q = Q()
            for token in tokens:
                if token == 'expense':
                    q |= Q(type='actual', document__type='expense')
                elif token == 'actual':
                    # Settled = actual but NOT expense-linked
                    q |= Q(type='actual') & ~Q(document__type='expense')
                else:
                    q |= Q(type=token)
            qs = qs.filter(q)

        # ── Single ?type= kept for backward compat (PrintSheet, ledger etc.) ───
        elif params.get('type') not in (None, ''):
            qs = qs.filter(type=params['type'])

        return qs


    def update(self, request, *args, **kwargs):
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
        ftxn             = self.get_object()
        ftxn.document_id = request.data.get('document')
        ftxn.save(update_fields=['document', 'updated_at'])
        return Response(FinancialTransactionSerializer(ftxn, context={'request': request}).data)


    # ── Print Transactions PDF (TV-06) ────────────────────────────────────────
    @action(detail=False, methods=['get'])
    def print(self, request):
        qs        = self.filter_queryset(self.get_queryset())
        view_mode = request.query_params.get('view', 'list')
        is_ledger = view_mode == 'ledger'

        contact    = None
        contact_id = request.query_params.get('contact')
        if contact_id not in (None, ''):
            try:
                contact = Contact.objects.get(pk=contact_id)
            except Contact.DoesNotExist:
                pass

        account    = None
        account_id = request.query_params.get('account')
        if account_id not in (None, ''):
            try:
                account = PaymentAccount.objects.get(pk=account_id)
            except PaymentAccount.DoesNotExist:
                pass

        # ── Parse date_from ───────────────────────────────────────────────────
        date_from = None
        raw_df    = request.query_params.get('date_from')
        if raw_df not in (None, ''):
            try:
                from datetime import date as date_type
                date_from = date_type.fromisoformat(raw_df)
            except Exception:
                pass

        # ── opening_balance_at — contact ledger ───────────────────────────────
        opening_balance_at = None
        raw_oba            = request.query_params.get('opening_balance_at')
        if raw_oba not in (None, ''):
            try:
                opening_balance_at = Decimal(raw_oba)
            except Exception:
                pass
        elif contact and is_ledger:
            opening_balance_at = compute_opening_balance_for_print(contact, date_from)

        # ── balance_before_period — account statement / account ledger ────────
        balance_before_period = None
        raw_bbp               = request.query_params.get('balance_before_period')
        if raw_bbp not in (None, ''):
            try:
                balance_before_period = Decimal(raw_bbp)
            except Exception:
                pass
        elif account:
            if date_from:
                # Balance just before the filtered window
                after_sum = (
                    FinancialTransaction.objects
                    .filter(payment_account=account, date__gte=date_from)
                    .aggregate(total=Sum('amount'))['total'] or Decimal('0')
                )
                balance_before_period = Decimal(str(account.current_balance)) - after_sum
            else:
                # No filter: balance before ALL txns = initial seeded balance
                all_sum = (
                    FinancialTransaction.objects
                    .filter(payment_account=account)
                    .aggregate(total=Sum('amount'))['total'] or Decimal('0')
                )
                balance_before_period = Decimal(str(account.current_balance)) - all_sum

        # For account ledger view, pipe balance_before_period as opening_balance_at
        # so generate_transactions_pdf can seed running_cf correctly
        if is_ledger and account and balance_before_period is not None:
            opening_balance_at = balance_before_period

        try:
            pdf_bytes, filename = generate_transactions_pdf(
                list(qs),
                contact               = contact,
                request               = request,
                opening_balance_at    = opening_balance_at,
                is_ledger_view        = is_ledger,
                account               = account,
                balance_before_period = balance_before_period,
            )
        except Exception as e:
            return Response(
                {'error': f'PDF generation failed: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
