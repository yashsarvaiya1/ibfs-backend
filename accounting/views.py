import decimal
from django.db import transaction as db_transaction
from django.db.models import F
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from .models import Document, FinancialTransaction, PaymentAccount
from .serializers import DocumentSerializer, FinancialTransactionSerializer, PaymentAccountSerializer


# ── Document types that auto-create a Record transaction on create ─────────────
# Sign: Negative = we owe | Positive = they owe us
RECORD_SIGN = {
    'bill':           decimal.Decimal('-1'),
    'invoice':        decimal.Decimal('1'),
    'cn':             decimal.Decimal('1'),
    'dn':             decimal.Decimal('-1'),
    'cash_voucher':   decimal.Decimal('-1'),
    'income_voucher': decimal.Decimal('1'),
}

# ── Stock direction per document type (used by inventory app) ──────────────────
STOCK_DIRECTION = {
    'bill':    decimal.Decimal('1'),   # IN
    'invoice': decimal.Decimal('-1'),  # OUT
    'cn':      decimal.Decimal('-1'),  # OUT (return to vendor)
    'dn':      decimal.Decimal('1'),   # IN  (return from customer)
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def calculate_document_total(document):
    """Sum line_item amounts + charges + tax - discount"""
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
    """
    Calculate monthly_cumulative_delta for a new transaction.
    Only applies to record/payment types (not contra).
    """
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
    After update/delete: recalculate deltas for all subsequent
    transactions in the same month. Future months are NOT touched.
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


class DocumentViewSet(viewsets.ModelViewSet):
    queryset = Document.objects.all()
    serializer_class = DocumentSerializer
    permission_classes = [IsAuthenticated]

    @db_transaction.atomic
    def perform_create(self, serializer):
        document = serializer.save()
        self._create_record_transaction(document)
        self._create_stock_transactions(document)

    @db_transaction.atomic
    def perform_update(self, serializer):
        old_line_items = self.get_object().line_items or []
        document = serializer.save()
        self._handle_stock_update(document, old_line_items)

    # ── Private helpers ──

    def _create_record_transaction(self, document):
        """Auto-create record transaction for financial document types"""
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

    def _create_stock_transactions(self, document):
        """
        Auto-create StockTransaction for each line_item with a product_id.
        Skipped silently if inventory app not in use (no product_id in items).
        """
        if document.document_type not in STOCK_DIRECTION:
            return
        if not document.document_date:
            return

        direction  = STOCK_DIRECTION[document.document_type]
        line_items = document.line_items or []

        from inventory.models import Product, StockTransaction

        for item in line_items:
            product_id = item.get('product_id')
            if not product_id:
                continue                          # No product selected → skip

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
        """
        On document update: calculate qty diff per product
        and create adjustment StockTransactions for the difference only.
        """
        if document.document_type not in STOCK_DIRECTION:
            return
        if not document.document_date:
            return

        direction = STOCK_DIRECTION[document.document_type]

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
            diff = (new_map.get(pid, decimal.Decimal('0')) - old_map.get(pid, decimal.Decimal('0'))) * direction
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
    queryset = FinancialTransaction.objects.all()
    serializer_class = FinancialTransactionSerializer
    permission_classes = [IsAuthenticated]

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

        # Update payment account balance for payment/contra
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

        txn = serializer.save()

        # Reverse old account effect → apply new account effect
        if old_account and old_type in ('payment', 'contra'):
            PaymentAccount.objects.filter(pk=old_account.pk).update(
                current_balance=F('current_balance') - old_amount
            )
        if txn.payment_account and txn.transaction_type in ('payment', 'contra'):
            PaymentAccount.objects.filter(pk=txn.payment_account.pk).update(
                current_balance=F('current_balance') + txn.amount
            )

        # Recalculate monthly delta (same month only, future months untouched)
        if txn.contact and txn.transaction_type in ('record', 'payment'):
            recalculate_monthly_deltas(
                txn.contact,
                txn.transaction_date.year,
                txn.transaction_date.month,
                txn.transaction_date,
            )

    @db_transaction.atomic
    def perform_destroy(self, instance):
        contact  = instance.contact
        date     = instance.transaction_date
        txn_type = instance.transaction_type
        amount   = instance.amount
        account  = instance.payment_account

        instance.delete()

        # Reverse account balance
        if account and txn_type in ('payment', 'contra'):
            PaymentAccount.objects.filter(pk=account.pk).update(
                current_balance=F('current_balance') - amount
            )

        # Recalculate monthly delta
        if contact and txn_type in ('record', 'payment'):
            recalculate_monthly_deltas(
                contact, date.year, date.month, date
            )
