# inventory/models.py

from django.db import models
from django.db import transaction as db_transaction


class Product(models.Model):
    name = models.CharField(max_length=255, unique=True)
    description = models.TextField(blank=True, null=True)
    image_url = models.URLField(max_length=500, blank=True, null=True)

    # Default rate for quick fill in documents
    rate = models.DecimalField(
        max_digits=15, decimal_places=2,
        default=0, blank=True, null=True
    )

    # Editable directly by user OR auto-updated via StockTransactions
    current_stock = models.DecimalField(
        max_digits=15, decimal_places=3, default=0
    )

    # Low stock alert threshold (frontend handles the alert logic)
    minimum_stock = models.DecimalField(
        max_digits=15, decimal_places=3,
        default=0, blank=True, null=True
    )

    hsn_code = models.CharField(max_length=20, blank=True, null=True)
    unit = models.CharField(
        max_length=20, blank=True, null=True,
        help_text='kg, pcs, liters, etc.'
    )
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} (Stock: {self.current_stock} {self.unit or ""})'


class StockTransaction(models.Model):
    document = models.ForeignKey(
        'accounting.Document',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='stock_transactions'
    )
    product = models.ForeignKey(
        'inventory.Product',
        on_delete=models.CASCADE,
        related_name='stock_transactions'
    )

    # Positive (+) = Stock IN  (bill, dn)
    # Negative (-) = Stock OUT (invoice, cn)
    quantity = models.DecimalField(max_digits=15, decimal_places=3)

    transaction_date = models.DateField()

    # Rate at time of transaction — reference only, not used in calculations
    rate = models.DecimalField(
        max_digits=15, decimal_places=2,
        null=True, blank=True
    )
    notes = models.TextField(blank=True, null=True)

    # True when the linked document has been soft-deleted
    # Shown to user as reference info only — "stock came from deleted Bill #101"
    # User can freely re-link to a new document anytime
    is_document_deleted = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['transaction_date', 'created_at']

    def __str__(self):
        direction = 'IN' if self.quantity > 0 else 'OUT'
        return f'{self.product.name} | {direction} {abs(self.quantity)}'

    def save(self, *args, **kwargs):
        with db_transaction.atomic():
            if self.pk:
                # UPDATE: apply only the difference to avoid double counting
                old = StockTransaction.objects.select_for_update().get(pk=self.pk)
                diff = self.quantity - old.quantity
                Product.objects.filter(pk=self.product_id).update(
                    current_stock=models.F('current_stock') + diff
                )
            else:
                # CREATE: add full quantity to stock
                Product.objects.filter(pk=self.product_id).update(
                    current_stock=models.F('current_stock') + self.quantity
                )
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        with db_transaction.atomic():
            # Reverses stock only on MANUAL deletion
            # (called by delete_with_resolution when user picks "revert")
            Product.objects.filter(pk=self.product_id).update(
                current_stock=models.F('current_stock') - self.quantity
            )
            super().delete(*args, **kwargs)
