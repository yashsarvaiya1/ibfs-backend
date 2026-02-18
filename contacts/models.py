from django.db import models


class Contact(models.Model):
    company_name = models.CharField(max_length=255, blank=True, null=True)
    contact_name = models.CharField(max_length=255, blank=True, null=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    additional_contacts = models.JSONField(
        default=list, blank=True, null=True,
        help_text='[{"name": "John", "phone": "9876543210"}]'
    )
    opening_balance = models.DecimalField(
        max_digits=15, decimal_places=2,
        default=0, blank=True, null=True,
        help_text='Negative = we owe them | Positive = they owe us'
    )
    gstin = models.CharField(max_length=15, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.company_name or self.contact_name or f'Contact #{self.pk}'
