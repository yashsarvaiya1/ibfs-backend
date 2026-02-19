from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from .models import Product, StockTransaction
from .serializers import ProductSerializer, StockTransactionSerializer


class ProductViewSet(viewsets.ModelViewSet):
    serializer_class = ProductSerializer
    permission_classes = [IsAuthenticated]
    search_fields = ['name', 'hsn_code', 'description']

    def get_queryset(self):
        # Default: only active products
        # Pass ?include_inactive=true to get all
        include_inactive = self.request.query_params.get('include_inactive', 'false')
        if include_inactive.lower() == 'true':
            return Product.objects.all()
        return Product.objects.filter(is_active=True)

    def destroy(self, request, *args, **kwargs):
        # Soft delete — preserves FK references in StockTransactions
        product = self.get_object()
        product.is_active = False
        product.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


class StockTransactionViewSet(viewsets.ModelViewSet):
    serializer_class = StockTransactionSerializer
    permission_classes = [IsAuthenticated]
    search_fields = ['product__name', 'notes']
    # Stock updates handled entirely by model's save() and delete()

    def get_queryset(self):
        qs = StockTransaction.objects.all()

        product_id = self.request.query_params.get('product')
        if product_id:
            qs = qs.filter(product_id=product_id)

        document_id = self.request.query_params.get('document')
        if document_id:
            qs = qs.filter(document_id=document_id)

        return qs
