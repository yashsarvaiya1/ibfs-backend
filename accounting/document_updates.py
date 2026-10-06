"""Edits preserve IBFS automation and keep cash separate from obligations."""
from collections import defaultdict
from decimal import Decimal
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from shared.models import Contact, Settings
from inventory.models import Product, StockTransaction
from .models import Document, FinancialTransaction
from .commands import DocumentWriteSerializer, EditConflict
from .calculations import decimal_value, document_totals
from .workflows import FINANCIAL_SIGNS, stock_mode, stock_direction
from .services import _create_ftxn, _create_stxn, _recalculate_mcd


def _quantities(items):
    result = defaultdict(Decimal)
    for item in items or []:
        if item.get('product_id'):
            result[int(item['product_id'])] += decimal_value(item.get('quantity'))
    return result


def sync_stock(document, previous_items, force=False):
    sign = stock_direction(document.type, document.reference.type if document.reference else None)
    if sign is None:
        return
    existing = StockTransaction.objects.filter(document=document)
    mode = document.stock_mode
    if mode is None:
        # Historical documents keep their existing mode, even if settings changed.
        mode = 'record' if existing.filter(type='record').exists() else 'actual' if existing.filter(type='actual').exists() else stock_mode(document.type, Settings.get(), document.reference.type if document.reference else None)
        document.stock_mode = mode
        document.save(update_fields=['stock_mode'])
    if mode == 'none':
        return
    quantities = _quantities(document.line_items)
    products = {p.pk: p for p in Product.objects.filter(pk__in=quantities)}
    if mode == 'record':
        # Expected movements are replaceable; real delivery transactions are retained.
        existing.filter(type='record').delete()
        for pid, qty in quantities.items():
            _create_stxn('record', sign * qty, products[pid], document, document.date)
    elif force or quantities != _quantities(previous_items):
        actuals = {row['product_id']: row['total'] for row in existing.filter(type='actual').values('product_id').annotate(total=Sum('quantity'))}
        all_products = {p.pk: p for p in Product.objects.filter(pk__in=set(quantities) | set(actuals))}
        for pid in sorted(all_products):
            difference = sign * quantities.get(pid, Decimal('0')) - actuals.get(pid, Decimal('0'))
            if difference:
                _create_stxn('actual', difference, all_products[pid], document, document.date,
                    notes='Automatic adjustment after document edit')


@transaction.atomic
def update_document(document, payload, preserve_total=False):
    document = Document.objects.select_for_update().get(pk=document.pk)
    serializer = DocumentWriteSerializer(document, data=payload, partial=True)
    serializer.is_valid(raise_exception=True)
    data = dict(serializer.validated_data)
    expected = data.pop('expected_updated_at', None)
    if expected and expected != document.updated_at:
        raise EditConflict()
    old_items, old_date, old_contact = document.line_items, document.date, document.contact
    old_contact_id = document.contact_id
    new_contact = data.get('contact', old_contact)
    list(Contact.objects.select_for_update().filter(pk__in=[pk for pk in (old_contact_id, new_contact.pk if new_contact else None) if pk]).order_by('pk'))
    affected = {(txn.contact_id, txn.date) for txn in document.transactions.all()}
    account = data.pop('payment_account', None)
    for field, value in data.items():
        setattr(document, field, value)
    calculation_changed = any(key in data for key in ('line_items', 'charges', 'taxes', 'discount'))
    if calculation_changed and document.line_items and (not preserve_total or document.total_amount is None):
        document.total_amount = document_totals({key: getattr(document, key) for key in ('line_items','charges','taxes','discount')}, document.type)['total']
    document.save()
    if calculation_changed or 'date' in data or 'reference' in data:
        sync_stock(document, old_items, force='reference' in data)
    if document.type in FINANCIAL_SIGNS:
        records = list(document.transactions.filter(type='record').order_by('pk'))
        amount = FINANCIAL_SIGNS[document.type] * decimal_value(document.total_amount)
        if records:
            primary = records[0]
            primary.amount, primary.date, primary.contact = amount, document.date, document.contact
            primary.save()
            document.transactions.filter(type='record').exclude(pk=primary.pk).delete()
        elif amount:
            _create_ftxn('record', amount, document.contact, document=document, date=document.date)
    elif document.type == 'interest':
        records = list(document.transactions.filter(type='record').order_by('pk'))
        if records:
            # Preserve pay/receive direction; a discount can legitimately reverse the net.
            old_net = document_totals({'line_items':old_items}, 'interest')['subtotal']
            sign = 1 if (records[0].amount >= 0) == (old_net >= 0) else -1
            new_net = document_totals({'line_items':document.line_items}, 'interest')['subtotal']
            records[0].amount = sign * new_net
            records[0].date = document.date
            records[0].save()
    elif document.type == 'expense':
        actuals = list(document.transactions.filter(type='actual').order_by('pk'))
        expected_amount = -decimal_value(document.total_amount)
        if actuals:
            txn = actuals[0]
            other_amount = sum((t.amount for t in actuals[1:]), Decimal('0'))
            new_amount = expected_amount - other_amount
            from django.db.models import F
            from shared.models import PaymentAccount
            new_account = account if 'payment_account' in payload else txn.payment_account
            deltas = defaultdict(Decimal)
            if txn.payment_account_id:
                deltas[txn.payment_account_id] -= txn.amount
            if new_account:
                deltas[new_account.pk] += new_amount
            for pk, delta in sorted(deltas.items()):
                PaymentAccount.objects.filter(pk=pk).update(current_balance=F('current_balance')+delta, updated_at=timezone.now())
            txn.amount, txn.date, txn.payment_account = new_amount, document.date, new_account
            txn.save()
        elif expected_amount:
            _create_ftxn('actual', expected_amount, document.contact, account, document, document.date, force_mcd_zero=True)
    document.transactions.update(contact=document.contact)
    affected |= {(txn.contact_id, txn.date) for txn in document.transactions.all()}
    contacts = {c.pk:c for c in Contact.objects.filter(pk__in={pk for pk,_ in affected if pk})}
    for pk, date in sorted(affected, key=lambda pair: (pair[0] or 0, pair[1])):
        if pk in contacts:
            _recalculate_mcd(contacts[pk], date)
    return document
