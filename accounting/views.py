# accounting/views.py
from decimal import Decimal
from django.db import models as django_models
from django.db.models import Q, Sum, F, OuterRef, Subquery, DecimalField, Value
from django.db.models.functions import Abs, Coalesce
from django.http import HttpResponse
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Document, FinancialTransaction, PaymentAllocation
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
from .workflows import OUTGOING_TYPES
from .commands import DocumentWriteSerializer, PaymentCommandSerializer, command_data
from .document_updates import update_document

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
            .prefetch_related('transactions__allocations__document', 'payment_allocations__payment__allocations__document', 'payment_allocations__payment__document', 'payment_allocations__payment__payment_account', 'payment_allocations__payment__contact')
        )
        params = self.request.query_params

        # List filters do not hide archived read-only detail/print/history.
        # Mutation actions can never select archived documents using query params.
        raw_active = params.get('is_active')
        if self.action == 'list':
            qs = qs.filter(is_active=raw_active.lower() == 'true' if raw_active not in (None, '') else True)
        elif self.request.method not in ('GET', 'HEAD', 'OPTIONS'):
            qs = qs.filter(is_active=True)

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

        if params.get('is_paid') is not None or params.get('is_due') == 'true' or params.get('payment_status'):
            decimal_field = DecimalField(max_digits=15, decimal_places=2)
            records = FinancialTransaction.objects.filter(document_id=OuterRef('pk'),type='record').values('document_id').annotate(total=Sum('amount')).values('total')
            allocations = PaymentAllocation.objects.filter(document_id=OuterRef('pk')).values('document_id').annotate(total=Sum('amount')).values('total')
            qs = qs.filter(type__in=HAS_BALANCE_TYPES).annotate(
                record_amount=Abs(Coalesce(Subquery(records),Value(0),output_field=decimal_field)),
                paid_amount=Coalesce(Subquery(allocations),Value(0),output_field=decimal_field))
            settled = Q(is_paid=True) | (Q(record_amount__gt=0) & Q(paid_amount__gte=F('record_amount')))
            state = params.get('payment_status')
            if params.get('is_paid') == 'true' or state == 'paid':
                qs = qs.filter(settled)
            elif params.get('is_paid') == 'false' or state in {'unpaid','partial','due'} or params.get('is_due') == 'true':
                qs = qs.exclude(settled)
            if state == 'partial':
                qs = qs.filter(paid_amount__gt=0, paid_amount__lt=F('record_amount'))
            if state == 'due' or params.get('is_due') == 'true':
                from django.utils import timezone
                qs = qs.filter(due_date__lt=timezone.localdate())

        return qs


    def get_serializer_class(self):
        return DocumentListSerializer if self.action == 'list' else DocumentSerializer


    def get_serializer_context(self):
        return {'request': self.request}

    def destroy(self, request, *args, **kwargs):
        strategy = request.data.get('strategy') or request.query_params.get('strategy')
        if strategy not in {'revert', 'manual'}:
            return Response({'error': 'Choose revert or manual when deleting a document.'}, status=400)
        process_document_delete(self.get_object(), strategy)
        return Response(status=204)


    def create(self, request, *args, **kwargs):
        serializer = DocumentWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = command_data(serializer)
        doc_type = data['type']
        if doc_type in {'cash_payment_voucher', 'cash_receipt_voucher', 'interest'}:
            return Response({'error': 'Use the payment or interest action to create this document.'}, status=400)
        feature = {'po':'enable_po', 'pi':'enable_pi', 'quotation':'enable_quotation',
                   'challan':'enable_challan', 'cn':'enable_cn', 'dn':'enable_dn'}.get(doc_type)
        if feature and not getattr(Settings.get(), feature):
            return Response({'error': 'Enable this document type in Settings first.'}, status=400)
        contact = serializer.validated_data.get('contact')
        doc = process_document_create(doc_type, data, contact)
        return Response(DocumentSerializer(doc, context={'request': request}).data, status=201)

    def update(self, request, *args, **kwargs):
        doc = update_document(self.get_object(), request.data)
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

        serializer = PaymentCommandSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = command_data(serializer)
        amount_raw = data['amount']
        account = serializer.validated_data.get('payment_account')
        date = _parse_date(data.get('date'))
        notes = data.get('notes')
        interest_lines = data.get('interest_lines', [])

        direction  = 'send' if doc.type in OUTGOING_TYPES else 'receive'
        actual_amt = -amount_raw if direction == 'send' else amount_raw
        result     = {}

        from .payments import allocate_payment, payment_status
        with transaction.atomic():
            doc = Document.objects.select_for_update().get(pk=doc.pk)
            interest_doc = None
            net = Decimal('0')
            if interest_lines:
                net = sum((Decimal(str(line['amount'])) * (1 if line.get('type') == 'charge' else -1) for line in interest_lines), Decimal('0'))
                interest_doc = Document.objects.create(type='interest', doc_id=_next_doc_id('interest'),
                    contact=doc.contact, line_items=interest_lines, total_amount=abs(net), date=date, reference=doc)
                _create_ftxn('record', -net if direction == 'receive' else net,
                    doc.contact, None, interest_doc, date)
                result['interest_doc'] = interest_doc.pk
            ftxn = _create_ftxn('actual', actual_amt, doc.contact, account, doc, date, notes, auto_allocate=False)
            allocate_payment(ftxn, doc, max(amount_raw-net, Decimal('0')))
            if interest_doc:
                allocate_payment(ftxn, interest_doc, min(net, amount_raw))
            result['ftxn'] = ftxn.pk
            result['is_paid'] = bool((payment_status(doc) or {}).get('is_paid'))
        return Response(result, status=201)


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
        from .stock_status import document_stock_status
        return Response(document_stock_status(self.get_object()))

    # ── Add Details ───────────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def add_details(self, request, pk=None):
        doc = update_document(self.get_object(), {'line_items':request.data.get('line_items', [])}, preserve_total=True)
        return Response(DocumentSerializer(doc, context={'request': request}).data)

    @action(detail=True, methods=['get'])
    def history(self, request, pk=None):
        from .reports import CADocumentPagination
        document = self.get_object()
        pagination = CADocumentPagination()
        if request.query_params.get('revision'):
            from django.shortcuts import get_object_or_404
            revision_id = serializers.IntegerField(min_value=1).run_validation(request.query_params['revision'])
            row = get_object_or_404(document.revisions, pk=revision_id)
            return Response({'id': row.pk, 'recorded_at': row.recorded_at, 'event': row.event, 'snapshot': row.snapshot})
        query = document.revisions.values('id', 'recorded_at', 'event', 'changed_fields', 'snapshot__doc_id', 'snapshot__date', 'snapshot__total_amount')
        rows = pagination.paginate_queryset(query, request, view=self)
        return pagination.get_paginated_response([{'id': row['id'], 'recorded_at': row['recorded_at'],
            'event': row['event'], 'changed_fields': row['changed_fields'], 'doc_id': row['snapshot__doc_id'],
            'date': row['snapshot__date'], 'total_amount': row['snapshot__total_amount']} for row in rows])

    @action(detail=False, methods=['post'])
    def preview_totals(self, request):
        from .commands import DocumentWriteSerializer, command_data
        from .calculations import document_totals
        serializer = DocumentWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(document_totals(command_data(serializer), request.data.get('type')))

    # ── Reference Data ────────────────────────────────────────────────────────
    @action(detail=True, methods=['get'])
    def reference_data(self, request, pk=None):
        doc = self.get_object()
        return Response({
            'line_items':    doc.line_items,
            'charges':       doc.charges,
            'taxes':         doc.taxes,
            'tax_mode': doc.tax_mode,
            'supply_category': doc.supply_category,
            'supplier_invoice_number': doc.supplier_invoice_number,
            'consignee':     doc.consignee_id,
            'discount':      str(doc.discount),
            'discount_percentage': str(doc.discount_percentage) if doc.discount_percentage is not None else None,
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
        from .commands import StandaloneInterestSerializer
        serializer=StandaloneInterestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data=command_data(serializer)
        result = process_standalone_interest(serializer.validated_data.get('contact'), data)
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


# ─── FinancialTransaction ViewSet ─────────────────────────────────────────────


class FinancialTransactionViewSet(viewsets.ModelViewSet):
    serializer_class = FinancialTransactionSerializer
    search_fields    = ['contact__contact_name', 'contact__company_name', 'notes']
    ordering_fields  = ['date', 'amount', 'created_at']
    ordering         = ['-date', '-created_at']

    def create(self, request, *args, **kwargs):
        from rest_framework.exceptions import ValidationError
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data['type'] != 'actual':
            raise ValidationError({'type': 'Use document creation or account transfer for this transaction type.'})
        ftxn = _create_ftxn('actual', data['amount'], data.get('contact'),
            data.get('payment_account'), data.get('document'), data['date'], data.get('notes'))
        return Response(self.get_serializer(ftxn).data, status=201)

    @action(detail=True, methods=['post'])
    @transaction.atomic
    def allocate(self, request, pk=None):
        from rest_framework.exceptions import ValidationError
        from .payments import replace_allocations
        payment = self.get_object()
        if payment.type != 'actual' or (payment.document and payment.document.type == 'expense'):
            raise ValidationError({'allocations':'Only ordinary payments can be allocated.'})
        entries = request.data.get('allocations', [])
        if not isinstance(entries, list) or any(not isinstance(row, dict) or not isinstance(row.get('document'), int) for row in entries):
            raise ValidationError({'allocations':'Choose documents and amounts.'})
        contact_ids = set(Document.objects.filter(pk__in=[row['document'] for row in entries]).values_list('contact_id', flat=True))
        contact_ids.add(payment.contact_id)
        list(Contact.objects.select_for_update().filter(pk__in=[pk for pk in contact_ids if pk]).order_by('pk'))
        if payment.document_id:
            Document.objects.select_for_update().get(pk=payment.document_id)
        payment = FinancialTransaction.objects.select_for_update().get(pk=payment.pk)
        replace_allocations(payment, entries)
        return Response(self.get_serializer(payment).data)


    @action(detail=True,methods=['post'])
    def reverse_transfer(self,request,pk=None):
        from .services import reverse_transfer
        return Response(reverse_transfer(self.get_object()))

    def get_serializer_context(self):
        return {'request': self.request}


    def get_queryset(self):
        qs = (
            FinancialTransaction.objects
            .select_related('document', 'contact', 'payment_account')
            .prefetch_related('allocations__document')
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


    @transaction.atomic
    def update(self, request, *args, **kwargs):
        ftxn = self.get_object()
        if ftxn.contact_id:
            Contact.objects.select_for_update().get(pk=ftxn.contact_id)
        if ftxn.document_id:
            Document.objects.select_for_update().get(pk=ftxn.document_id)
        ftxn = FinancialTransaction.objects.select_for_update().get(pk=ftxn.pk)
        if ftxn.type != 'actual':
            return Response(
                {'error': 'Only actual transactions can be edited directly.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        old_date = ftxn.date
        serializer = self.get_serializer(ftxn, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        old_amount, old_account_id = ftxn.amount, ftxn.payment_account_id

        if 'amount' in data:
            ftxn.amount = data['amount']

        if 'date' in data:
            ftxn.date = data['date']

        if 'payment_account' in data:
            ftxn.payment_account = data['payment_account']

        from django.db.models import F
        from django.utils import timezone
        deltas = {}
        if old_account_id:
            deltas[old_account_id] = -old_amount
        if ftxn.payment_account_id:
            deltas[ftxn.payment_account_id] = deltas.get(ftxn.payment_account_id, Decimal('0')) + ftxn.amount
        for account_id, delta in sorted(deltas.items()):
            PaymentAccount.objects.filter(pk=account_id).update(current_balance=F('current_balance') + delta, updated_at=timezone.now())

        if 'notes' in data:
            ftxn.notes = data['notes']

        ftxn.save()
        from .payments import rescale_allocations
        rescale_allocations(ftxn, old_amount)
        if ftxn.document and ftxn.document.type in {'expense','cash_payment_voucher','cash_receipt_voucher'}:
            ftxn.document.total_amount = abs(sum((row.amount for row in ftxn.document.transactions.filter(type='actual')), Decimal('0')))
            ftxn.document.save(update_fields=['total_amount','updated_at'])
        _recalculate_mcd(ftxn.contact, ftxn.date)
        if old_date.month != ftxn.date.month or old_date.year != ftxn.date.year:
            _recalculate_mcd(ftxn.contact, old_date)

        return Response(FinancialTransactionSerializer(ftxn, context={'request': request}).data)


    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        ftxn = self.get_object()
        if ftxn.contact_id:
            Contact.objects.select_for_update().get(pk=ftxn.contact_id)
        if ftxn.document_id:
            Document.objects.select_for_update().get(pk=ftxn.document_id)
        ftxn = FinancialTransaction.objects.select_for_update().get(pk=ftxn.pk)
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
            from django.db.models import F
            from django.utils import timezone
            PaymentAccount.objects.filter(pk=ftxn.payment_account_id).update(current_balance=F('current_balance') - ftxn.amount, updated_at=timezone.now())

        ftxn.delete()
        _recalculate_mcd(contact, date)
        return Response(status=status.HTTP_204_NO_CONTENT)


    @action(detail=True, methods=['post'])
    @transaction.atomic
    def link_document(self, request, pk=None):
        from rest_framework.exceptions import ValidationError
        from django.shortcuts import get_object_or_404
        from .payments import replace_allocations
        ftxn = self.get_object()
        if ftxn.type != 'actual' or (ftxn.document and ftxn.document.type == 'expense'):
            raise ValidationError({'document':'Only ordinary payments can be linked to documents.'})
        doc_id = request.data.get('document')
        doc = get_object_or_404(Document, pk=doc_id, is_active=True, type__in=HAS_BALANCE_TYPES) if doc_id else None
        list(Contact.objects.select_for_update().filter(pk__in=[pk for pk in
            (ftxn.contact_id, doc.contact_id if doc else None) if pk]).order_by('pk'))
        if ftxn.document_id:
            Document.objects.select_for_update().get(pk=ftxn.document_id)
        ftxn = FinancialTransaction.objects.select_for_update().get(pk=ftxn.pk)
        adjustments = sum((row.amount for row in ftxn.allocations.filter(document__type='interest')), Decimal('0'))
        available = max(abs(ftxn.amount) - adjustments, Decimal('0'))
        replace_allocations(ftxn, [{'document':doc.pk, 'amount':available}] if doc and available else [])
        return Response(self.get_serializer(ftxn).data)


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
