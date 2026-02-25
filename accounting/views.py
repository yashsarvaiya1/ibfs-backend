# accounting/views.py
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Document, FinancialTransaction
from .serializers import (
    DocumentSerializer, DocumentListSerializer,
    FinancialTransactionSerializer
)
from .services import (
    process_document_create, process_document_delete,
    process_move_stock, process_send_receive,
    _create_ftxn, _recalculate_mcd, _parse_date
)
from shared.models import Contact, PaymentAccount, Settings
from decimal import Decimal


# ─── Document ViewSet ─────────────────────────────────────────────────────────

class DocumentViewSet(viewsets.ModelViewSet):
    search_fields   = ['doc_id', 'contact__contact_name', 'contact__company_name']
    ordering_fields = ['date', 'created_at', 'total_amount']
    ordering        = ['-date']

    def get_queryset(self):
        qs = Document.objects.filter(is_active=True)
        if t := self.request.query_params.get('type'):
            qs = qs.filter(type=t)
        if c := self.request.query_params.get('contact'):
            qs = qs.filter(contact_id=c)
        return qs

    def get_serializer_class(self):
        return DocumentListSerializer if self.action == 'list' else DocumentSerializer

    def create(self, request):
        doc_type   = request.data.get('type')
        contact_id = request.data.get('contact')
        contact    = Contact.objects.get(pk=contact_id) if contact_id else None
        doc        = process_document_create(doc_type, request.data, contact)
        return Response(DocumentSerializer(doc).data, status=status.HTTP_201_CREATED)

    # ── Record Payment ────────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def record_payment(self, request, pk=None):
        """
        Record a payment (actual f.txn) against a document.
        Sign is derived from doc type — user always enters a positive amount.

        Bill / CN  → money goes OUT → actual = −amount
        Invoice / DN → money comes IN → actual = +amount

        Supports optional interest lines (same formula as send/receive).
        """
        doc        = self.get_object()
        amount_raw = Decimal(str(request.data['amount']))
        account_id = request.data.get('payment_account')
        account    = PaymentAccount.objects.get(pk=account_id) if account_id else None
        date       = _parse_date(request.data.get('date'))
        notes      = request.data.get('notes')
        interest_lines = request.data.get('interest_lines', [])

        # Derive direction from document type
        # Bill / CN  = we owe them = we PAY = send = actual negative
        # Invoice/DN = they owe us = we RECEIVE = actual positive
        outgoing_types = {'bill', 'cn', 'cash_payment_voucher'}
        direction  = 'send' if doc.type in outgoing_types else 'receive'
        actual_amt = -amount_raw if direction == 'send' else amount_raw

        result = {}

        # ── Interest record FIRST, then actual ───────────────────────────────
        if interest_lines:
            net = sum(
                Decimal(str(l['amount'])) if l.get('type') == 'charge'
                else -Decimal(str(l['amount']))
                for l in interest_lines
            )
            interest_record_amount = net * (
                Decimal('-1') if direction == 'receive' else Decimal('1')
            )
            interest_doc = Document.objects.create(
                type='interest',
                doc_id=_next_doc_id_local('interest'),
                contact=doc.contact,
                line_items=interest_lines,
                total_amount=abs(net),
                date=date,
                reference=doc,   # interest doc references the original bill/invoice
            )
            int_ftxn = _create_ftxn(
                'record', interest_record_amount,
                doc.contact, None, interest_doc, date,
                'Interest/penalty on payment'
            )
            result['interest_doc']  = interest_doc.pk
            result['interest_ftxn'] = int_ftxn.pk

        # ── Actual payment ────────────────────────────────────────────────────
        ftxn = _create_ftxn(
            'actual', actual_amt, doc.contact, account, doc, date, notes
        )
        result['ftxn'] = ftxn.pk

        return Response(result, status=status.HTTP_201_CREATED)

    # ── Move Stock ────────────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def move_stock(self, request, pk=None):
        doc    = self.get_object()
        result = process_move_stock(doc, request.data)
        return Response(result, status=status.HTTP_201_CREATED)

    # ── Stock Preview ─────────────────────────────────────────────────────────
    @action(detail=True, methods=['get'])
    def stock_preview(self, request, pk=None):
        from inventory.models import StockTransaction
        doc     = self.get_object()
        records = StockTransaction.objects.filter(document=doc, type='record')
        preview = []
        for r in records:
            actuals_sum = sum(
                t.quantity for t in StockTransaction.objects.filter(
                    document=doc, product=r.product, type='actual'
                )
            )
            preview.append({
                'product_id':    r.product_id,
                'product_name':  r.product.name,
                'record_qty':    str(r.quantity),
                'moved_qty':     str(actuals_sum),
                'remaining_qty': str(r.quantity - actuals_sum),
            })
        return Response(preview)

    # ── Add Details ───────────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def add_details(self, request, pk=None):
        from inventory.models import Product
        from .services import _create_stxn
        doc        = self.get_object()
        settings   = Settings.get()
        line_items = request.data.get('line_items', [])
        doc.line_items = line_items
        if not doc.total_amount:
            doc.total_amount = sum(
                Decimal(str(i.get('amount', 0))) for i in line_items
            )
        doc.save()

        sign_map = {'bill': 1, 'invoice': -1, 'cn': 1, 'dn': -1}
        sign     = Decimal(str(sign_map.get(doc.type, 1)))

        for item in line_items:
            pid = item.get('product_id')
            if not pid:
                continue
            try:
                product = Product.objects.get(pk=pid)
            except Product.DoesNotExist:
                continue
            qty      = sign * Decimal(str(item.get('quantity', 0)))
            txn_type = 'actual' if settings.auto_stock else 'record'
            _create_stxn(txn_type, qty, product, doc, doc.date, item.get('rate'))

        return Response(DocumentSerializer(doc).data)

    # ── Delete Document ───────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def delete_document(self, request, pk=None):
        doc      = self.get_object()
        strategy = request.data.get('strategy', 'orphan')
        result   = process_document_delete(doc, strategy)
        return Response(result)


