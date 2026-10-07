"""Authoritative contact balances, including pages and filtered ledger windows."""
from decimal import Decimal
from django.db.models import Q, Sum, OuterRef, Subquery, Value, DecimalField
from django.db.models.functions import Coalesce
from .models import FinancialTransaction
from .workflows import CASH_ONLY_TYPES


def ledger_dates(params):
    from rest_framework import serializers
    start = serializers.DateField().run_validation(params['date_from']) if params.get('date_from') else None
    end = serializers.DateField().run_validation(params['date_to']) if params.get('date_to') else None
    if start and end and start > end:
        raise serializers.ValidationError({'date_to': 'Choose an end date on or after the start date.'})
    return start, end


def account_opening_balance(account, start=None):
    rows = FinancialTransaction.objects.filter(payment_account=account)
    if start:
        rows = rows.filter(date__gte=start)
    movement = rows.aggregate(total=Sum('amount'))['total'] or Decimal('0')
    return account.current_balance - movement


def with_running_account_balance(queryset, account):
    prior = FinancialTransaction.objects.filter(payment_account_id=OuterRef('payment_account_id')).filter(
        Q(date__lt=OuterRef('date')) |
        Q(date=OuterRef('date'), created_at__lt=OuterRef('created_at')) |
        Q(date=OuterRef('date'), created_at=OuterRef('created_at'), pk__lte=OuterRef('pk'))
    ).values('payment_account_id').annotate(total=Sum('amount')).values('total')
    field = DecimalField(max_digits=18, decimal_places=2)
    return queryset.annotate(running_balance=Value(account_opening_balance(account), output_field=field) +
        Coalesce(Subquery(prior), Value(Decimal('0')), output_field=field))


def cf_transactions():
    return FinancialTransaction.objects.exclude(document__type__in=CASH_ONLY_TYPES).exclude(type='contra')


def with_running_cf(queryset, opening_balance):
    # Correlated aggregate includes hidden rows and previous pages. A filtered
    # ledger therefore never presents a page subtotal as the contact balance.
    prior = cf_transactions().filter(contact_id=OuterRef('contact_id')).filter(
        Q(date__lt=OuterRef('date')) |
        Q(date=OuterRef('date'), created_at__lt=OuterRef('created_at')) |
        Q(date=OuterRef('date'), created_at=OuterRef('created_at'), pk__lte=OuterRef('pk'))
    ).values('contact_id').annotate(total=Sum('amount')).values('total')
    field = DecimalField(max_digits=18, decimal_places=2)
    return queryset.annotate(running_cf=Value(opening_balance, output_field=field) +
        Coalesce(Subquery(prior), Value(Decimal('0')), output_field=field))
