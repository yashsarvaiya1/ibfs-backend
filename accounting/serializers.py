# accounting/serializers.py
from rest_framework import serializers
from .models import Document, FinancialTransaction


class FinancialTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = FinancialTransaction
        fields = '__all__'


class DocumentSerializer(serializers.ModelSerializer):
    transactions = FinancialTransactionSerializer(many=True, read_only=True)

    class Meta:
        model = Document
        fields = '__all__'


class DocumentListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Document
        fields = ['id', 'type', 'doc_id', 'contact', 'date', 'total_amount', 'is_active']
