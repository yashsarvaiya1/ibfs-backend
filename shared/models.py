# shared/models.py
from django.db import models


class BaseModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Settings(BaseModel):
    header_image = models.URLField(blank=True, null=True)
    sign_image = models.URLField(blank=True, null=True)
    auto_stock = models.BooleanField(default=True)
    auto_transaction = models.BooleanField(default=True)
    enable_po = models.BooleanField(default=False)
    enable_quotation = models.BooleanField(default=False)
    enable_pi = models.BooleanField(default=False)
    enable_challan = models.BooleanField(default=False)
    enable_vouchers = models.BooleanField(default=False)
    enable_interest = models.BooleanField(default=False)
    enable_cn = models.BooleanField(default=False)
    enable_dn = models.BooleanField(default=False)

    class Meta:
        verbose_name = 'Settings'
        verbose_name_plural = 'Settings'

    def save(self, *args, **kwargs):
        self.pk = 1  # singleton
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Contact(BaseModel):
    company_name = models.CharField(max_length=255, blank=True, null=True)
    contact_name = models.CharField(max_length=255)
    phone = models.CharField(max_length=20)
    additional_contacts = models.JSONField(default=list, blank=True)
    opening_balance = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    gstin = models.CharField(max_length=15, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.company_name or self.contact_name


class PaymentAccount(BaseModel):
    TYPE_CHOICES = [('bank', 'Bank'), ('upi', 'UPI'), ('cash', 'Cash')]
    type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    name = models.CharField(max_length=255)
    account_number = models.CharField(max_length=50, blank=True, null=True)
    ifsc_code = models.CharField(max_length=20, blank=True, null=True)
    upi_id = models.CharField(max_length=100, blank=True, null=True)
    current_balance = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name
