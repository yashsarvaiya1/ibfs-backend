import decimal
from django.db import transaction as db_transaction
from django.db.models import F
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Document, FinancialTransaction, PaymentAccount
from .serializers import DocumentSerializer, FinancialTransactionSerializer, PaymentAccountSerializer


# ── Document types that auto-create a Record transaction on create ─────────────
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
    'bill':    decimal.Decimal('1'),
    'invoice': decimal.Decimal('-1'),
    'cn':      decimal.Decimal('-1'),
    'dn':      decimal.Decimal('1'),
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def calculate_document_total(document):
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
    Recalculate monthly_cumulative_delta for all record/payment transactions
    from from_date onwards within the same month only.
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


class DocumentViewSet(viewsets.ModelViewSet):
    serializer_class = DocumentSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # Default: active documents only
        # Pass ?include_inactive=true to see soft-deleted ones
        qs = Document.objects.all()
        if self.request.query_params.get('include_inactive', 'false').lower() != 'true':
            qs = qs.filter(is_active=True)

        # Optional filter by document_type
        doc_type = self.request.query_params.get('type')
        if doc_type:
            qs = qs.filter(document_type=doc_type)

        # Optional filter by contact
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

        # Update the linked record transaction if total or date changed
        self._update_record_transaction(document, old_total, old_date)

        # Handle stock quantity adjustments
        self._handle_stock_update(document, old_line_items)

    def destroy(self, request, *args, **kwargs):
        """
        Soft delete — use delete_with_resolution for documents
        that have linked transactions and stock entries.
        Simple soft delete for documents with no linked entries.
        """
        document = self.get_object()

        has_payment_txns = document.transactions.filter(
            transaction_type__in=['payment', 'contra']
        ).exists()
        has_stock_txns = document.stock_transactions.exists()

        if has_payment_txns or has_stock_txns:
            return Response(
                {
                    'error': 'This document has linked transactions.',
                    'detail': 'Use /delete_with_resolution/ to handle linked entries.',
                    'has_payment_transactions': has_payment_txns,
                    'has_stock_transactions': has_stock_txns,
                },
                status=status.HTTP_409_CONFLICT
            )

        # No linked payment/stock entries — safe to soft delete directly
        # Auto-delete the record transaction (no user choice needed)
        record_txn = document.transactions.filter(transaction_type='record').first()
        if record_txn:
            contact  = record_txn.contact
            date     = record_txn.transaction_date
            record_txn.delete()
            recalculate_monthly_deltas(contact, date.year, date.month, date)

        document.is_active = False
        document.save()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'], url_path='delete_with_resolution')
    @db_transaction.atomic
    def delete_with_resolution(self, request, pk=None):
        """
        Full deletion flow for documents with linked transactions/stock.

        POST /api/accounting/documents/{id}/delete_with_resolution/
        {
            "payment_transaction_actions": {
                "15": "revert",   // delete txn + reverse account balance
                "16": "keep"      // set document FK to null, keep as orphan
            },
            "stock_transaction_actions": {
                "8": "revert",    // delete stock txn + reverse product stock
                "9": "keep"       // set document FK to null, keep as orphan
            }
        }
        """
        document = self.get_object()

        payment_actions = request.data.get('payment_transaction_actions', {})
        stock_actions   = request.data.get('stock_transaction_actions', {})

        # ── Step 1: Always delete record transactions + recalculate delta ──────
        record_txns = document.transactions.filter(transaction_type='record')
        contact     = None
        earliest_date = None

        for txn in record_txns:
            if contact is None:
                contact = txn.contact
            if earliest_date is None or txn.transaction_date < earliest_date:
                earliest_date = txn.transaction_date
            txn.delete()

        # Recalculate monthly delta once after all record txns are deleted
        if contact and earliest_date:
            recalculate_monthly_deltas(
                contact,
                earliest_date.year,
                earliest_date.month,
                earliest_date
            )

        # ── Step 2: Handle payment/contra transactions ────────────────────────
        payment_txns = document.transactions.filter(
            transaction_type__in=['payment', 'contra']
        )

        payment_contact     = None
        payment_earliest    = None

        for txn in payment_txns:
            action_choice = payment_actions.get(str(txn.pk), 'keep')

            if action_choice == 'revert':
                # Reverse the payment account balance
                if txn.payment_account and txn.transaction_type in ('payment', 'contra'):
                    PaymentAccount.objects.filter(pk=txn.payment_account.pk).update(
                        current_balance=F('current_balance') - txn.amount
                    )

                # Track earliest date for delta recalculation
                if txn.contact:
                    if payment_contact is None:
                        payment_contact = txn.contact
                    if payment_earliest is None or txn.transaction_date < payment_earliest:
                        payment_earliest = txn.transaction_date

                txn.delete()

            else:
                # Keep — orphan the transaction (unlink from document)
                txn.document = None
                txn.save(update_fields=['document'])

        # Recalculate delta once for payment transactions after all are processed
        if payment_contact and payment_earliest:
            recalculate_monthly_deltas(
                payment_contact,
                payment_earliest.year,
                payment_earliest.month,
                payment_earliest
            )

        # ── Step 3: Handle stock transactions ─────────────────────────────────
        from inventory.models import StockTransaction, Product

        stock_txns = document.stock_transactions.all()

        for stk in stock_txns:
            action_choice = stock_actions.get(str(stk.pk), 'keep')

            if action_choice == 'revert':
                # Reverse product stock by calling model delete()
                # which handles the stock reversal automatically
                stk.delete()
            else:
                # Keep — orphan the stock transaction
                stk.document = None
                stk.save(update_fields=['document'])

        # ── Step 4: Soft delete the document ──────────────────────────────────
        document.is_active = False
        document.save()

        return Response(
            {'status': 'deleted', 'document_id': document.pk},
            status=status.HTTP_200_OK
        )

    # ── Private helpers ────────────────────────────────────────────────────────

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
        """
        When a document is edited, update the linked record transaction
        amount and recalculate monthly deltas if amount or date changed.
        """
        if document.document_type not in RECORD_SIGN:
            return
        if not document.document_date:
            return

        record_txn = document.transactions.filter(transaction_type='record').first()
        if not record_txn:
            # Was missing — create it now
            self._create_record_transaction(document)
            return

        new_total  = calculate_document_total(document)
        new_amount = RECORD_SIGN[document.document_type] * new_total

        if record_txn.amount == new_amount and record_txn.transaction_date == document.document_date:
            return  # Nothing changed — skip

        record_txn.amount           = new_amount
        record_txn.transaction_date = document.document_date
        record_txn.save(update_fields=['amount', 'transaction_date'])

        # Recalculate from the earlier of old/new date
        recalc_from = min(old_date, document.document_date) if old_date else document.document_date
        recalculate_monthly_deltas(
            document.contact,
            recalc_from.year,
            recalc_from.month,
            recalc_from
        )

    def _create_stock_transactions(self, document):
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
    serializer_class = FinancialTransactionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = FinancialTransaction.objects.all()

        # Filter by contact
        contact_id = self.request.query_params.get('contact')
        if contact_id:
            qs = qs.filter(contact_id=contact_id)

        # Filter by type
        txn_type = self.request.query_params.get('type')
        if txn_type:
            qs = qs.filter(transaction_type=txn_type)

        # Filter by payment account
        account_id = self.request.query_params.get('account')
        if account_id:
            qs = qs.filter(payment_account_id=account_id)

        # Filter by date range
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

        # Reverse old → apply new account balance
        if old_account and old_type in ('payment', 'contra'):
            PaymentAccount.objects.filter(pk=old_account.pk).update(
                current_balance=F('current_balance') - old_amount
            )
        if txn.payment_account and txn.transaction_type in ('payment', 'contra'):
            PaymentAccount.objects.filter(pk=txn.payment_account.pk).update(
                current_balance=F('current_balance') + txn.amount
            )

        # Recalculate from the earlier of old/new date in same month
        if txn.contact and txn.transaction_type in ('record', 'payment'):
            recalc_from = min(old_date, txn.transaction_date)
            recalculate_monthly_deltas(
                txn.contact,
                recalc_from.year,
                recalc_from.month,
                recalc_from
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
