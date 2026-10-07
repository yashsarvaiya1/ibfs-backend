# accounting/models.py
from django.db import models, transaction
from django.core.serializers.json import DjangoJSONEncoder
from shared.models import BaseModel


class Document(BaseModel):
    TYPE_CHOICES = [
        ("bill",                 "Bill"),
        ("invoice",              "Invoice"),
        ("po",                   "Purchase Order"),
        ("pi",                   "Proforma Invoice"),
        ("quotation",            "Quotation"),
        ("challan",              "Challan"),
        ("cn",                   "Credit Note"),
        ("dn",                   "Debit Note"),
        ("cash_payment_voucher", "Cash Payment Voucher"),
        ("cash_receipt_voucher", "Cash Receipt Voucher"),
        ("interest",             "Interest"),
        ("expense",              "Expense"),
        ("income",               "Income"),
    ]
    type            = models.CharField(max_length=30, choices=TYPE_CHOICES)
    doc_id          = models.CharField(max_length=50, unique=True)
    contact         = models.ForeignKey(
        "shared.Contact", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="documents",
    )
    consignee       = models.ForeignKey(
        "shared.Contact", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="consignee_documents",
    )
    reference       = models.ForeignKey(
        "self", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="referenced_by",
    )
    line_items      = models.JSONField(default=list, blank=True)
    total_amount    = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    discount        = models.DecimalField(max_digits=15, decimal_places=2, default=0, blank=True)
    discount_percentage = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    charges         = models.JSONField(default=list, blank=True)
    taxes           = models.JSONField(default=list, blank=True)
    tax_mode = models.CharField(max_length=10, default='document', choices=[('document', 'Whole document'), ('item', 'Per item')])
    supply_category = models.CharField(max_length=20, null=True, blank=True, choices=[('taxable', 'Taxable domestic'), ('nil_rated', 'Nil rated'), ('exempt', 'Exempt'), ('non_gst', 'Non GST'), ('export', 'Export'), ('import', 'Import')])
    supplier_invoice_number = models.CharField(max_length=100, null=True, blank=True)
    date            = models.DateField()
    due_date        = models.DateField(null=True, blank=True)
    payment_terms   = models.CharField(max_length=255, blank=True, null=True)
    place_of_supply = models.CharField(max_length=100, blank=True, null=True)
    reverse_charge = models.BooleanField(blank=True, null=True, default=None)
    attachment_urls = models.JSONField(default=list, blank=True)
    notes           = models.TextField(blank=True, null=True)
    is_active       = models.BooleanField(default=True)
    stock_mode = models.CharField(max_length=10, null=True, blank=True,
        choices=[('none', 'No stock'), ('record', 'Move stock later'), ('actual', 'Automatic stock')])
    # Manual paid flag — display only, no f.txn / balance effect
    is_paid         = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        from .history import capture_revision
        adding = self._state.adding
        with transaction.atomic():
            if not adding and self.pk:
                previous = type(self).objects.select_for_update().get(pk=self.pk)
                if not previous.revisions.exists():
                    capture_revision(previous, 'baseline')
            super().save(*args, **kwargs)
            # update_fields may leave unsaved attributes on self; record DB facts.
            persisted = type(self).objects.get(pk=self.pk)
            capture_revision(persisted, 'created' if adding else 'updated')

    class Meta:
        ordering = ['-date', '-created_at']             # DV-01: latest first

    def __str__(self):
        return f"{self.type.upper()} #{self.doc_id}"


class FinancialTransaction(BaseModel):
    TYPE_CHOICES = [("record", "Record"), ("actual", "Actual"), ("contra", "Contra")]
    type            = models.CharField(max_length=10, choices=TYPE_CHOICES)
    date            = models.DateField()
    amount          = models.DecimalField(max_digits=15, decimal_places=2)
    document        = models.ForeignKey(
        Document, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="transactions",
    )
    contact         = models.ForeignKey(
        "shared.Contact", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="transactions",
    )
    payment_account = models.ForeignKey(
        "shared.PaymentAccount", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="transactions",
    )
    notes                    = models.TextField(blank=True, null=True)
    transfer_group = models.UUIDField(null=True,blank=True,db_index=True)
    is_reversed = models.BooleanField(default=False)
    monthly_cumulative_delta = models.DecimalField(max_digits=15, decimal_places=2, default=0)

    class Meta:
        ordering = ['-date', '-created_at']             # BF-01: newest first everywhere

    def __str__(self):
        return f"{self.type} {self.amount}"


class PaymentAllocation(BaseModel):
    payment = models.ForeignKey(FinancialTransaction, on_delete=models.CASCADE, related_name='allocations')
    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name='payment_allocations')
    # Positive settles an obligation; negative reverses settlement (refund/discount).
    amount = models.DecimalField(max_digits=15, decimal_places=2)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['payment', 'document'], name='unique_payment_document_allocation')]


class DocumentRevision(models.Model):
    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name='revisions')
    recorded_at = models.DateTimeField(auto_now_add=True)
    event = models.CharField(max_length=20)
    snapshot = models.JSONField(encoder=DjangoJSONEncoder)
    changed_fields = models.JSONField(default=list)

    class Meta:
        ordering = ['-id']
