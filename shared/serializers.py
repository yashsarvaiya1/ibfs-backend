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
        FIXED FOR BUG #14: Compute live running CF using the exact MCD engine
        logic from the accounting service. 
        
        Note: We must safely exclude expenses because an expense at the very 
        end of a month forces MCD=0 and would wipe out the month's balance.
        """
        from accounting.models import FinancialTransaction
        
        # Get all distinct year-month combos for this contact, excluding expenses
        months = (
            FinancialTransaction.objects
            .filter(contact=obj)
            .exclude(document__type='expense')
            .dates('date', 'month')
        )

        total_mcd = Decimal('0')
        for month_date in months:
            # Get the last MCD value for this month (highest created_at in month)
            last_txn = (
                FinancialTransaction.objects
                .filter(
                    contact=obj,
                    date__year=month_date.year,
                    date__month=month_date.month,
                )
                .exclude(document__type='expense')
                .order_by('date', 'created_at')
                .last()
            )
            
            # FIX 1: Explicitly check for None to prevent TypeError crashes 
            if last_txn and last_txn.monthly_cumulative_delta is not None:
                total_mcd += Decimal(str(last_txn.monthly_cumulative_delta))

        # FIX 2: Check specifically for 'is not None' instead of relying on truthiness, 
        # because Decimal('0') is falsy and would fail a simple 'if obj.opening_balance' check
        opening = Decimal(str(obj.opening_balance)) if obj.opening_balance is not None else Decimal('0')
        
        return str(opening + total_mcd)


class PaymentAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model  = PaymentAccount
        fields = '__all__'
