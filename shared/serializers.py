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
           + sum of (last MCD of each completed month, skipping expense txns)
           + sum of (all individual f.txn amounts in current month, skipping expense)

        Per spec Part 11:
          - expense f.txns have MCD forced to 0 — must be excluded from past-month lookup
          - contra f.txns have no contact — never appear here
          - The "last MCD" of a month = monthly_cumulative_delta of the last non-expense
            txn ordered by (date, created_at) in that month
        """
        from accounting.models import FinancialTransaction
        from django.db.models import Max
        from datetime import date

        today     = date.today()
        month_start = today.replace(day=1)

        # ── Past months: get last non-expense txn per month ───────────────────
        # We need the last (date, created_at) row per (year, month) that is NOT expense.
        # Strategy: get all non-expense txns before this month, ordered desc,
        # then pick the first per (year, month) group using Python — fast enough
        # since MCD makes this at most N_months rows after grouping.

        past_txns = (
            FinancialTransaction.objects
            .filter(contact=obj, date__lt=month_start)
            .exclude(document__type='expense')   # ✅ skip expense — MCD is always 0
            .order_by('date__year', 'date__month', '-date', '-created_at')
            .values('date__year', 'date__month', 'monthly_cumulative_delta')
        )

        # Pick last row per (year, month) — first in desc ordering = last in asc
        seen_months = set()
        past_mcd_sum = 0
        for row in past_txns:
            key = (row['date__year'], row['date__month'])
            if key not in seen_months:
                seen_months.add(key)
                past_mcd_sum += row['monthly_cumulative_delta']

        # ── Current month: sum individual amounts, skip expense ───────────────
        current_month_sum = sum(
            t.amount
            for t in FinancialTransaction.objects.filter(
                contact=obj,
                date__year=today.year,
                date__month=today.month,
            ).exclude(document__type='expense')   # ✅ expense never affects CF
            .select_related('document')
        )

        cf = obj.opening_balance + past_mcd_sum + current_month_sum
        return str(cf)


class PaymentAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model  = PaymentAccount
        fields = '__all__'
