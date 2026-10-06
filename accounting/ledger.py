"""Authoritative contact balances, including pages and filtered ledger windows."""
from decimal import Decimal
from django.db.models import Q, Sum, OuterRef, Subquery, Value, DecimalField
from django.db.models.functions import Coalesce
from .models import FinancialTransaction


def cf_transactions():
    return FinancialTransaction.objects.exclude(document__type='expense').exclude(type='contra')


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
