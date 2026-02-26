# accounting/urls.py
from rest_framework.routers import DefaultRouter
from .views import DocumentViewSet, FinancialTransactionViewSet, QuickActionViewSet

router = DefaultRouter()
router.register('documents',     DocumentViewSet,             basename='documents')
router.register('transactions',  FinancialTransactionViewSet, basename='transactions')
router.register('quick-actions', QuickActionViewSet,          basename='quick-actions')

urlpatterns = router.urls
