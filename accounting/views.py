# accounting/views.py
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Document, FinancialTransaction
from .serializers import (
    DocumentSerializer, DocumentListSerializer,
    FinancialTransactionSerializer,
)
from .services import (
    process_document_create, process_document_delete,
    process_move_stock, process_send_receive,
    process_expense, process_standalone_interest,
    _create_ftxn, _recalculate_mcd, _parse_date, _next_doc_id,
)
from shared.models import Contact, PaymentAccount, Settings
from decimal import Decimal


# ─── Document ViewSet ─────────────────────────────────────────────────────────

class DocumentViewSet(viewsets.ModelViewSet):
    search_fields   = ['doc_id', 'contact__contact_name', 'contact__company_name']
    ordering_fields = ['date', 'created_at', 'total_amount']
    ordering        = ['-date']

    def get_queryset(self):
        qs = Document.objects.filter(is_active=True).select_related('contact')
        if t := self.request.query_params.get('type'):
            qs = qs.filter(type=t)
        if c := self.request.query_params.get('contact'):
            qs = qs.filter(contact_id=c)
        if date_from := self.request.query_params.get('date_from'):
            qs = qs.filter(date__gte=date_from)
        if date_to := self.request.query_params.get('date_to'):
            qs = qs.filter(date__lte=date_to)
        if self.request.query_params.get('pending_stock') == 'true':
            from inventory.models import StockTransaction
            from django.db.models import Exists, OuterRef
            has_record = StockTransaction.objects.filter(
                document=OuterRef('pk'), type='record'
            )
            qs = qs.filter(Exists(has_record))
        return qs

    def get_serializer_class(self):
        return DocumentListSerializer if self.action == 'list' else DocumentSerializer

    def create(self, request):
        doc_type   = request.data.get('type')
        contact_id = request.data.get('contact')
        contact    = Contact.objects.get(pk=contact_id) if contact_id else None
        doc        = process_document_create(doc_type, request.data, contact)
        return Response(DocumentSerializer(doc).data, status=status.HTTP_201_CREATED)

    # ── Record Payment ─────────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def record_payment(self, request, pk=None):
        """
        Bill/CN  → money OUT → actual = −amount
        Invoice/DN → money IN → actual = +amount
        Supports optional interest lines.
        """
        doc            = self.get_object()
        amount_raw     = Decimal(str(request.data['amount']))
        account_id     = request.data.get('payment_account')
        account        = PaymentAccount.objects.get(pk=account_id) if account_id else None
        date           = _parse_date(request.data.get('date'))
        notes          = request.data.get('notes')
        interest_lines = request.data.get('interest_lines', [])

        outgoing_types = {'bill', 'cn', 'cash_payment_voucher'}
        direction  = 'send' if doc.type in outgoing_types else 'receive'
        actual_amt = -amount_raw if direction == 'send' else amount_raw

        result = {}

        # Interest record FIRST, then actual
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
                contact=doc.contact,
                line_items=interest_lines,
                total_amount=abs(net),
                date=date,
                reference=doc,
            )
            int_ftxn = _create_ftxn(
                'record', interest_record_amount,
                doc.contact, None, interest_doc, date,
                'Interest/penalty on payment',
            )
            result['interest_doc']  = interest_doc.pk
            result['interest_ftxn'] = int_ftxn.pk

        ftxn           = _create_ftxn(
            'actual', actual_amt, doc.contact, account, doc, date, notes
        )
        result['ftxn'] = ftxn.pk
        return Response(result, status=status.HTTP_201_CREATED)

    # ── Move Stock ─────────────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def move_stock(self, request, pk=None):
        doc    = self.get_object()
        result = process_move_stock(doc, request.data)
        return Response(result, status=status.HTTP_201_CREATED)

    # ── Stock Preview ──────────────────────────────────────────────────────────
    @action(detail=True, methods=['get'])
    def stock_preview(self, request, pk=None):
        from inventory.models import StockTransaction
        doc     = self.get_object()
        records = StockTransaction.objects.filter(
            document=doc, type='record'
        ).select_related('product')
        preview = []
        for r in records:
            actuals_sum = sum(
                t.quantity for t in StockTransaction.objects.filter(
                    document=doc, product=r.product, type='actual'
                )
            )
            remaining = r.quantity - actuals_sum
            preview.append({
                'product_id':    r.product_id,
                'product_name':  r.product.name,
                'unit':          r.product.unit,
                'record_qty':    str(r.quantity),
                'moved_qty':     str(actuals_sum),
                'remaining_qty': str(remaining),
                'is_complete':   remaining == 0,
            })
        return Response(preview)

    # ── Add Details ────────────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def add_details(self, request, pk=None):
        from inventory.models import Product
        from .services import _create_stxn
        doc        = self.get_object()
        settings   = Settings.get()
        line_items = request.data.get('line_items', [])
        doc.line_items = line_items
        if not doc.total_amount:
            doc.total_amount = sum(
                Decimal(str(i.get('amount', 0))) for i in line_items
            )
        doc.save()

        sign_map = {'bill': 1, 'invoice': -1, 'cn': 1, 'dn': -1}
        sign     = Decimal(str(sign_map.get(doc.type, 1)))

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
            _create_stxn(txn_type, qty, product, doc, doc.date, item.get('rate'))

        return Response(DocumentSerializer(doc).data)

    # ── Delete Document ────────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def delete_document(self, request, pk=None):
        doc      = self.get_object()
        strategy = request.data.get('strategy', 'orphan')
        result   = process_document_delete(doc, strategy)
        return Response(result)


