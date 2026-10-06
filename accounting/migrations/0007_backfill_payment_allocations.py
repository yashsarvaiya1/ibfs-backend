from decimal import Decimal
from django.db import migrations
from django.db.models import Sum


def backfill(apps, schema_editor):
    Transaction = apps.get_model('accounting', 'FinancialTransaction')
    Allocation = apps.get_model('accounting', 'PaymentAllocation')
    Document = apps.get_model('accounting', 'Document')
    signs = {'bill':1, 'invoice':-1, 'cn':1, 'dn':-1}
    batch = []
    for payment in Transaction.objects.filter(type='actual', document__type__in=signs).select_related('document').iterator(chunk_size=500):
        amount = -signs[payment.document.type] * payment.amount
        if amount:
            batch.append(Allocation(payment_id=payment.pk, document_id=payment.document_id, amount=amount))
        if len(batch) >= 500:
            Allocation.objects.bulk_create(batch, ignore_conflicts=True)
            batch.clear()
    Allocation.objects.bulk_create(batch, ignore_conflicts=True)
    # Settlement is derived. Retain manual closure only where it is needed.
    for doc in Document.objects.filter(is_paid=True, type__in=signs).iterator(chunk_size=500):
        record = abs(Transaction.objects.filter(document=doc, type='record').aggregate(total=Sum('amount'))['total'] or Decimal('0'))
        paid = Allocation.objects.filter(document=doc).aggregate(total=Sum('amount'))['total'] or Decimal('0')
        if record > 0 and paid >= record:
            Document.objects.filter(pk=doc.pk).update(is_paid=False)


class Migration(migrations.Migration):
    dependencies = [('accounting','0006_paymentallocation')]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
