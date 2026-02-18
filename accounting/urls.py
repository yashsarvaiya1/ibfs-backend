from rest_framework.routers import DefaultRouter
from .views import PaymentAccountViewSet, DocumentViewSet, FinancialTransactionViewSet

router = DefaultRouter()
router.register(r'payment-accounts', PaymentAccountViewSet, basename='payment-account')
router.register(r'documents', DocumentViewSet, basename='document')
router.register(r'transactions', FinancialTransactionViewSet, basename='transaction')

urlpatterns = router.urls
