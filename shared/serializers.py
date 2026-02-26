# shared/serializers.py
from rest_framework import serializers
from .models import Settings, Contact, PaymentAccount


class SettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Settings
        fields = '__all__'


class ContactSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Contact
        fields = '__all__'


class PaymentAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model  = PaymentAccount
        fields = '__all__'
