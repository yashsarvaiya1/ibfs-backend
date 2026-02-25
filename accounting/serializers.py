# accounting/serializers.py
from rest_framework import serializers
from .models import Document, FinancialTransaction


class FinancialTransactionSerializer(serializers.ModelSerializer):
    document_type = serializers.SerializerMethodField()

    class Meta:
        model = FinancialTransaction
        fields = '__all__'

    def get_document_type(self, obj):
        return obj.document.type if obj.document else None


class DocumentSerializer(serializers.ModelSerializer):
    transactions = FinancialTransactionSerializer(many=True, read_only=True)

    class Meta:
        model = Document
        fields = '__all__'


class DocumentListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Document
        fields = ['id', 'type', 'doc_id', 'contact', 'date', 'total_amount', 'is_active']


