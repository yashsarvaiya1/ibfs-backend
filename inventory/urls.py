from rest_framework.routers import DefaultRouter
from .views import ProductViewSet, StockTransactionViewSet

router = DefaultRouter()
router.register(r'products', ProductViewSet, basename='product')
router.register(r'stock-transactions', StockTransactionViewSet, basename='stock-transaction')

urlpatterns = router.urls
