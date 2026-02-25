# accounting/views.py
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Document, FinancialTransaction
from .serializers import DocumentSerializer, DocumentListSerializer, FinancialTransactionSerializer
from .services import (
    process_document_create, process_document_delete,
    process_move_stock, _create_ftxn
)
from shared.models import Contact, Settings
from django.utils import timezone
from decimal import Decimal


class DocumentViewSet(viewsets.ModelViewSet):
    search_fields = ['doc_id', 'contact__contact_name', 'contact__company_name']
    ordering_fields = ['date', 'created_at', 'total_amount']
    ordering = ['-date']

    def get_queryset(self):
        qs = Document.objects.filter(is_active=True)
        doc_type = self.request.query_params.get('type')
        contact = self.request.query_params.get('contact')
        if doc_type:
            qs = qs.filter(type=doc_type)
        if contact:
            qs = qs.filter(contact_id=contact)
        return qs

    def get_serializer_class(self):
        if self.action == 'list':
            return DocumentListSerializer
        return DocumentSerializer

    def create(self, request):
        doc_type = request.data.get('type')
        contact_id = request.data.get('contact')
        contact = Contact.objects.get(pk=contact_id) if contact_id else None
        doc = process_document_create(doc_type, request.data, contact)
        return Response(DocumentSerializer(doc).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def record_payment(self, request, pk=None):
        doc = self.get_object()
        amount = Decimal(str(request.data['amount']))
        account_id = request.data.get('payment_account')
        from shared.models import PaymentAccount
        account = PaymentAccount.objects.get(pk=account_id) if account_id else None
        date = request.data.get('date', timezone.localdate())
        ftxn = _create_ftxn('actual', amount, doc.contact, account, doc, date,
                             request.data.get('notes'))
        return Response(FinancialTransactionSerializer(ftxn).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def move_stock(self, request, pk=None):
        doc = self.get_object()
        result = process_move_stock(doc, request.data)
        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'])
    def stock_preview(self, request, pk=None):
        from inventory.models import StockTransaction
        doc = self.get_object()
        records = StockTransaction.objects.filter(document=doc, type='record')
        preview = []
        for r in records:
            actuals_sum = sum(
                t.quantity for t in StockTransaction.objects.filter(
                    document=doc, product=r.product, type='actual'
                )
            )
            preview.append({
                'product_id': r.product_id,
                'product_name': r.product.name,
                'record_qty': str(r.quantity),
                'moved_qty': str(actuals_sum),
                'remaining_qty': str(r.quantity - actuals_sum),
            })
        return Response(preview)

    @action(detail=True, methods=['post'])
    def add_details(self, request, pk=None):
        """Add line items to a fast-created bill/invoice after the fact."""
        from inventory.models import Product, StockTransaction
        doc = self.get_object()
        settings = Settings.get()
        line_items = request.data.get('line_items', [])
        doc.line_items = line_items
        if not doc.total_amount:
            doc.total_amount = sum(Decimal(str(i.get('amount', 0))) for i in line_items)
        doc.save()

        sign_map = {'bill': 1, 'invoice': -1, 'cn': 1, 'dn': -1}
        sign = Decimal(str(sign_map.get(doc.type, 1)))

        for item in line_items:
            pid = item.get('product_id')
            if not pid:
                continue
            try:
                product = Product.objects.get(pk=pid)
            except Product.DoesNotExist:
                continue
            qty = sign * Decimal(str(item.get('quantity', 0)))
            txn_type = 'actual' if settings.auto_stock else 'record'
            from .services import _create_stxn
            _create_stxn(txn_type, qty, product, doc, doc.date, item.get('rate'))

        return Response(DocumentSerializer(doc).data)

    @action(detail=True, methods=['post'])
    def delete_document(self, request, pk=None):
        doc = self.get_object()
        strategy = request.data.get('strategy', 'orphan')
        result = process_document_delete(doc, strategy)
        return Response(result)


class FinancialTransactionViewSet(viewsets.ModelViewSet):
    serializer_class = FinancialTransactionSerializer
    search_fields = ['contact__contact_name', 'contact__company_name', 'notes']
    ordering_fields = ['date', 'amount', 'created_at']
    ordering = ['-date']

    def get_queryset(self):
        qs = FinancialTransaction.objects.all()
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

    @action(detail=True, methods=['post'])
    def link_document(self, request, pk=None):
        ftxn = self.get_object()
        doc_id = request.data.get('document')
        ftxn.document_id = doc_id
        ftxn.save(update_fields=['document'])
        return Response(FinancialTransactionSerializer(ftxn).data)
