from decimal import Decimal
from django.db import models
from django.http import HttpResponse                          # ← THIS was missing
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

        # Comma-separated IDs — used by selective print, short-circuits all other filters
        ids_param = params.get('ids')
        if ids_param:
            id_list = [int(i) for i in ids_param.split(',') if i.strip().isdigit()]
            return qs.filter(id__in=id_list)

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
            type_    = 'actual',
            quantity = Decimal(str(request.data['quantity'])),
            product  = product,
            document = None,
            date     = _parse_date(request.data.get('date')),
            rate     = request.data.get('rate'),
            notes    = request.data.get('notes'),
        )
        return Response(
            StockTransactionSerializer(stxn, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=['post'])
    def set_stock(self, request, pk=None):
        """
        Direct stock overwrite — NO s.txn created at all.
        Per spec 6.3 — Direct Edit method.
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

    @action(detail=False, methods=['get'])
    def print(self, request):
        """
        Generates stock list PDF.
        ?ids=1,2,3        → only those products (selective print)
        ?low_stock=true   → only low stock products
        ?is_active=true   → all active products (default)
        """
        from accounting.services import generate_stock_list_pdf
        qs             = self.filter_queryset(self.get_queryset())
        low_stock_only = request.query_params.get('low_stock', '').lower() == 'true'
        try:
            pdf_bytes, filename = generate_stock_list_pdf(
                list(qs), request=request, low_stock_only=low_stock_only,
            )
        except Exception as e:
            import traceback; traceback.print_exc()
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


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
        Record s.txns are managed via document edit → _sync_record_stxns.
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
        Per spec 6.3 — Adjust Stock.
        """
        product = Product.objects.get(pk=request.data['product'])
        stxn = _create_stxn(
            type_    = 'actual',
            quantity = Decimal(str(request.data['quantity'])),
            product  = product,
            document = None,
            date     = _parse_date(request.data.get('date')),
            rate     = request.data.get('rate'),
            notes    = request.data.get('notes'),
        )
        return Response(
            StockTransactionSerializer(stxn, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=['get'])
    def print(self, request):
        """
        Generates stock transactions PDF for a product with optional date range.
        ?product=5            → filter by product (required for running balance)
        ?date_from=2026-01-01 → period start
        ?date_to=2026-03-24   → period end
        """
        from datetime import date as date_type
        from accounting.services import generate_stock_transactions_pdf

        qs = self.filter_queryset(self.get_queryset())

        product = None
        if request.query_params.get('product'):
            try:
                product = Product.objects.get(pk=request.query_params['product'])
            except Product.DoesNotExist:
                pass

        date_from, date_to = None, None
        if request.query_params.get('date_from'):
            try: date_from = date_type.fromisoformat(request.query_params['date_from'])
            except ValueError: pass
        if request.query_params.get('date_to'):
            try: date_to = date_type.fromisoformat(request.query_params['date_to'])
            except ValueError: pass

        try:
            pdf_bytes, filename = generate_stock_transactions_pdf(
                list(qs), request=request,
                product=product, date_from=date_from, date_to=date_to,
            )
        except Exception as e:
            import traceback; traceback.print_exc()
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
