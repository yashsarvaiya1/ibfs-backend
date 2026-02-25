# inventory/urls.py
from rest_framework.routers import DefaultRouter
from .views import ProductViewSet, StockTransactionViewSet

router = DefaultRouter()
router.register('products', ProductViewSet, basename='products')
router.register('stock-transactions', StockTransactionViewSet, basename='stock-transactions')

urlpatterns = router.urls
