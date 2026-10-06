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

    def validate_letterhead_height_mm(self, value):
        if not 15 <= value <= 65:
            raise serializers.ValidationError('Use a height between 15 and 65 mm.')
        return value

    def validate_letterhead_footer_mm(self, value):
        if not 10 <= value <= 40:
            raise serializers.ValidationError('Use a footer space between 10 and 40 mm.')
        return value

    def validate_company_gstin(self, value):
        import re
        value = value.strip().upper()
        if value and not re.fullmatch(r'[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]', value):
            raise serializers.ValidationError('Enter a valid 15-character GSTIN.')
        return value

    def _validate_branding(self, value):
        from pathlib import Path
        from PIL import Image, UnidentifiedImageError
        if not value:
            return value
        root = Path(django_settings.MEDIA_ROOT).resolve()
        path = (root / value).resolve()
        if not path.is_relative_to(root / 'uploads' / 'settings') or not path.is_file():
            raise serializers.ValidationError('Upload a branding image before selecting it.')
        try:
            with Image.open(path) as image:
                image.verify()
        except (UnidentifiedImageError, OSError, ValueError):
            raise serializers.ValidationError('Letterhead and signature must be PNG, JPEG or WebP images.')
        return value

    def validate_header_image(self, value):
        return self._validate_branding(value)

    def validate_sign_image(self, value):
        return self._validate_branding(value)

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
        from accounting.ledger import cf_transactions
        from django.db.models import Sum
        from decimal import Decimal
        delta = getattr(obj, 'cf_delta', None)
        if delta is None:
            delta = cf_transactions().filter(contact=obj).aggregate(total=Sum('amount'))['total'] or Decimal('0')
        return str(obj.opening_balance + delta)



class PaymentAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model  = PaymentAccount
        fields = '__all__'
