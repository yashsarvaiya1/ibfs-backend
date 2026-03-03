from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import SettingsViewSet, ContactViewSet, PaymentAccountViewSet

router = DefaultRouter()
router.register('contacts', ContactViewSet,        basename='contacts')
router.register('accounts', PaymentAccountViewSet, basename='accounts')

urlpatterns = [
    path('settings/', SettingsViewSet.as_view({
        'get':   'list',
        'post':  'create',
        'patch': 'partial_update',
        'put':   'update',
    })),
    path('', include(router.urls)),
]
