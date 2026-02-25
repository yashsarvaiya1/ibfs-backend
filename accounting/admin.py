# accounting/admin.py
from django.contrib import admin
from .models import Document, FinancialTransaction
admin.site.register(Document)
admin.site.register(FinancialTransaction)