def _next_doc_id_local(doc_type):
    """Local helper to avoid circular import with services."""
    from .services import _next_doc_id
    return _next_doc_id(doc_type)


# ─── FinancialTransaction ViewSet ─────────────────────────────────────────────

class FinancialTransactionViewSet(viewsets.ModelViewSet):
    serializer_class = FinancialTransactionSerializer
    search_fields    = ['contact__contact_name', 'contact__company_name', 'notes']
    ordering_fields  = ['date', 'amount', 'created_at']
    # ✅ CRITICAL: must be ascending for MCD recalculation to be correct
    # Frontend reverses for display — backend always serves oldest→newest
    ordering         = ['date', 'created_at']

    def get_queryset(self):
        qs     = FinancialTransaction.objects.all()
        params = self.request.query_params
        if params.get('contact'):
            qs = qs.filter(contact_id=params['contact'])
        if params.get('account'):
            qs = qs.filter(payment_account_id=params['account'])
        if params.get('type'):
            qs = qs.filter(type=params['type'])
        if params.get('document'):
            qs = qs.filter(document_id=params['document'])
        return qs

    # ── Edit Transaction ──────────────────────────────────────────────────────
    def update(self, request, *args, **kwargs):
        """
        Allow editing amount, date, notes, payment_account on a transaction.
        After edit, recalculate MCD for the affected month.
        If date changed, recalculate BOTH old and new month.
        """
        ftxn     = self.get_object()
        old_date = ftxn.date
        old_amt  = ftxn.amount

        # Only allow safe field edits
        allowed = ['amount', 'date', 'notes', 'payment_account']
        for field in allowed:
            if field in request.data:
                if field == 'amount':
                    new_amt = Decimal(str(request.data['amount']))
                    # Adjust account balance: reverse old, apply new
                    if ftxn.payment_account:
                        ftxn.payment_account.current_balance -= ftxn.amount
                        ftxn.payment_account.current_balance += new_amt
                        ftxn.payment_account.save(update_fields=['current_balance'])
                    ftxn.amount = new_amt
                elif field == 'date':
                    ftxn.date = _parse_date(request.data['date'])
                elif field == 'payment_account':
                    acct_id = request.data['payment_account']
                    # Reverse old account balance
                    if ftxn.payment_account:
                        ftxn.payment_account.current_balance -= ftxn.amount
                        ftxn.payment_account.save(update_fields=['current_balance'])
                    # Apply to new account
                    new_acct = PaymentAccount.objects.get(pk=acct_id) if acct_id else None
                    if new_acct:
                        new_acct.current_balance += ftxn.amount
                        new_acct.save(update_fields=['current_balance'])
                    ftxn.payment_account = new_acct
                else:
                    setattr(ftxn, field, request.data[field])

        ftxn.save()

        # Recalculate MCD — if date changed, recalculate old month too
        _recalculate_mcd(ftxn.contact, ftxn.date)
        if old_date.month != ftxn.date.month or old_date.year != ftxn.date.year:
            _recalculate_mcd(ftxn.contact, old_date)

        return Response(FinancialTransactionSerializer(ftxn).data)

    # ── Delete Transaction ────────────────────────────────────────────────────
    def destroy(self, request, *args, **kwargs):
        """
        Delete a transaction.
        - Reverse account balance if actual type.
        - Recalculate MCD for the month.
        - If it's a record type on an active document, warn but allow.
        """
        ftxn = self.get_object()
        date = ftxn.date

        # Reverse account balance for actual txns
        if ftxn.type == 'actual' and ftxn.payment_account:
            ftxn.payment_account.current_balance -= ftxn.amount
            ftxn.payment_account.save(update_fields=['current_balance'])

        contact = ftxn.contact
        ftxn.delete()

        # Recalculate MCD after deletion
        _recalculate_mcd(contact, date)

        return Response(status=status.HTTP_204_NO_CONTENT)

    # ── Link Document ─────────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def link_document(self, request, pk=None):
        ftxn            = self.get_object()
        ftxn.document_id = request.data.get('document')
        ftxn.save(update_fields=['document'])
        return Response(FinancialTransactionSerializer(ftxn).data)
