# inventory/serializers.py
from rest_framework import serializers
from .models import Product, StockTransaction


class StockTransactionSerializer(serializers.ModelSerializer):
    product_name = serializers.SerializerMethodField()
    document_type = serializers.SerializerMethodField()
    document_doc_id = serializers.SerializerMethodField()
    contact_name = serializers.SerializerMethodField()

    class Meta:
        model  = StockTransaction
        fields = '__all__'

    def get_product_name(self, obj):
        return obj.product.name if obj.product else None

    def get_document_type(self, obj):
        return obj.document.type if obj.document else None

    def get_document_doc_id(self, obj):
        return obj.document.doc_id if obj.document else None

    def get_contact_name(self, obj):
        if obj.document and obj.document.contact:
            c = obj.document.contact
            return c.company_name or c.contact_name
        return None


class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Product
        fields = '__all__'


class ProductListSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Product
        # Added hsn_code for SearchSelect display in frontend
        fields = ['id', 'name', 'rate', 'current_stock', 'min_stock', 'unit', 'hsn_code', 'is_active']
