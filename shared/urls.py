from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import SettingsViewSet, ContactViewSet, PaymentAccountViewSet

router = DefaultRouter()
# Do NOT register SettingsViewSet in router — it's a singleton, handle manually
router.register('contacts', ContactViewSet,         basename='contacts')
router.register('accounts', PaymentAccountViewSet,  basename='accounts')

urlpatterns = [
    # Singleton settings — explicit method map, no pk needed
    path('settings/', SettingsViewSet.as_view({
        'get':   'list',
        'post':  'create',
        'patch': 'partial_update',
        'put':   'update',
    })),
    path('', include(router.urls)),
]
