# accounting/serializers.py
from decimal import Decimal
from django.db.models import Sum
from rest_framework import serializers
from django.conf import settings as django_settings
from .models import Document, FinancialTransaction


def _build_media_url(request, relative_path):
    if not relative_path:
        return None
    if request:
        return request.build_absolute_uri(f"{django_settings.MEDIA_URL}{relative_path}")
    return f"{django_settings.MEDIA_URL}{relative_path}"


class FinancialTransactionSerializer(serializers.ModelSerializer):
    running_cf = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True, required=False)
    running_balance = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True, required=False)
    allocations = serializers.SerializerMethodField()
    document_type        = serializers.SerializerMethodField()
    is_document_deleted  = serializers.SerializerMethodField()
    contact_name         = serializers.SerializerMethodField()
    payment_account_name = serializers.SerializerMethodField()
    payment_account_type = serializers.SerializerMethodField()
    doc_id               = serializers.SerializerMethodField()

    class Meta:
        model  = FinancialTransaction
        fields = '__all__'
        read_only_fields = ['monthly_cumulative_delta','transfer_group','is_reversed']

    def validate(self, attrs):
        document = attrs.get('document', self.instance.document if self.instance else None)
        contact = attrs.get('contact', self.instance.contact if self.instance else None)
        if document and contact and document.contact_id != contact.pk:
            raise serializers.ValidationError({'document': 'Choose a document belonging to this contact.'})
        return attrs

    def get_document_type(self, obj):
        return obj.document.type if obj.document else None

    def get_allocations(self, obj):
        return [{'document':row.document_id, 'doc_id':row.document.doc_id,
                 'document_type':row.document.type, 'amount':str(row.amount)}
                for row in obj.allocations.all()]

    def get_is_document_deleted(self, obj):
        # document_id check avoids any DB hit when document is null
        if not obj.document_id:
            return False
        # document should be select_related by the viewset queryset
        return not obj.document.is_active

    def get_contact_name(self, obj):
        if not obj.contact:
            return None
        return obj.contact.company_name or obj.contact.contact_name

    def get_payment_account_name(self, obj):
        return obj.payment_account.name if obj.payment_account else None

    def get_payment_account_type(self, obj):
        return obj.payment_account.type if obj.payment_account else None

    def get_doc_id(self, obj):
        return obj.document.doc_id if obj.document else None


# ─── List serializer ──────────────────────────────────────────────────────────

class DocumentListSerializer(serializers.ModelSerializer):
    contact_name   = serializers.SerializerMethodField()
    payment_status = serializers.SerializerMethodField()

    class Meta:
        model  = Document
        fields = [
            'id', 'type', 'doc_id', 'contact', 'contact_name',
            'date', 'due_date', 'total_amount', 'is_active', 'is_paid',
            'payment_status',
        ]

    def get_contact_name(self, obj):
        if not obj.contact:
            return None
        return obj.contact.company_name or obj.contact.contact_name

    def get_payment_status(self, obj):
        from .payments import payment_status
        return payment_status(obj)


# ─── Detail serializer ────────────────────────────────────────────────────────

class DocumentSerializer(serializers.ModelSerializer):
    calculated_totals = serializers.SerializerMethodField()

    def get_calculated_totals(self, obj):
        from .calculations import document_totals
        try:
            return document_totals({key: getattr(obj, key) for key in ('line_items', 'charges', 'taxes', 'discount', 'discount_percentage', 'tax_mode')}, obj.type)
        except (ValueError, TypeError, ArithmeticError):
            return None

    transactions         = serializers.SerializerMethodField()
    payment_status       = serializers.SerializerMethodField()
    stock_status         = serializers.SerializerMethodField()
    attachment_urls_full = serializers.SerializerMethodField()
    contact_display      = serializers.SerializerMethodField()
    consignee_display    = serializers.SerializerMethodField()

    class Meta:
        model  = Document
        fields = '__all__'

    def get_transactions(self, obj):
        transactions = {txn.pk:txn for txn in obj.transactions.all()}
        for allocation in obj.payment_allocations.all():
            transactions[allocation.payment_id] = allocation.payment
        rows = sorted(transactions.values(), key=lambda txn: (txn.date, txn.created_at, txn.pk), reverse=True)
        return FinancialTransactionSerializer(rows, many=True, context=self.context).data

    def get_attachment_urls_full(self, obj):
        request = self.context.get('request')
        return [_build_media_url(request, path) for path in (obj.attachment_urls or [])]

    def get_contact_display(self, obj):
        if not obj.contact:
            return None
        c          = obj.contact
        all_phones = [{'name': c.company_name or c.contact_name, 'number': c.phone, 'role': 'primary'}]
        for ac in (c.additional_contacts or []):
            all_phones.append({
                'name':   ac.get('name', ''),
                'number': ac.get('number', ''),
                'role':   ac.get('role', ''),
            })
        return {
            'name':       c.company_name or c.contact_name,
            'phone':      c.phone,
            'gstin':      c.gstin,
            'address':    c.address,
            'all_phones': all_phones,
        }

    def get_consignee_display(self, obj):
        if not obj.consignee:
            return None
        c = obj.consignee
        return {
            'name':    c.company_name or c.contact_name,
            'phone':   c.phone,
            'address': c.address,
        }

    def get_payment_status(self, obj):
        from .payments import payment_status
        return payment_status(obj)

    def get_stock_status(self, obj):
        from .stock_status import document_stock_status
        return document_stock_status(obj) or None
