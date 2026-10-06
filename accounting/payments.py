from decimal import Decimal
from .calculations import money
from .models import PaymentAllocation
from .workflows import FINANCIAL_SIGNS


def allocate_payment(payment, document, amount=None):
    if not document:
        return
    if amount is None:
        sign = FINANCIAL_SIGNS.get(document.type)
        if sign is None:
            return
        amount = -sign * payment.amount
    amount = money(amount)
    if amount:
        PaymentAllocation.objects.update_or_create(payment=payment, document=document, defaults={'amount':amount})


def payment_status(document):
    if document.type not in FINANCIAL_SIGNS:
        return None
    records = sum((txn.amount for txn in document.transactions.all() if txn.type == 'record'), Decimal('0'))
    paid = sum((allocation.amount for allocation in document.payment_allocations.all()), Decimal('0'))
    total = abs(records)
    return {'record':str(total), 'paid':str(paid), 'remaining':str(total-paid),
            'is_paid':total > 0 and paid >= total}


def rescale_allocations(payment, old_amount):
    allocations = list(payment.allocations.order_by('pk'))
    if not allocations or not old_amount or payment.amount == old_amount:
        return
    ratio = payment.amount / old_amount
    target = money(sum((row.amount for row in allocations), Decimal('0')) * ratio)
    assigned = Decimal('0')
    for index, row in enumerate(allocations):
        row.amount = target-assigned if index == len(allocations)-1 else money(row.amount*ratio)
        assigned += row.amount
        if row.amount:
            row.save(update_fields=['amount','updated_at'])
        else:
            row.delete()


def replace_allocations(payment, entries):
    """Reassign principal without losing charge/discount allocations or moving cash."""
    from rest_framework.exceptions import ValidationError
    from .models import Document
    from .services import _recalculate_mcd
    documents = {doc.pk:doc for doc in Document.objects.filter(pk__in=[row['document'] for row in entries],
        is_active=True, type__in=FINANCIAL_SIGNS)}
    if len(documents) != len(entries):
        raise ValidationError({'allocations':'Choose each active bill/invoice/note only once.'})
    contacts = {doc.contact_id for doc in documents.values()}
    if len(contacts) > 1 or (payment.contact_id and contacts and contacts != {payment.contact_id}):
        raise ValidationError({'allocations':'All allocations must belong to the payment contact.'})
    amounts = []
    for row in entries:
        try:
            from rest_framework import serializers
            amount = serializers.DecimalField(max_digits=15, decimal_places=2, min_value=Decimal('0.01')).run_validation(row['amount'])
        except (ValueError, KeyError) as exc:
            raise ValidationError({'allocations':'Enter a valid amount for each document.'}) from exc
        if amount <= 0:
            raise ValidationError({'allocations':'Allocation amounts must be greater than zero.'})
        amounts.append(amount)
    adjustments = sum((row.amount for row in payment.allocations.filter(document__type='interest')), Decimal('0'))
    if sum(amounts, Decimal('0')) + adjustments > abs(payment.amount):
        raise ValidationError({'allocations':'Allocations cannot exceed the payment after charges and discounts.'})
    payment.allocations.filter(document__type__in=FINANCIAL_SIGNS).delete()
    for row, amount in zip(entries, amounts):
        document = documents[row['document']]
        direction = -FINANCIAL_SIGNS[document.type] * (1 if payment.amount > 0 else -1)
        allocate_payment(payment, document, direction * amount)
    if contacts and payment.contact_id is None:
        payment.contact_id = next(iter(contacts))
    first = documents[entries[0]['document']] if entries else None
    if payment.document and payment.document.type in {'cash_payment_voucher','cash_receipt_voucher'}:
        payment.document.reference = first if len(entries) == 1 else None
        payment.document.save(update_fields=['reference','updated_at'])
    else:
        payment.document = first
    payment.save()
    _recalculate_mcd(payment.contact, payment.date)
