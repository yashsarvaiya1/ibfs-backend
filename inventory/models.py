# inventory/models.py
from django.db import models
from shared.models import BaseModel


class Product(BaseModel):
    name          = models.CharField(max_length=255)
    description   = models.TextField(blank=True, null=True)
    image_url     = models.CharField(max_length=500, blank=True, null=True)  # relative media path
    rate          = models.DecimalField(max_digits=15, decimal_places=2)
    current_stock = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    min_stock     = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    hsn_code      = models.CharField(max_length=20, blank=True, null=True)
    unit          = models.CharField(max_length=20, default='pcs')
    is_active     = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class StockTransaction(BaseModel):
    TYPE_CHOICES = [('record', 'Record'), ('actual', 'Actual')]
    type         = models.CharField(max_length=10, choices=TYPE_CHOICES)
    document     = models.ForeignKey(
        'accounting.Document', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='stock_transactions'
    )
    product      = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='stock_transactions')
    quantity     = models.DecimalField(max_digits=15, decimal_places=2)  # signed
    date         = models.DateField()
    rate         = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True)
    notes        = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f"{self.type} {self.quantity} {self.product}"
