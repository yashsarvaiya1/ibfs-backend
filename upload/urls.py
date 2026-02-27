from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import UploadViewSet

router = DefaultRouter()
router.register(r'upload', UploadViewSet, basename='upload')  # ← no slashes

urlpatterns = [
    path('', include(router.urls)),
]
