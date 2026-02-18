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
    # Both methods coexist — no conflict
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

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} (Stock: {self.current_stock} {self.unit or ""})'


class StockTransaction(models.Model):

    # CASCADE: when document deleted → stock transactions deleted too
    # BUT stock is NOT auto-reversed (user must fix manually)
    # null=True: allows manual adjustments without a document
    document = models.ForeignKey(
        'accounting.Document',
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='stock_transactions'
    )
    product = models.ForeignKey(
        'inventory.Product',
        on_delete=models.CASCADE,
        related_name='stock_transactions'
    )

    # Positive (+) = Stock IN  (bill, dn, challan-from-bill)
    # Negative (-) = Stock OUT (invoice, cn, challan-from-invoice)
    quantity = models.DecimalField(max_digits=15, decimal_places=3)

    transaction_date = models.DateField()

    # Rate at time of transaction — reference only, not used in calculations
    rate = models.DecimalField(
        max_digits=15, decimal_places=2,
        null=True, blank=True
    )
    notes = models.TextField(blank=True, null=True)

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
            # Reverse stock on delete
            # Note: when document is deleted → CASCADE deletes this
            # BUT current_stock is NOT auto-reversed (by design)
            # This delete() only runs on MANUAL deletion by user
            Product.objects.filter(pk=self.product_id).update(
                current_stock=models.F('current_stock') - self.quantity
            )
            super().delete(*args, **kwargs)
