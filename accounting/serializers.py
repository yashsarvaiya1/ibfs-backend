# accounting/serializers.py
from decimal import Decimal
from rest_framework import serializers
from .models import Document, FinancialTransaction


class FinancialTransactionSerializer(serializers.ModelSerializer):
    document_type = serializers.SerializerMethodField()

    class Meta:
        model  = FinancialTransaction
        fields = '__all__'

    def get_document_type(self, obj):
        return obj.document.type if obj.document else None


class DocumentSerializer(serializers.ModelSerializer):
    transactions    = FinancialTransactionSerializer(many=True, read_only=True)
    payment_status  = serializers.SerializerMethodField()
    stock_status    = serializers.SerializerMethodField()

    class Meta:
        model  = Document
        fields = '__all__'

    def get_payment_status(self, obj):
        """
        Compares record vs actual f.txns for this document.
        Returns: { record: x, paid: y, remaining: z, is_paid: bool }
        Only meaningful for bill/invoice/cn/dn.
        """
        NO_PAYMENT_TYPES = {
            'po', 'pi', 'quotation', 'challan',
            'interest', 'expense',
            'cash_payment_voucher', 'cash_receipt_voucher',
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
            'cash_payment_voucher', 'cash_receipt_voucher',
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
