# inventory/views.py
from decimal import Decimal
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Product, StockTransaction
from .serializers import ProductSerializer, ProductListSerializer, StockTransactionSerializer
from accounting.services import _create_stxn, _parse_date


class ProductViewSet(viewsets.ModelViewSet):
    search_fields   = ['name', 'hsn_code']
    ordering_fields = ['name', 'current_stock', 'rate']
    ordering        = ['name']

    def get_serializer_class(self):
        return ProductListSerializer if self.action == 'list' else ProductSerializer

    def get_queryset(self):
        qs     = Product.objects.all()
        params = self.request.query_params
        if params.get('is_active') is not None:
            qs = qs.filter(is_active=params['is_active'].lower() == 'true')
        if params.get('low_stock') == 'true':
            from django.db.models import F
            qs = qs.filter(current_stock__lt=F('min_stock'))
        return qs

    @action(detail=True, methods=['post'])
    def adjust_stock(self, request, pk=None):
        """
        Adjust stock with an actual s.txn — no document reference.
        quantity can be positive or negative.
        """
        product = self.get_object()
        stxn = _create_stxn(
            type     = 'actual',
            quantity = Decimal(str(request.data['quantity'])),
            product  = product,
            document = None,
            date     = _parse_date(request.data.get('date')),
            rate     = request.data.get('rate'),
            notes    = request.data.get('notes'),
        )
        return Response(StockTransactionSerializer(stxn).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def set_stock(self, request, pk=None):
        """
        Direct stock overwrite — no s.txn created.
        Used for initial setup or manual correction.
        """
        product = self.get_object()
        product.current_stock = Decimal(str(request.data['current_stock']))
        product.save(update_fields=['current_stock', 'updated_at'])
        return Response(ProductSerializer(product).data)

    @action(detail=True, methods=['get'])
    def pending_moves(self, request, pk=None):
        """
        All pending record s.txns for this product grouped by document.
        Returns only entries where remaining quantity != 0.
        """
        product = self.get_object()
        records = StockTransaction.objects.filter(
            product=product, type='record', is_doc_deleted=False
        ).select_related('document', 'document__contact')

        result = []
        for r in records:
            moved = sum(
                StockTransaction.objects.filter(
                    document=r.document, product=product, type='actual'
                ).values_list('quantity', flat=True)
            )
            remaining = r.quantity - moved
            if remaining != 0:
                result.append({
                    'document_id':   r.document_id,
                    'doc_id':        r.document.doc_id if r.document else None,
                    'doc_type':      r.document.type if r.document else None,
                    'contact':       str(r.document.contact) if r.document and r.document.contact else None,
                    'date':          r.document.date if r.document else None,
                    'record_qty':    str(r.quantity),
                    'moved_qty':     str(moved),
                    'remaining_qty': str(remaining),
                })
        return Response(result)

    @action(detail=True, methods=['post'])
    def move_stock_from_product(self, request, pk=None):
        """
        Triggers move_stock for a specific document from the product page.
        """
        product = self.get_object()
        from accounting.models import Document
        from accounting.services import process_move_stock
        doc    = Document.objects.get(pk=request.data['document_id'])
        result = process_move_stock(doc, {
            'items': [{'product_id': product.pk, 'quantity': request.data['quantity']}],
            'date':  _parse_date(request.data.get('date')),
        })
        return Response(result)


class StockTransactionViewSet(viewsets.ModelViewSet):
    serializer_class = StockTransactionSerializer
    search_fields    = ['product__name', 'notes']
    ordering_fields  = ['date', 'quantity', 'created_at']
    ordering         = ['-date']

    def get_queryset(self):
        qs     = StockTransaction.objects.all()
        params = self.request.query_params
        if params.get('product'):
            qs = qs.filter(product_id=params['product'])
        if params.get('document'):
            qs = qs.filter(document_id=params['document'])
        if params.get('type'):
            qs = qs.filter(type=params['type'])
        if params.get('date_from'):
            qs = qs.filter(date__gte=params['date_from'])
        if params.get('date_to'):
            qs = qs.filter(date__lte=params['date_to'])
        if params.get('is_doc_deleted') is not None:
            qs = qs.filter(is_doc_deleted=params['is_doc_deleted'].lower() == 'true')
        return qs

    def update(self, request, *args, **kwargs):
        """
        Only actual s.txns can be edited.
        Record s.txns are managed via document edit → _sync_record_stxns.
        Quantity diff is applied to product.current_stock.
        """
        stxn = self.get_object()
        if stxn.type == 'record':
            return Response(
                {'error': 'Record stock transactions are managed via document edit.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if 'quantity' in request.data:
            new_qty = Decimal(str(request.data['quantity']))
            diff    = new_qty - stxn.quantity
            stxn.product.current_stock += diff
            stxn.product.save(update_fields=['current_stock'])
            stxn.quantity = new_qty

        if 'notes' in request.data:
            stxn.notes = request.data['notes']

        if 'date' in request.data:
            stxn.date = _parse_date(request.data['date'])

        if 'rate' in request.data:
            stxn.rate = request.data['rate']

        stxn.save()
        return Response(StockTransactionSerializer(stxn).data)

    def destroy(self, request, *args, **kwargs):
        """
        Only actual s.txns can be deleted directly.
        Record s.txns are deleted via document deletion flow.
        Reverses the stock change on delete.
        """
        stxn = self.get_object()
        if stxn.type == 'record':
            return Response(
                {'error': 'Record stock transactions are managed via document deletion.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        stxn.product.current_stock -= stxn.quantity
        stxn.product.save(update_fields=['current_stock'])
        stxn.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=['post'])
    def adjust(self, request):
        """
        Standalone stock adjustment — actual s.txn, no document reference.
        Available from the Stock Transactions page directly.
        """
        product = Product.objects.get(pk=request.data['product'])
        stxn = _create_stxn(
            type     = 'actual',
            quantity = Decimal(str(request.data['quantity'])),
            product  = product,
            document = None,
            date     = _parse_date(request.data.get('date')),
            rate     = request.data.get('rate'),
            notes    = request.data.get('notes'),
        )
        return Response(StockTransactionSerializer(stxn).data, status=status.HTTP_201_CREATED)
