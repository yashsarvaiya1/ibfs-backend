import decimal
from django.db import transaction as db_transaction
from django.db.models import F
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Document, FinancialTransaction, PaymentAccount
from .serializers import DocumentSerializer, FinancialTransactionSerializer, PaymentAccountSerializer


# ── Document types that auto-create a Record transaction ──────────────────────
# Sign: Negative = we owe | Positive = they owe us
RECORD_SIGN = {
    'bill':           decimal.Decimal('-1'),
    'invoice':        decimal.Decimal('1'),
    'cn':             decimal.Decimal('1'),
    'dn':             decimal.Decimal('-1'),
    'cash_voucher':   decimal.Decimal('-1'),
    'income_voucher': decimal.Decimal('1'),
}

# ── Stock direction per document type ─────────────────────────────────────────
STOCK_DIRECTION = {
    'bill':    decimal.Decimal('1'),   # IN
    'invoice': decimal.Decimal('-1'),  # OUT
    'cn':      decimal.Decimal('-1'),  # OUT (return to vendor)
    'dn':      decimal.Decimal('1'),   # IN  (return from customer)
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def calculate_document_total(document):
    """Sum line_item amounts + charges + taxes - discount"""
    line_items = document.line_items or []
    charges    = document.charges or []
    taxes      = document.taxes or []
    discount   = document.discount or decimal.Decimal('0')

    subtotal = sum(
        decimal.Decimal(str(item.get('amount', 0)))
        for item in line_items
        if item.get('amount') is not None
    )
    total_charges = sum(
        decimal.Decimal(str(c.get('amount', 0))) for c in charges
    )
    total_tax = sum(
        subtotal * decimal.Decimal(str(t.get('percentage', 0))) / decimal.Decimal('100')
        for t in taxes
    )
    return subtotal + total_charges + total_tax - discount


def get_monthly_delta(contact, date, amount, txn_type):
    """Calculate monthly_cumulative_delta for a new transaction."""
    if not contact or txn_type not in ('record', 'payment'):
        return amount

    last = FinancialTransaction.objects.filter(
        contact=contact,
        transaction_date__year=date.year,
        transaction_date__month=date.month,
        transaction_date__lte=date,
        transaction_type__in=['record', 'payment']
    ).order_by('-transaction_date', '-created_at').first()

    return (last.monthly_cumulative_delta + amount) if last else amount


def recalculate_monthly_deltas(contact, year, month, from_date):
    """
    Recalculate monthly_cumulative_delta for all record/payment
    transactions from from_date onwards within the same month only.
    Future months are NOT touched — they recalculate independently.
    """
    if not contact:
        return

    previous = FinancialTransaction.objects.filter(
        contact=contact,
        transaction_date__year=year,
        transaction_date__month=month,
        transaction_date__lt=from_date,
        transaction_type__in=['record', 'payment']
    ).order_by('-transaction_date', '-created_at').first()

    running = previous.monthly_cumulative_delta if previous else decimal.Decimal('0')

    subsequent = FinancialTransaction.objects.filter(
        contact=contact,
        transaction_date__year=year,
        transaction_date__month=month,
        transaction_date__gte=from_date,
        transaction_type__in=['record', 'payment']
    ).order_by('transaction_date', 'created_at')

    for txn in subsequent:
        running += txn.amount
        FinancialTransaction.objects.filter(pk=txn.pk).update(
            monthly_cumulative_delta=running
        )


# ── ViewSets ───────────────────────────────────────────────────────────────────

class PaymentAccountViewSet(viewsets.ModelViewSet):
    queryset = PaymentAccount.objects.all()
    serializer_class = PaymentAccountSerializer
    permission_classes = [IsAuthenticated]
    search_fields = ['name', 'account_number', 'upi_id']


class DocumentViewSet(viewsets.ModelViewSet):
    serializer_class = DocumentSerializer
    permission_classes = [IsAuthenticated]
    search_fields = ['document_number', 'notes', 'contact__company_name', 'contact__contact_name']

    def get_queryset(self):
        qs = Document.objects.all()

        if self.request.query_params.get('include_inactive', 'false').lower() != 'true':
            qs = qs.filter(is_active=True)

        doc_type = self.request.query_params.get('type')
        if doc_type:
            qs = qs.filter(document_type=doc_type)

        contact_id = self.request.query_params.get('contact')
        if contact_id:
            qs = qs.filter(contact_id=contact_id)

        return qs

    @db_transaction.atomic
    def perform_create(self, serializer):
        document = serializer.save()
        self._create_record_transaction(document)
        self._create_stock_transactions(document)

    @db_transaction.atomic
    def perform_update(self, serializer):
        old_doc        = self.get_object()
        old_line_items = old_doc.line_items or []
        old_total      = calculate_document_total(old_doc)
        old_date       = old_doc.document_date

        document = serializer.save()

        self._update_record_transaction(document, old_total, old_date)
        self._handle_stock_update(document, old_line_items)

    def destroy(self, request, *args, **kwargs):
        """
        Simple destroy for documents with no stock transactions.
        Documents with stock transactions must use delete_with_resolution.
        Payment transactions are ALWAYS kept — document FK cleared + flagged.
        Record transactions are ALWAYS deleted.
        """
        document = self.get_object()

        has_stock_txns = document.stock_transactions.exists()

        if has_stock_txns:
            return Response(
                {
                    'error': 'This document has linked stock transactions.',
                    'detail': 'Use /delete_with_resolution/ to handle stock entries.',
                    'has_stock_transactions': True,
                    'stock_transactions': list(
                        document.stock_transactions.values(
                            'id', 'product_id', 'quantity',
                            'transaction_date', 'notes'
                        )
                    ),
                },
                status=status.HTTP_409_CONFLICT
            )

        self._delete_document_core(document)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'], url_path='delete_with_resolution')
    @db_transaction.atomic
    def delete_with_resolution(self, request, pk=None):
        """
        Deletion flow for documents with linked stock transactions.

        Payment transactions are ALWAYS kept (document FK cleared + flagged).
        Record transactions are ALWAYS deleted.
        Only stock transactions need user resolution.

        POST /api/accounting/documents/{id}/delete_with_resolution/
        {
            "stock_transaction_actions": {
                "8": "revert",   // delete + reverse product stock
                "9": "keep"      // unlink + flag is_document_deleted=True
            }
        }
        """
        document      = self.get_object()
        stock_actions = request.data.get('stock_transaction_actions', {})

        from inventory.models import StockTransaction

        for stk in document.stock_transactions.all():
            action_choice = stock_actions.get(str(stk.pk), 'keep')

            if action_choice == 'revert':
                # model delete() auto-reverses product.current_stock
                stk.delete()
            else:
                # Keep — unlink + flag for user reference
                stk.document            = None
                stk.is_document_deleted = True
                stk.save(update_fields=['document', 'is_document_deleted'])

        self._delete_document_core(document)

        return Response(
            {'status': 'deleted', 'document_id': document.pk},
            status=status.HTTP_200_OK
        )

    # ── Private helpers ────────────────────────────────────────────────────────

    def _delete_document_core(self, document):
        """
        Shared logic for all deletion paths:
        1. Delete record transactions + recalculate monthly delta
        2. Keep payment/contra transactions — clear FK + flag is_document_deleted
        3. Soft delete the document
        """
        record_txns   = list(document.transactions.filter(transaction_type='record'))
        contact       = None
        earliest_date = None

        for txn in record_txns:
            if contact is None:
                contact = txn.contact
            if earliest_date is None or txn.transaction_date < earliest_date:
                earliest_date = txn.transaction_date
            txn.delete()

        if contact and earliest_date:
            recalculate_monthly_deltas(
                contact,
                earliest_date.year,
                earliest_date.month,
                earliest_date
            )

        # User sees "this payment was for Bill #101 (deleted)"
        # User can freely re-link to any new document anytime
        document.transactions.filter(
            transaction_type__in=['payment', 'contra']
        ).update(
            document=None,
            is_document_deleted=True
        )

        document.is_active = False
        document.save()

    def _get_challan_direction(self, document):
        """
        Returns stock direction for Challan based on reference document type.
        Bill reference  → +1 (IN)
        Invoice reference → -1 (OUT)
        No reference or unrecognized type → None (no auto stock)
        """
        if not document.reference:
            return None
        ref_type = document.reference.document_type
        if ref_type == 'bill':
            return decimal.Decimal('1')
        if ref_type == 'invoice':
            return decimal.Decimal('-1')
        return None

    def _create_record_transaction(self, document):
        if document.document_type not in RECORD_SIGN:
            return
        if not document.document_date:
            return

        amount = RECORD_SIGN[document.document_type] * calculate_document_total(document)
        delta  = get_monthly_delta(document.contact, document.document_date, amount, 'record')

        FinancialTransaction.objects.create(
            transaction_type='record',
            transaction_date=document.document_date,
            amount=amount,
            document=document,
            contact=document.contact,
            monthly_cumulative_delta=delta,
        )

    def _update_record_transaction(self, document, old_total, old_date):
        """Update linked record transaction when document is edited."""
        if document.document_type not in RECORD_SIGN:
            return
        if not document.document_date:
            return

        record_txn = document.transactions.filter(transaction_type='record').first()
        if not record_txn:
            self._create_record_transaction(document)
            return

        new_total  = calculate_document_total(document)
        new_amount = RECORD_SIGN[document.document_type] * new_total

        if record_txn.amount == new_amount and record_txn.transaction_date == document.document_date:
            return  # Nothing changed

        record_txn.amount           = new_amount
        record_txn.transaction_date = document.document_date
        record_txn.save(update_fields=['amount', 'transaction_date'])

        # ── FIX: cross-month date change requires both months recalculated ─
        if old_date:
            old_month = (old_date.year, old_date.month)
            new_month = (document.document_date.year, document.document_date.month)

            if old_month != new_month:
                # Transaction moved to a different month — recalculate BOTH
                recalculate_monthly_deltas(
                    document.contact, old_date.year, old_date.month, old_date
                )
                recalculate_monthly_deltas(
                    document.contact,
                    document.document_date.year,
                    document.document_date.month,
                    document.document_date
                )
            else:
                # Same month — recalculate from the earlier of the two dates
                recalc_from = min(old_date, document.document_date)
                recalculate_monthly_deltas(
                    document.contact,
                    recalc_from.year,
                    recalc_from.month,
                    recalc_from
                )
        else:
            recalculate_monthly_deltas(
                document.contact,
                document.document_date.year,
                document.document_date.month,
                document.document_date
            )

    def _create_stock_transactions(self, document):
        if not document.document_date:
            return

        # ── FIX: Challan direction comes from reference, not STOCK_DIRECTION ─
        if document.document_type == 'challan':
            direction = self._get_challan_direction(document)
            if direction is None:
                return  # No reference = no auto stock (frontend handles manual)
        elif document.document_type in STOCK_DIRECTION:
            direction = STOCK_DIRECTION[document.document_type]
        else:
            return

        from inventory.models import Product, StockTransaction
        line_items = document.line_items or []

        for item in line_items:
            product_id = item.get('product_id')
            if not product_id:
                continue
            try:
                product = Product.objects.get(pk=product_id)
            except Product.DoesNotExist:
                continue

            StockTransaction.objects.create(
                document=document,
                product=product,
                quantity=decimal.Decimal(str(item.get('quantity', 0))) * direction,
                transaction_date=document.document_date,
                rate=decimal.Decimal(str(item['rate'])) if item.get('rate') else None,
            )

    def _handle_stock_update(self, document, old_line_items):
        if not document.document_date:
            return

        # ── FIX: Challan direction comes from reference, not STOCK_DIRECTION ─
        if document.document_type == 'challan':
            direction = self._get_challan_direction(document)
            if direction is None:
                return
        elif document.document_type in STOCK_DIRECTION:
            direction = STOCK_DIRECTION[document.document_type]
        else:
            return

        def build_map(items):
            qty_map = {}
            for item in items:
                pid = item.get('product_id')
                if pid:
                    qty_map[pid] = qty_map.get(pid, decimal.Decimal('0')) + decimal.Decimal(str(item.get('quantity', 0)))
            return qty_map

        old_map  = build_map(old_line_items)
        new_map  = build_map(document.line_items or [])
        all_pids = set(list(old_map.keys()) + list(new_map.keys()))

        from inventory.models import Product, StockTransaction

        for pid in all_pids:
            diff = (
                new_map.get(pid, decimal.Decimal('0')) -
                old_map.get(pid, decimal.Decimal('0'))
            ) * direction
            if diff == 0:
                continue
            try:
                StockTransaction.objects.create(
                    document=document,
                    product=Product.objects.get(pk=pid),
                    quantity=diff,
                    transaction_date=document.document_date,
                    notes='Adjustment from document update',
                )
            except Product.DoesNotExist:
                continue


class FinancialTransactionViewSet(viewsets.ModelViewSet):
    serializer_class = FinancialTransactionSerializer
    permission_classes = [IsAuthenticated]
    search_fields = ['notes', 'contact__company_name', 'contact__contact_name']

    def get_queryset(self):
        qs = FinancialTransaction.objects.all()

        contact_id = self.request.query_params.get('contact')
        if contact_id:
            qs = qs.filter(contact_id=contact_id)

        txn_type = self.request.query_params.get('type')
        if txn_type:
            qs = qs.filter(transaction_type=txn_type)

        account_id = self.request.query_params.get('account')
        if account_id:
            qs = qs.filter(payment_account_id=account_id)

        date_from = self.request.query_params.get('date_from')
        date_to   = self.request.query_params.get('date_to')
        if date_from:
            qs = qs.filter(transaction_date__gte=date_from)
        if date_to:
            qs = qs.filter(transaction_date__lte=date_to)

        return qs

    @db_transaction.atomic
    def perform_create(self, serializer):
        data     = serializer.validated_data
        contact  = data.get('contact')
        date     = data.get('transaction_date')
        txn_type = data.get('transaction_type')
        amount   = data.get('amount', decimal.Decimal('0'))
        account  = data.get('payment_account')

        delta = get_monthly_delta(contact, date, amount, txn_type)
        serializer.save(monthly_cumulative_delta=delta)

        if account and txn_type in ('payment', 'contra'):
            PaymentAccount.objects.filter(pk=account.pk).update(
                current_balance=F('current_balance') + amount
            )

    @db_transaction.atomic
    def perform_update(self, serializer):
        old         = self.get_object()
        old_amount  = old.amount
        old_account = old.payment_account
        old_type    = old.transaction_type
        old_date    = old.transaction_date

        txn = serializer.save()

        # Reverse old balance → apply new balance
        if old_account and old_type in ('payment', 'contra'):
            PaymentAccount.objects.filter(pk=old_account.pk).update(
                current_balance=F('current_balance') - old_amount
            )
        if txn.payment_account and txn.transaction_type in ('payment', 'contra'):
            PaymentAccount.objects.filter(pk=txn.payment_account.pk).update(
                current_balance=F('current_balance') + txn.amount
            )

        # ── FIX: cross-month date change requires both months recalculated ─
        if txn.contact and txn.transaction_type in ('record', 'payment'):
            old_month = (old_date.year, old_date.month)
            new_month = (txn.transaction_date.year, txn.transaction_date.month)

            if old_month != new_month:
                # Transaction moved to a different month — recalculate BOTH
                recalculate_monthly_deltas(
                    txn.contact, old_date.year, old_date.month, old_date
                )
                recalculate_monthly_deltas(
                    txn.contact,
                    txn.transaction_date.year,
                    txn.transaction_date.month,
                    txn.transaction_date
                )
            else:
                # Same month — recalculate from the earlier of the two dates
                recalc_from = min(old_date, txn.transaction_date)
                recalculate_monthly_deltas(
                    txn.contact, recalc_from.year, recalc_from.month, recalc_from
                )

    @db_transaction.atomic
    def perform_destroy(self, instance):
        contact  = instance.contact
        date     = instance.transaction_date
        txn_type = instance.transaction_type
        amount   = instance.amount
        account  = instance.payment_account

        instance.delete()

        if account and txn_type in ('payment', 'contra'):
            PaymentAccount.objects.filter(pk=account.pk).update(
                current_balance=F('current_balance') - amount
            )

        if contact and txn_type in ('record', 'payment'):
            recalculate_monthly_deltas(
                contact, date.year, date.month, date
            )
