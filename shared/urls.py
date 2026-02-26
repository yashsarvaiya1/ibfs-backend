# shared/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import SettingsViewSet, ContactViewSet, PaymentAccountViewSet

router = DefaultRouter()
router.register('settings', SettingsViewSet,       basename='settings')
router.register('contacts', ContactViewSet,         basename='contacts')
router.register('accounts', PaymentAccountViewSet,  basename='accounts')

urlpatterns = [
    path('', include(router.urls)),
]
