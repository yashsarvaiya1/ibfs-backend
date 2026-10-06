from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import DocumentViewSet, FinancialTransactionViewSet
from .reports import ReportViewSet

router = DefaultRouter()
router.register('documents',    DocumentViewSet,             basename='documents')
router.register('transactions', FinancialTransactionViewSet, basename='transactions')
router.register('reports', ReportViewSet, basename='reports')

urlpatterns = [
    path('', include(router.urls)),
]
