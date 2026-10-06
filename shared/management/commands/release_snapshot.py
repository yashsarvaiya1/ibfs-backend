"""Read-only, comparable accounting snapshot for migration/restore review."""
import hashlib
import json
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.models import Sum
from accounting.ledger import cf_transactions
from accounting.models import Document, FinancialTransaction, PaymentAllocation
from inventory.models import Product, StockTransaction
from shared.models import Contact, PaymentAccount


def snapshot():
    models = {
        'documents': (Document, ('id', 'type', 'doc_id', 'contact_id', 'reference_id', 'date', 'total_amount', 'is_active', 'line_items', 'charges', 'taxes', 'discount')),
        'financial_transactions': (FinancialTransaction, ('id', 'type', 'date', 'amount', 'document_id', 'contact_id', 'payment_account_id', 'monthly_cumulative_delta')),
        'allocations': (PaymentAllocation, ('id', 'payment_id', 'document_id', 'amount')),
        'stock_transactions': (StockTransaction, ('id', 'type', 'document_id', 'product_id', 'quantity', 'date', 'rate')),
    }
    result = {'version': 1, 'records': {}}
    for label, (model, fields) in models.items():
        digest = hashlib.sha256()
        count = 0
        for row in model.objects.order_by('pk').values(*fields).iterator(chunk_size=1000):
            digest.update((json.dumps(row, default=str, sort_keys=True, separators=(',', ':')) + '\n').encode())
            count += 1
        result['records'][label] = {'count': count, 'sha256': digest.hexdigest()}
    deltas = dict(cf_transactions().values('contact_id').annotate(total=Sum('amount')).values_list('contact_id', 'total'))
    result['contacts'] = {str(c.pk): str(c.opening_balance + deltas.get(c.pk, Decimal('0'))) for c in Contact.objects.order_by('pk')}
    result['accounts'] = {str(a.pk): str(a.current_balance) for a in PaymentAccount.objects.order_by('pk')}
    result['stock'] = {str(p.pk): str(p.current_stock) for p in Product.objects.order_by('pk')}
    return result


class Command(BaseCommand):
    help = 'Print a read-only accounting snapshot. Save before/after migrations and compare; no automatic corrections.'

    def handle(self, *args, **options):
        with transaction.atomic():
            if connection.vendor == 'postgresql':
                with connection.cursor() as cursor:
                    cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            self.stdout.write(json.dumps(snapshot(), indent=2, sort_keys=True))
