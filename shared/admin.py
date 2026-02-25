# shared/admin.py
from django.contrib import admin
from .models import Settings, Contact, PaymentAccount
admin.site.register(Settings)
admin.site.register(Contact)
admin.site.register(PaymentAccount)
