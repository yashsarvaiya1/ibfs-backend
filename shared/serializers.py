from rest_framework import serializers
from django.conf import settings as django_settings
from .models import Settings, Contact, PaymentAccount


def _build_media_url(request, relative_path):
    """
    Given a relative path like 'uploads/settings/uuid.jpg',
    returns the full absolute URL the frontend can use directly.
    Returns None if relative_path is None/empty.
    """
    if not relative_path:
        return None
    if request:
        return request.build_absolute_uri(f"{django_settings.MEDIA_URL}{relative_path}")
    # Fallback without request context
    return f"{django_settings.MEDIA_URL}{relative_path}"


class SettingsSerializer(serializers.ModelSerializer):
    # Computed absolute URLs for frontend image display — read-only
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
        CF = opening_balance + sum of last MCD per completed month + sum of current month txns.

        Per spec (Part 11):
        c/f = contact.opening_balance
            + sum of (last MCD of each month before current month)
            + sum of (all individual f.txn amounts in current month up to today)

        We calculate the full-to-date CF (no date cutoff) here for the contact list/detail view.
        For the ledger running c/f, the accounting serializer handles it row-by-row.
        """
        from accounting.models import FinancialTransaction
        from django.db.models import Max
        from datetime import date

        today = date.today()

        # --- Past months: use last MCD per month (fast — one row per month) ---
        # Get the ID of the last transaction per year/month for this contact
        # We use Max('id') as a proxy for last-inserted in that month.
        # This is safe because within a month, IDs are always increasing.
        past_month_last_ids = (
            FinancialTransaction.objects
            .filter(contact=obj, date__lt=today.replace(day=1))
            .values('date__year', 'date__month')
            .annotate(last_id=Max('id'))
            .values_list('last_id', flat=True)
        )

        past_mcd_sum = sum(
            t.monthly_cumulative_delta
            for t in FinancialTransaction.objects.filter(id__in=list(past_month_last_ids))
        ) if past_month_last_ids else 0

        # --- Current month: sum all individual amounts (per spec) ---
        current_month_sum = sum(
            t.amount
            for t in FinancialTransaction.objects.filter(
                contact=obj,
                date__year=today.year,
                date__month=today.month,
            )
        )

        cf = obj.opening_balance + past_mcd_sum + current_month_sum
        return str(cf)


class PaymentAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model  = PaymentAccount
        fields = '__all__'
