# inventory/serializers.py
from rest_framework import serializers
from .models import Product, StockTransaction


class StockTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model  = StockTransaction
        fields = '__all__'


class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Product
        fields = '__all__'


class ProductListSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Product
        fields = ['id', 'name', 'rate', 'current_stock', 'min_stock', 'unit', 'is_active']
