# accounting/serializers.py
from rest_framework import serializers
from .models import Document, FinancialTransaction
from inventory.models import StockTransaction


class FinancialTransactionSerializer(serializers.ModelSerializer):
    document_type        = serializers.SerializerMethodField()
    contact_name         = serializers.SerializerMethodField()
    payment_account_name = serializers.SerializerMethodField()

    class Meta:
        model  = FinancialTransaction
        fields = '__all__'

    def get_document_type(self, obj):
        return obj.document.type if obj.document else None

    def get_contact_name(self, obj):
        if not obj.contact:
            return None
        return obj.contact.company_name or obj.contact.contact_name

    def get_payment_account_name(self, obj):
        return obj.payment_account.name if obj.payment_account else None


class DocumentSerializer(serializers.ModelSerializer):
    # Only f.txns for non-challan docs — challan has no f.txns by design
    # Frontend should also guard, but we filter here for safety
    transactions  = serializers.SerializerMethodField()
    contact_name  = serializers.SerializerMethodField()
    reference_doc_id = serializers.SerializerMethodField()

    class Meta:
        model  = Document
        fields = '__all__'

    def get_transactions(self, obj):
        # Challan never has f.txns — return empty list to avoid confusion
        if obj.type == 'challan':
            return []
        txns = obj.transactions.all()
        return FinancialTransactionSerializer(txns, many=True).data

    def get_contact_name(self, obj):
        if not obj.contact:
            return None
        return obj.contact.company_name or obj.contact.contact_name

    def get_reference_doc_id(self, obj):
        return obj.reference.doc_id if obj.reference else None


class DocumentListSerializer(serializers.ModelSerializer):
    contact_name   = serializers.SerializerMethodField()
    stock_status   = serializers.SerializerMethodField()

    class Meta:
        model  = Document
        fields = [
            'id', 'type', 'doc_id', 'contact', 'contact_name',
            'date', 'total_amount', 'is_active', 'stock_status',
        ]

    def get_contact_name(self, obj):
        if not obj.contact:
            return None
        return obj.contact.company_name or obj.contact.contact_name

    def get_stock_status(self, obj):
        """
        Returns stock movement status for documents that have s.txns.
        Used to separate pending vs fully-stocked docs in Challan list
        and Move Stock views.

        Returns:
          'na'      — document type never has stock (expense, interest, etc.)
          'pending' — record s.txns exist with remaining qty > 0
          'partial' — some stock moved but not all
          'done'    — all record qty matched by actuals (or auto_stock was ON)
        """
        stock_types = {'bill', 'invoice', 'cn', 'dn', 'challan'}
        if obj.type not in stock_types:
            return 'na'

        records = StockTransaction.objects.filter(document=obj, type='record')
        if not records.exists():
            # auto_stock was ON — actuals created directly, no records
            # Check if any actuals exist
            has_actuals = StockTransaction.objects.filter(
                document=obj, type='actual'
            ).exists()
            return 'done' if has_actuals else 'na'

        total_record = sum(abs(r.quantity) for r in records)
        total_actual = sum(
            abs(t.quantity)
            for t in StockTransaction.objects.filter(document=obj, type='actual')
        )

        if total_actual == 0:
            return 'pending'
        if total_actual >= total_record:
            return 'done'
        return 'partial'
