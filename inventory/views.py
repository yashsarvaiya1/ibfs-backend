from rest_framework import viewsets, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from .models import Product, StockTransaction
from .serializers import ProductSerializer, StockTransactionSerializer


class ProductViewSet(viewsets.ModelViewSet):
    serializer_class = ProductSerializer
    permission_classes = [IsAuthenticated]

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
    queryset = StockTransaction.objects.all()
    serializer_class = StockTransactionSerializer
    permission_classes = [IsAuthenticated]
    # Stock updates handled entirely by model's save() and delete()
