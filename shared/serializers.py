# shared/serializers.py
from rest_framework import serializers
from django.conf import settings as django_settings
from .models import Settings, Contact, PaymentAccount


def _build_media_url(request, relative_path):
    if not relative_path:
        return None
    if request:
        return request.build_absolute_uri(f"{django_settings.MEDIA_URL}{relative_path}")
    return f"{django_settings.MEDIA_URL}{relative_path}"


class SettingsSerializer(serializers.ModelSerializer):
    header_image_url = serializers.SerializerMethodField()
    sign_image_url   = serializers.SerializerMethodField()

    class Meta:
        model  = Settings
        fields = '__all__'

    def get_header_image_url(self, obj):
        return _build_media_url(self.context.get('request'), obj.header_image)

    def get_sign_image_url(self, obj):
        return _build_media_url(self.context.get('request'), obj.sign_image)


class ContactSerializer(serializers.ModelSerializer):
    current_cf = serializers.SerializerMethodField()

    class Meta:
        model  = Contact
        fields = '__all__'

    def get_current_cf(self, obj):
        """
        CF = opening_balance
           + sum of (last MCD of each completed month, excluding expense txns)
           + sum of (all individual f.txn amounts in current month, excluding expense)

        Per spec Part 11:
          - expense f.txns → MCD forced to 0, excluded from CF
          - contra f.txns  → no contact, never appear here
        """
        from accounting.models import FinancialTransaction
        from datetime import date

        today       = date.today()
        month_start = today.replace(day=1)

        # ── Past months: get last non-expense txn per (year, month) ──────────
        # BF-03: 'date__year' / 'date__month' are NOT valid Django order_by fields.
        # Fix: order by '-date', '-created_at' — Python grouping handles month bucketing.
        past_txns = (
            FinancialTransaction.objects
            .filter(contact=obj, date__lt=month_start)
            .exclude(document__type='expense')
            .order_by('-date', '-created_at')           # ✅ valid ORM ordering
            .values('date__year', 'date__month', 'monthly_cumulative_delta')
        )

        # First row per (year, month) in DESC order = last row in ASC = correct MCD
        seen_months  = set()
        past_mcd_sum = 0
        for row in past_txns:
            key = (row['date__year'], row['date__month'])
            if key not in seen_months:
                seen_months.add(key)
                past_mcd_sum += row['monthly_cumulative_delta']

        # ── Current month: sum individual amounts, exclude expense ────────────
        current_month_sum = sum(
            t.amount
            for t in FinancialTransaction.objects
            .filter(contact=obj, date__year=today.year, date__month=today.month)
            .exclude(document__type='expense')
            .select_related('document')
        )

        cf = obj.opening_balance + past_mcd_sum + current_month_sum
        return str(cf)


class PaymentAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model  = PaymentAccount
        fields = '__all__'
