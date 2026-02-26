# config/urls.py
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/auth/', include('rest_framework.urls')),
    path('api/', include('shared.urls')),
    path('api/', include('accounting.urls')),
    path('api/', include('inventory.urls')),
    path('api/', include('upload.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
