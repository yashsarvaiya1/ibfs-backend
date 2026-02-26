# accounting/views.py
from decimal import Decimal
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Document, FinancialTransaction
from .serializers import DocumentSerializer, DocumentListSerializer, FinancialTransactionSerializer
from .services import (
    _create_ftxn, _create_stxn, _next_doc_id,
    _parse_date, _recalculate_mcd,
    process_document_create, process_document_delete, process_move_stock,
)
from shared.models import Contact, PaymentAccount, Settings


class DocumentViewSet(viewsets.ModelViewSet):
    search_fields   = ['doc_id', 'contact__contact_name', 'contact__company_name']
    ordering_fields = ['date', 'created_at', 'total_amount']
    ordering        = ['-date']

    def get_queryset(self):
        qs = Document.objects.filter(is_active=True)
        if t := self.request.query_params.get('type'):
            qs = qs.filter(type=t)
        if c := self.request.query_params.get('contact'):
            qs = qs.filter(contact_id=c)
        return qs

    def get_serializer_class(self):
        return DocumentListSerializer if self.action == 'list' else DocumentSerializer

    def create(self, request, *args, **kwargs):
        doc_type   = request.data.get('type')
        contact_id = request.data.get('contact')
        contact    = Contact.objects.get(pk=contact_id) if contact_id else None
        doc        = process_document_create(doc_type, request.data, contact)
        return Response(DocumentSerializer(doc).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def record_payment(self, request, pk=None):
        """
        Records an actual f.txn against a document.
        Derives direction from doc type — user always passes positive amount.
        Supports optional interest_lines.
        """
        doc        = self.get_object()
        amount_raw = Decimal(str(request.data['amount']))
        account_id = request.data.get('payment_account')
        account    = PaymentAccount.objects.get(pk=account_id) if account_id else None
        date       = _parse_date(request.data.get('date'))
        notes      = request.data.get('notes')
        interest_lines = request.data.get('interest_lines', [])

        # bill/cn → we pay out → actual negative
        # invoice/dn → we receive → actual positive
        outgoing  = {'bill', 'cn', 'cash_payment_voucher'}
        direction = 'send' if doc.type in outgoing else 'receive'
        actual_amt = -amount_raw if direction == 'send' else amount_raw

        result = {}

        # Interest record FIRST — lower created_at ensures correct ledger order
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

    @action(detail=True, methods=['post'])
    def move_stock(self, request, pk=None):
        doc    = self.get_object()
        result = process_move_stock(doc, request.data)
        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'])
    def stock_preview(self, request, pk=None):
        from inventory.models import StockTransaction
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

    @action(detail=True, methods=['post'])
    def add_details(self, request, pk=None):
        """
        Adds line_items to a Fast Bill after creation.
        Safe to call once — checks for existing s.txns to avoid duplicates.
        """
        from inventory.models import Product, StockTransaction
        doc        = self.get_object()
        settings   = Settings.get()
        line_items = request.data.get('line_items', [])

        doc.line_items = line_items
        if not doc.total_amount:
            doc.total_amount = sum(Decimal(str(i.get('amount', 0))) for i in line_items)
        doc.save(update_fields=['line_items', 'total_amount', 'updated_at'])

        from .services import STXN_SIGN, CHALLAN_STXN_SIGN
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

            # Guard: skip if a s.txn already exists for this product+doc
            already_exists = StockTransaction.objects.filter(
                document=doc, product=product
            ).exists()
            if already_exists:
                continue

            qty      = sign * Decimal(str(item.get('quantity', 0)))
            txn_type = 'actual' if settings.auto_stock else 'record'
            _create_stxn(txn_type, qty, product, doc, doc.date, item.get('rate'))

        return Response(DocumentSerializer(doc).data)

    @action(detail=True, methods=['post'])
    def delete_document(self, request, pk=None):
        doc      = self.get_object()
        strategy = request.data.get('strategy', 'orphan')
        result   = process_document_delete(doc, strategy)
        return Response(result)


class FinancialTransactionViewSet(viewsets.ModelViewSet):
    serializer_class = FinancialTransactionSerializer
    search_fields    = ['contact__contact_name', 'contact__company_name', 'notes']
    ordering_fields  = ['date', 'amount', 'created_at']
    ordering         = ['date', 'created_at']  # ascending — MCD depends on order

    def get_queryset(self):
        qs     = FinancialTransaction.objects.all()
        params = self.request.query_params
        if params.get('contact'):
            qs = qs.filter(contact_id=params['contact'])
        if params.get('account'):
            qs = qs.filter(payment_account_id=params['account'])
        if params.get('type'):
            qs = qs.filter(type=params['type'])
        if params.get('document'):
            qs = qs.filter(document_id=params['document'])
        return qs

    def update(self, request, *args, **kwargs):
        ftxn     = self.get_object()
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
        ftxn = self.get_object()
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
