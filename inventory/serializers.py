from rest_framework import serializers
from django.conf import settings as django_settings
from .models import Product, StockTransaction


def _build_media_url(request, relative_path):
    if not relative_path:
        return None
    if request:
        return request.build_absolute_uri(f"{django_settings.MEDIA_URL}{relative_path}")
    return f"{django_settings.MEDIA_URL}{relative_path}"


class StockTransactionSerializer(serializers.ModelSerializer):
    # Per spec: "When document.is_active = false, the UI derives and renders a ⚠️ 'Document Deleted'
    # warning badge on the transaction." — computed, never stored as a DB field.
    is_document_deleted = serializers.SerializerMethodField()

    class Meta:
        model  = StockTransaction
        fields = '__all__'

    def get_is_document_deleted(self, obj):
        """
        True  → document exists but is soft-deleted (is_active=False) → show ⚠️ badge
        False → document is active or document is null (no document linked)
        """
        if obj.document_id is None:
            return False
        # document FK is SET_NULL — if document_id is set, document object must exist
        # (system never hard-deletes documents, only sets is_active=False)
        return not obj.document.is_active


class ProductSerializer(serializers.ModelSerializer):
    # Relative path stored in DB — used for cron orphan detection and internal references
    # Full absolute URL — used by frontend to render images directly
    image_url_full = serializers.SerializerMethodField()

    class Meta:
        model  = Product
        fields = '__all__'

    def get_image_url_full(self, obj):
        return _build_media_url(self.context.get('request'), obj.image_url)


class ProductListSerializer(serializers.ModelSerializer):
    image_url_full = serializers.SerializerMethodField()

    class Meta:
        model  = Product
        fields = ['id', 'name', 'rate', 'current_stock', 'min_stock', 'unit', 'is_active', 'image_url', 'image_url_full']

    def get_image_url_full(self, obj):
        return _build_media_url(self.context.get('request'), obj.image_url)
