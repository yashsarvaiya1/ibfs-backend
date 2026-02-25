# accounting/models.py
from django.db import models
from shared.models import BaseModel


class Document(BaseModel):
    TYPE_CHOICES = [
        ('bill', 'Bill'), ('invoice', 'Invoice'),
        ('po', 'Purchase Order'), ('pi', 'Proforma Invoice'),
        ('quotation', 'Quotation'), ('challan', 'Challan'),
        ('cn', 'Credit Note'), ('dn', 'Debit Note'),
        ('cash_payment_voucher', 'Cash Payment Voucher'),
        ('cash_receipt_voucher', 'Cash Receipt Voucher'),
        ('interest', 'Interest'), ('expense', 'Expense'),
    ]
    type = models.CharField(max_length=30, choices=TYPE_CHOICES)
    doc_id = models.CharField(max_length=50, unique=True)
    # ✅ String references to shared models
    contact = models.ForeignKey(
        'shared.Contact', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='documents'
    )
    consignee = models.ForeignKey(
        'shared.Contact', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='consignee_documents'
    )
    reference = models.ForeignKey(
        'self', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='referenced_by'
    )
    line_items = models.JSONField(default=list, blank=True)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    discount = models.DecimalField(max_digits=15, decimal_places=2, default=0, blank=True)
    charges = models.JSONField(default=list, blank=True)
    taxes = models.JSONField(default=list, blank=True)
    date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    payment_terms = models.CharField(max_length=255, blank=True, null=True)
    attachment_urls = models.JSONField(default=list, blank=True)
    notes = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.type.upper()} #{self.doc_id}"


class FinancialTransaction(BaseModel):
    TYPE_CHOICES = [('record', 'Record'), ('actual', 'Actual'), ('contra', 'Contra')]
    type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    date = models.DateField()
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    document = models.ForeignKey(
        Document, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='transactions'
    )
    contact = models.ForeignKey(
        'shared.Contact', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='transactions'
    )
    payment_account = models.ForeignKey(
        'shared.PaymentAccount', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='transactions'
    )
    notes = models.TextField(blank=True, null=True)
    monthly_cumulative_delta = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    is_doc_deleted = models.BooleanField(default=False)

    class Meta:
        ordering = ['date', 'created_at']

    def __str__(self):
        return f"{self.type} {self.amount}"