# ─── FinancialTransaction ViewSet ─────────────────────────────────────────────

class FinancialTransactionViewSet(viewsets.ModelViewSet):
    serializer_class = FinancialTransactionSerializer
    search_fields    = ['contact__contact_name', 'contact__company_name', 'notes']
    ordering_fields  = ['date', 'amount', 'created_at']
    # CRITICAL: ascending for correct MCD. Frontend reverses for display.
    ordering         = ['date', 'created_at']

    def get_queryset(self):
        qs     = FinancialTransaction.objects.all().select_related(
            'contact', 'payment_account', 'document'
        )
        params = self.request.query_params
        if params.get('contact'):
            qs = qs.filter(contact_id=params['contact'])
        if params.get('account'):
            qs = qs.filter(payment_account_id=params['account'])
        if params.get('type'):
            qs = qs.filter(type=params['type'])
        if params.get('document'):
            qs = qs.filter(document_id=params['document'])
        if params.get('exclude_type'):
            qs = qs.exclude(type=params['exclude_type'])
        return qs

    def update(self, request, *args, **kwargs):
        ftxn     = self.get_object()
        old_date = ftxn.date

        allowed = ['amount', 'date', 'notes', 'payment_account']
        for field in allowed:
            if field not in request.data:
                continue
            if field == 'amount':
                new_amt = Decimal(str(request.data['amount']))
                if ftxn.payment_account:
                    ftxn.payment_account.current_balance -= ftxn.amount
                    ftxn.payment_account.current_balance += new_amt
                    ftxn.payment_account.save(update_fields=['current_balance'])
                ftxn.amount = new_amt
            elif field == 'date':
                ftxn.date = _parse_date(request.data['date'])
            elif field == 'payment_account':
                acct_id = request.data['payment_account']
                if ftxn.payment_account:
                    ftxn.payment_account.current_balance -= ftxn.amount
                    ftxn.payment_account.save(update_fields=['current_balance'])
                new_acct = PaymentAccount.objects.get(pk=acct_id) if acct_id else None
                if new_acct:
                    new_acct.current_balance += ftxn.amount
                    new_acct.save(update_fields=['current_balance'])
                ftxn.payment_account = new_acct
            else:
                setattr(ftxn, field, request.data[field])

        ftxn.save()
        _recalculate_mcd(ftxn.contact, ftxn.date)
        if old_date.month != ftxn.date.month or old_date.year != ftxn.date.year:
            _recalculate_mcd(ftxn.contact, old_date)

        return Response(FinancialTransactionSerializer(ftxn).data)

    def destroy(self, request, *args, **kwargs):
        ftxn    = self.get_object()
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


# ─── Quick Action ViewSet ─────────────────────────────────────────────────────

class QuickActionViewSet(viewsets.ViewSet):
    """
    Global Quick Actions — no document context required.

    POST /api/quick-actions/expense/
         { contact(opt), payment_account, date, line_items, notes }

    POST /api/quick-actions/interest/
         { contact(required), date, line_items, action: 'charge'|'credit', notes }
    """

    @action(detail=False, methods=['post'], url_path='expense')
    def create_expense(self, request):
        result = process_expense(request.data)
        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'], url_path='interest')
    def create_interest(self, request):
        result = process_standalone_interest(request.data)
        return Response(result, status=status.HTTP_201_CREATED)
