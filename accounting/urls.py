# accounting/urls.py
from rest_framework.routers import DefaultRouter
from .views import DocumentViewSet, FinancialTransactionViewSet

router = DefaultRouter()
router.register('documents', DocumentViewSet, basename='documents')
router.register('transactions', FinancialTransactionViewSet, basename='transactions')

urlpatterns = router.urls
