from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from .models import Product, StockTransaction
from .serializers import ProductSerializer, StockTransactionSerializer


class ProductViewSet(viewsets.ModelViewSet):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    permission_classes = [IsAuthenticated]


class StockTransactionViewSet(viewsets.ModelViewSet):
    queryset = StockTransaction.objects.all()
    serializer_class = StockTransactionSerializer
    permission_classes = [IsAuthenticated]
    # Stock updates handled entirely by model's save() and delete()
