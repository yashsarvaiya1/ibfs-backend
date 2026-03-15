# inventory/serializers.py
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
    is_document_deleted = serializers.SerializerMethodField()
    # ✅ Display fields — eliminates ID-only rendering in frontend cards
    product_name        = serializers.SerializerMethodField()
    doc_id              = serializers.SerializerMethodField()
    doc_type            = serializers.SerializerMethodField()
    contact_name        = serializers.SerializerMethodField()

    class Meta:
        model  = StockTransaction
        fields = '__all__'

    def get_is_document_deleted(self, obj):
        if obj.document_id is None:
            return False
        return not obj.document.is_active

    def get_product_name(self, obj):
        return obj.product.name if obj.product else None

    def get_doc_id(self, obj):
        # Human-readable e.g. "BILL-0001" — never the pk
        return obj.document.doc_id if obj.document else None

    def get_doc_type(self, obj):
        return obj.document.type if obj.document else None

    def get_contact_name(self, obj):
        if not obj.document or not obj.document.contact:
            return None
        c = obj.document.contact
        return c.company_name or c.contact_name


class ProductSerializer(serializers.ModelSerializer):
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
        fields = [
            'id', 'name', 'rate', 'current_stock', 'min_stock',
            'unit', 'is_active', 'image_url', 'image_url_full',
        ]

    def get_image_url_full(self, obj):
        return _build_media_url(self.context.get('request'), obj.image_url)
