from django.db import models


class PaymentAccount(models.Model):
    ACCOUNT_TYPE_CHOICES = [
        ('bank', 'Bank'),
        ('upi', 'UPI'),
        ('cash', 'Cash'),
    ]

    name = models.CharField(max_length=255)
    account_type = models.CharField(max_length=10, choices=ACCOUNT_TYPE_CHOICES)
    account_number = models.CharField(max_length=50, blank=True, null=True)
    ifsc_code = models.CharField(max_length=11, blank=True, null=True)
    upi_id = models.CharField(max_length=100, blank=True, null=True)

    # Editable directly by user OR auto-updated via payment/contra transactions
    # Both methods coexist — no conflict
    current_balance = models.DecimalField(
        max_digits=15, decimal_places=2, default=0
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} ({self.account_type})'


class Document(models.Model):
    DOCUMENT_TYPE_CHOICES = [
        ('bill', 'Bill'),
        ('invoice', 'Invoice'),
        ('po', 'Purchase Order'),
        ('pi', 'Performa Invoice'),
        ('challan', 'Challan'),
        ('quotation', 'Quotation'),
        ('cn', 'Credit Note'),
        ('dn', 'Debit Note'),
        ('cash_voucher', 'Cash Voucher'),
        ('income_voucher', 'Income Voucher'),
        ('interest', 'Interest'),
    ]

    document_type = models.CharField(max_length=20, choices=DOCUMENT_TYPE_CHOICES)
    document_number = models.CharField(max_length=100, blank=True, null=True)

    contact = models.ForeignKey(
        'contacts.Contact',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='documents'
    )
    consignee = models.ForeignKey(
        'contacts.Contact',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='consignee_documents'
    )

    # line_items structure varies by document_type:
    # Normal:   [{"name": "Item", "hsn": "1234", "quantity": 10, "rate": 100, "amount": 1000, "product_id": 5}]
    # Challan:  [{"name": "Item", "hsn": "1234", "quantity": 10}]
    # Interest: [{"name": "Late Fee", "amount": 500}]
    line_items = models.JSONField(default=list, blank=True, null=True)

    discount = models.DecimalField(
        max_digits=15, decimal_places=2,
        default=0, blank=True, null=True
    )

    # [{"name": "Shipping", "amount": 100}]
    charges = models.JSONField(default=list, blank=True, null=True)

    # [{"name": "CGST", "percentage": 9}]
    taxes = models.JSONField(default=list, blank=True, null=True)

    document_date = models.DateField(blank=True, null=True)
    due_date = models.DateField(blank=True, null=True)
    payment_terms = models.TextField(blank=True, null=True)

    # Self FK — tracks conversion chain: Quotation → PO → Bill → Challan
    reference = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='related_documents'
    )

    header_image_url = models.URLField(max_length=500, blank=True, null=True)
    signature_image_url = models.URLField(max_length=500, blank=True, null=True)

    # Array of GCS file URLs: ["https://storage.googleapis.com/..."]
    attachment_urls = models.JSONField(default=list, blank=True, null=True)

    notes = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-document_date', '-created_at']

    def __str__(self):
        return f'{self.document_type} #{self.document_number or self.pk}'


class FinancialTransaction(models.Model):
    TRANSACTION_TYPE_CHOICES = [
        ('record', 'Record'),    # Auto-created by backend → LEDGER only
        ('payment', 'Payment'),  # Manually by user → LEDGER + STATEMENT
        ('contra', 'Contra'),    # Fund transfer → STATEMENT only
    ]

    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPE_CHOICES)
    transaction_date = models.DateField()

    # DecimalField natively stores + and - values
    # Negative (-) = we owe / paying out
    # Positive (+) = they owe us / receiving money
    amount = models.DecimalField(max_digits=15, decimal_places=2)

    document = models.ForeignKey(
        'accounting.Document',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='transactions'
    )
    contact = models.ForeignKey(
        'contacts.Contact',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='transactions'
    )
    payment_account = models.ForeignKey(
        'accounting.PaymentAccount',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='transactions'
    )

    notes = models.TextField(blank=True, null=True)

    # Running total within the month for this contact
    # Enables Cash Flow lookup in max 12 DB reads/year
    # Negative = net payable | Positive = net receivable
    monthly_cumulative_delta = models.DecimalField(
        max_digits=15, decimal_places=2, default=0
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['transaction_date', 'created_at']
        indexes = [
            models.Index(fields=['contact', 'transaction_date']),
            models.Index(fields=['payment_account', 'transaction_date']),
            models.Index(fields=['transaction_type', 'transaction_date']),
        ]

    def __str__(self):
        return f'{self.transaction_type} | {self.amount} | {self.transaction_date}'
