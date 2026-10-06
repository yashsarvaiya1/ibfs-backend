from decimal import Decimal
from django.db.models import Sum
from inventory.models import StockTransaction


def document_stock_status(document):
    records = StockTransaction.objects.filter(document=document, type='record').values(
        'product_id', 'product__name').annotate(total=Sum('quantity'))
    actuals = {row['product_id']: abs(row['total']) for row in StockTransaction.objects.filter(
        document=document, type='actual').values('product_id').annotate(total=Sum('quantity'))}
    result = []
    for record in records:
        expected = abs(record['total'])
        moved = actuals.get(record['product_id'], Decimal('0'))
        result.append({'product_id':record['product_id'], 'product_name':record['product__name'],
            'record_qty':str(expected), 'moved_qty':str(moved),
            'remaining_qty':str(max(expected-moved, Decimal('0'))), 'is_moved':moved>=expected,
            'direction':'in' if record['total'] > 0 else 'out'})
    return result
