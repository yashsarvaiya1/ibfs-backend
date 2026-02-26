# accounting/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import DocumentViewSet, FinancialTransactionViewSet

router = DefaultRouter()
router.register('documents',     DocumentViewSet,             basename='documents')
router.register('transactions',  FinancialTransactionViewSet, basename='transactions')

urlpatterns = [
    path('', include(router.urls)),
]
