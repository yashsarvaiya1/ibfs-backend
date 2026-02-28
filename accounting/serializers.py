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
    document_type = serializers.SerializerMethodField()
    # Per spec: warning badge derived from document.is_active — never stored as DB field
    is_document_deleted = serializers.SerializerMethodField()

    class Meta:
        model  = FinancialTransaction
        fields = '__all__'

    def get_document_type(self, obj):
        return obj.document.type if obj.document else None

    def get_is_document_deleted(self, obj):
        """
        True  → document FK exists but document is soft-deleted (is_active=False) → show ⚠️ badge
        False → no document linked, or document is active
        Per spec Part 1: "When document.is_active = false, the UI derives and renders
        a ⚠️ 'Document Deleted' warning badge on the transaction."
        """
        if obj.document_id is None:
            return False
        return not obj.document.is_active


class DocumentSerializer(serializers.ModelSerializer):
    transactions         = FinancialTransactionSerializer(many=True, read_only=True)
    payment_status       = serializers.SerializerMethodField()
    stock_status         = serializers.SerializerMethodField()
    # Relative paths stored in DB — used by cron for orphan detection
    # Full absolute URLs — used by frontend to display/download attachments
    attachment_urls_full = serializers.SerializerMethodField()

    class Meta:
        model  = Document
        fields = '__all__'

    def get_attachment_urls_full(self, obj):
        """
        Converts each relative path in attachment_urls to an absolute URL.
        attachment_urls stores: ["uploads/documents/uuid.pdf", ...]
        Frontend receives:      ["http://host/media/uploads/documents/uuid.pdf", ...]
        """
        request = self.context.get('request')
        return [_build_media_url(request, path) for path in (obj.attachment_urls or [])]

    def get_payment_status(self, obj):
        """
        Compares record vs actual f.txns for this document.
        Returns: { record: x, paid: y, remaining: z, is_paid: bool }
        Only meaningful for bill/invoice/cn/dn/cash_payment_voucher/cash_receipt_voucher.
        """
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
        """
        Compares record vs actual s.txns for this document.
        Returns per-product pending info. Only for docs that move stock.
        """
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


class DocumentListSerializer(serializers.ModelSerializer):
    contact_name   = serializers.SerializerMethodField()
    payment_status = serializers.SerializerMethodField()

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
