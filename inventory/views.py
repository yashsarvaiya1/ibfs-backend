from decimal import Decimal
from django.db import models
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

    def get_serializer_context(self):
        return {'request': self.request}

    def get_queryset(self):
        qs     = Product.objects.all()
        params = self.request.query_params
        if params.get('is_active') is not None:
            qs = qs.filter(is_active=params['is_active'].lower() == 'true')
        if params.get('low_stock') == 'true':
            qs = qs.filter(current_stock__lt=models.F('min_stock'))
        return qs

    @action(detail=True, methods=['post'])
    def adjust_stock(self, request, pk=None):
        """
        Manual stock adjustment — creates actual s.txn, no document reference.
        quantity is signed (+ or −). Updates current_stock immediately.
        Per spec 6.3 — Adjust Stock method.
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
        return Response(
            StockTransactionSerializer(stxn, context={'request': request}).data,
            status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=['post'])
    def set_stock(self, request, pk=None):
        """
        Direct stock overwrite — NO s.txn created at all.
        Per spec 6.3 — Direct Edit method: "User directly edits current_stock on the Product.
        No s.txn is created at all."
        """
        product = self.get_object()
        product.current_stock = Decimal(str(request.data['current_stock']))
        product.save(update_fields=['current_stock', 'updated_at'])
        return Response(ProductSerializer(product, context={'request': request}).data)

    @action(detail=True, methods=['get'])
    def pending_moves(self, request, pk=None):
        """
        All pending record s.txns for this product where remaining quantity != 0.
        Per spec 6.2 — Product page stock movement section.
        Only shows records where document is still active (is_active=True).
        Record s.txns are always hard-deleted on document deletion (both options),
        so this filter is purely a safety net.
        """
        product = self.get_object()
        records = StockTransaction.objects.filter(
            product=product,
            type='record',
            document__is_active=True,
        ).select_related('document', 'document__contact')

        result = []
        for r in records:
            actuals   = StockTransaction.objects.filter(
                document=r.document,
                product=product,
                type='actual',
            ).values_list('quantity', flat=True)
            moved     = sum(actuals)
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
        Per spec 6.2 — each row on the product page has its own Move Stock button.
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

    def get_serializer_context(self):
        return {'request': self.request}

    def get_queryset(self):
        qs     = StockTransaction.objects.select_related('document', 'product').all()
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

        # Derived filter — no DB field, computed from document.is_active
        # ?is_document_deleted=true  → document exists but is soft-deleted
        # ?is_document_deleted=false → document is active or no document linked
        is_doc_deleted = params.get('is_document_deleted')
        if is_doc_deleted is not None:
            if is_doc_deleted.lower() == 'true':
                qs = qs.filter(document__isnull=False, document__is_active=False)
            else:
                qs = qs.filter(
                    models.Q(document__isnull=True) | models.Q(document__is_active=True)
                )

        return qs

    def update(self, request, *args, **kwargs):
        """
        Only actual s.txns can be edited directly.
        Record s.txns are managed via document edit → _sync_record_stxns in accounting.services.
        Quantity diff is applied to product.current_stock immediately.
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
            stxn.product.save(update_fields=['current_stock', 'updated_at'])
            stxn.quantity = new_qty

        if 'notes' in request.data:
            stxn.notes = request.data['notes']
        if 'date' in request.data:
            stxn.date = _parse_date(request.data['date'])
        if 'rate' in request.data:
            stxn.rate = request.data['rate']

        stxn.save()
        return Response(StockTransactionSerializer(stxn, context={'request': request}).data)

    def destroy(self, request, *args, **kwargs):
        """
        Only actual s.txns can be deleted directly.
        Record s.txns are deleted via document deletion flow only.
        Reverses the stock change on delete.
        """
        stxn = self.get_object()
        if stxn.type == 'record':
            return Response(
                {'error': 'Record stock transactions are managed via document deletion.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        stxn.product.current_stock -= stxn.quantity
        stxn.product.save(update_fields=['current_stock', 'updated_at'])
        stxn.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=['post'])
    def adjust(self, request):
        """
        Standalone stock adjustment from the Stock Transactions page.
        Per spec 6.3 — Adjust Stock: creates actual s.txn, no document, updates current_stock.
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
        return Response(
            StockTransactionSerializer(stxn, context={'request': request}).data,
            status=status.HTTP_201_CREATED
        )
