# accounting/serializers.py

from decimal import Decimal
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
    document_type       = serializers.SerializerMethodField()
    is_document_deleted = serializers.SerializerMethodField()

    class Meta:
        model  = FinancialTransaction
        fields = '__all__'

    def get_document_type(self, obj):
        return obj.document.type if obj.document else None

    def get_is_document_deleted(self, obj):
        if obj.document_id is None:
            return False
        return not obj.document.is_active


# ─── List serializer — lightweight, no heavy nested fields ────────────────────

class DocumentListSerializer(serializers.ModelSerializer):
    contact_name   = serializers.SerializerMethodField()
    payment_status = serializers.SerializerMethodField()
    # ✅ Removed contact_display / consignee_display — not needed on list view
    #    and were declared without being added to Meta.fields (caused AssertionError)

    class Meta:
        model  = Document
        fields = [
            'id', 'type', 'doc_id', 'contact', 'contact_name',
            'date', 'total_amount', 'is_active', 'payment_status',
        ]

    def get_contact_name(self, obj):
        if not obj.contact:
            return None
        return obj.contact.company_name or obj.contact.contact_name

    def get_payment_status(self, obj):
        NO_PAYMENT_TYPES = {
            'po', 'pi', 'quotation', 'challan',
            'interest', 'expense',
        }
        if obj.type in NO_PAYMENT_TYPES:
            return None
        txns   = obj.transactions.all()
        record = abs(sum(t.amount for t in txns if t.type == 'record'))
        paid   = abs(sum(t.amount for t in txns if t.type == 'actual'))
        return {
            'remaining': str(record - paid),
            'is_paid':   paid >= record,
        }


# ─── Detail serializer — full data including contact display ──────────────────

class DocumentSerializer(serializers.ModelSerializer):
    transactions         = FinancialTransactionSerializer(many=True, read_only=True)
    payment_status       = serializers.SerializerMethodField()
    stock_status         = serializers.SerializerMethodField()
    attachment_urls_full = serializers.SerializerMethodField()
    # ✅ Display fields live here only — used by detail page + print page
    contact_display      = serializers.SerializerMethodField()
    consignee_display    = serializers.SerializerMethodField()

    class Meta:
        model  = Document
        fields = '__all__'   # includes all + the declared SerializerMethodFields above

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
        NO_PAYMENT_TYPES = {
            'po', 'pi', 'quotation', 'challan',
            'interest', 'expense',
        }
        if obj.type in NO_PAYMENT_TYPES:
            return None
        txns   = obj.transactions.all()
        record = abs(sum(t.amount for t in txns if t.type == 'record'))
        paid   = abs(sum(t.amount for t in txns if t.type == 'actual'))
        return {
            'record':    str(record),
            'paid':      str(paid),
            'remaining': str(record - paid),
            'is_paid':   paid >= record,
        }

    def get_stock_status(self, obj):
        from inventory.models import StockTransaction
        NO_STOCK_TYPES = {
            'po', 'pi', 'quotation',
            'interest', 'expense',
            'cash_payment_voucher', 'cash_receipt_voucher',
        }
        if obj.type in NO_STOCK_TYPES:
            return None
        records = StockTransaction.objects.filter(
            document=obj, type='record'
        ).select_related('product')
        if not records.exists():
            return None
        result = []
        for r in records:
            moved = abs(sum(
                t.quantity for t in StockTransaction.objects.filter(
                    document=obj, product=r.product, type='actual'
                )
            ))
            record_qty = abs(r.quantity)
            result.append({
                'product_id':    r.product_id,
                'product_name':  r.product.name,
                'record_qty':    str(record_qty),
                'moved_qty':     str(moved),
                'remaining_qty': str(record_qty - moved),
                'is_moved':      moved >= record_qty,
            })
        return result
