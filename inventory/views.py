# inventory/views.py
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from decimal import Decimal
from django.utils import timezone
from .models import Product, StockTransaction
from .serializers import ProductSerializer, ProductListSerializer, StockTransactionSerializer
from accounting.services import _create_stxn


class ProductViewSet(viewsets.ModelViewSet):
    search_fields = ['name', 'hsn_code']
    ordering_fields = ['name', 'current_stock', 'rate']
    ordering = ['name']

    def get_queryset(self):
        qs = Product.objects.all()
        is_active = self.request.query_params.get('is_active')
        low_stock = self.request.query_params.get('low_stock')
        if is_active is not None:
            qs = qs.filter(is_active=is_active.lower() == 'true')
        if low_stock == 'true':
            from django.db.models import F
            qs = qs.filter(current_stock__lt=F('min_stock'))
        return qs

    def get_serializer_class(self):
        if self.action == 'list':
            return ProductListSerializer
        return ProductSerializer

    @action(detail=True, methods=['post'])
    def adjust_stock(self, request, pk=None):
        product = self.get_object()
        quantity = Decimal(str(request.data['quantity']))
        date = request.data.get('date', timezone.localdate())
        rate = request.data.get('rate')
        notes = request.data.get('notes')
        stxn = _create_stxn('actual', quantity, product, None, date, rate, notes)
        return Response(StockTransactionSerializer(stxn).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def set_stock(self, request, pk=None):
        # Direct edit — no s.txn created
        product = self.get_object()
        product.current_stock = Decimal(str(request.data['current_stock']))
        product.save(update_fields=['current_stock'])
        return Response(ProductSerializer(product).data)

    @action(detail=True, methods=['get'])
    def pending_moves(self, request, pk=None):
        """All pending record s.txns for this product grouped by document."""
        product = self.get_object()
        records = StockTransaction.objects.filter(
            product=product, type='record', is_doc_deleted=False
        ).select_related('document', 'document__contact')
        result = []
        for r in records:
            actuals_sum = StockTransaction.objects.filter(
                document=r.document, product=product, type='actual'
            ).values_list('quantity', flat=True)
            moved = sum(actuals_sum)
            remaining = r.quantity - moved
            if remaining != 0:
                result.append({
                    'document_id': r.document_id,
                    'doc_id': r.document.doc_id if r.document else None,
                    'doc_type': r.document.type if r.document else None,
                    'contact': str(r.document.contact) if r.document and r.document.contact else None,
                    'date': r.document.date if r.document else None,
                    'record_qty': str(r.quantity),
                    'moved_qty': str(moved),
                    'remaining_qty': str(remaining),
                })
        return Response(result)

    @action(detail=True, methods=['post'])
    def move_stock_from_product(self, request, pk=None):
        product = self.get_object()
        from accounting.models import Document
        from accounting.services import process_move_stock
        doc_id = request.data.get('document_id')
        doc = Document.objects.get(pk=doc_id)
        result = process_move_stock(doc, {
            'items': [{'product_id': product.pk, 'quantity': request.data['quantity']}],
            'date': request.data.get('date', timezone.localdate()),
        })
        return Response(result)


class StockTransactionViewSet(viewsets.ModelViewSet):
    serializer_class = StockTransactionSerializer
    search_fields = ['product__name', 'notes']
    ordering_fields = ['date', 'quantity', 'created_at']
    ordering = ['-date']

    def get_queryset(self):
        qs = StockTransaction.objects.all()
        params = self.request.query_params
        if params.get('product'):
            qs = qs.filter(product_id=params['product'])
        if params.get('document'):
            qs = qs.filter(document_id=params['document'])
        if params.get('type'):
            qs = qs.filter(type=params['type'])
        return qs

    @action(detail=False, methods=['post'])
    def adjust(self, request):
        from shared.models import Settings
        quantity = Decimal(str(request.data['quantity']))
        product_id = request.data['product']
        product = Product.objects.get(pk=product_id)
        date = request.data.get('date', timezone.localdate())
        stxn = _create_stxn('actual', quantity, product, None, date,
                             request.data.get('rate'), request.data.get('notes'))
        return Response(StockTransactionSerializer(stxn).data, status=status.HTTP_201_CREATED)
