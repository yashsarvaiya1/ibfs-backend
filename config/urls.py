from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.views.static import serve
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/auth/', include('rest_framework.urls')),
    path('api/', include('shared.urls')),
    path('api/', include('accounting.urls')),
    path('api/', include('inventory.urls')),
    path('api/', include('upload.urls')),
]

from shared.http import private_media, health

urlpatterns += [
    path('health/', health),
    re_path(r'^media/(?P<path>.*)$', private_media),
]
urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
