# shared/serializers.py
from rest_framework import serializers
from decimal import Decimal
from .models import Settings, Contact, PaymentAccount


class SettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Settings
        fields = '__all__'


class ContactSerializer(serializers.ModelSerializer):
    current_cf = serializers.SerializerMethodField()

    class Meta:
        model  = Contact
        fields = '__all__'

    def get_current_cf(self, obj):
        """
        CF = opening_balance + SUM of last MCD per month up to today.
        Reads only one row per month (last txn of that month) — fast.
        """
        from accounting.models import FinancialTransaction
        from django.db.models import Max

        # Get last created_at per month/year for this contact
        month_maxes = (
            FinancialTransaction.objects
            .filter(contact=obj)
            .values('date__year', 'date__month')
            .annotate(last_id=Max('id'))
            .values_list('last_id', flat=True)
        )
        if not month_maxes:
            return str(obj.opening_balance)

        total_mcd = sum(
            t.monthly_cumulative_delta
            for t in FinancialTransaction.objects.filter(id__in=month_maxes)
        )
        cf = obj.opening_balance + total_mcd
        return str(cf)


class PaymentAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model  = PaymentAccount
        fields = '__all__'
