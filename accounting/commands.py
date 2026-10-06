"""Validated inputs for the existing quick-entry document workflow."""
from decimal import Decimal
from rest_framework import serializers
from rest_framework.exceptions import APIException
from shared.models import PaymentAccount, Contact
from inventory.models import Product
from .models import Document
from .calculations import decimal_value, money, item_discount


class EditConflict(APIException):
    status_code = 409
    default_detail = 'This document changed while you were editing. Reload it before saving.'


class DocumentWriteSerializer(serializers.ModelSerializer):
    discount_percentage = serializers.DecimalField(max_digits=7, decimal_places=4, min_value=Decimal('0'), max_value=Decimal('100'), required=False, allow_null=True)
    payment_account = serializers.PrimaryKeyRelatedField(queryset=PaymentAccount.objects.filter(is_active=True), required=False, allow_null=True)
    expected_updated_at = serializers.DateTimeField(required=False, write_only=True)

    class Meta:
        model = Document
        fields = ['type', 'doc_id', 'contact', 'consignee', 'reference', 'line_items',
            'total_amount', 'charges', 'taxes', 'tax_mode', 'supply_category', 'supplier_invoice_number', 'discount', 'discount_percentage', 'date', 'due_date',
            'payment_terms', 'place_of_supply', 'reverse_charge', 'attachment_urls', 'notes', 'payment_account', 'expected_updated_at']
        extra_kwargs = {'doc_id': {'required': False}, 'date': {'required': False}}

    def validate_line_items(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError('Enter a list of document lines.')
        product_ids = set()
        rows = []
        for item in value:
            if not isinstance(item, dict) or not str(item.get('name', '')).strip():
                raise serializers.ValidationError('Every line needs a description.')
            row = dict(item)
            try:
                for key in ('quantity', 'rate', 'amount', 'discount'):
                    if row.get(key) is not None:
                        number = decimal_value(row[key])
                        if number < 0 or number > Decimal('9999999999999.99'):
                            raise ValueError('Line values must be between 0 and 9,999,999,999,999.99.')
                        row[key] = float(money(number))
                if row.get('amount') is None and row.get('quantity') is not None and row.get('rate') is not None:
                    row['amount'] = float(money(decimal_value(row['quantity']) * decimal_value(row['rate'])))
                if row.get('discount_percentage') is not None:
                    percentage = decimal_value(row['discount_percentage'])
                    if not 0 <= percentage <= 100:
                        raise ValueError('Item discount percentage must be between 0 and 100.')
                    row['discount_percentage'] = float(percentage)
                if row.get('discount') is not None or row.get('discount_percentage') is not None:
                    row['discount'] = float(item_discount(row))
                if row.get('product_id'):
                    row['product_id'] = int(row['product_id'])
                    product_ids.add(row['product_id'])
            except (ValueError, TypeError) as exc:
                raise serializers.ValidationError(str(exc)) from exc
            if row.get('type') not in (None, 'charge', 'discount'):
                raise serializers.ValidationError('Choose charge or discount for interest lines.')
            if 'taxes' in row:
                row['taxes'] = self.validate_taxes(row['taxes'])
            if 'supply_category' in row:
                row['supply_category'] = serializers.ChoiceField(choices=Document._meta.get_field('supply_category').choices, allow_blank=True, allow_null=True).run_validation(row['supply_category'])
            rows.append(row)
        products = {p.pk: p for p in Product.objects.filter(pk__in=product_ids)}
        if product_ids - products.keys():
            raise serializers.ValidationError('One of the selected products no longer exists.')
        for row in rows:
            if row.get('product_id') and not row.get('unit'):
                row['unit'] = products[row['product_id']].unit
        return rows

    def _validate_adjustments(self, value, field):
        if not isinstance(value, list):
            raise serializers.ValidationError('Enter a list.')
        result = []
        for entry in value:
            if not isinstance(entry, dict):
                raise serializers.ValidationError('Each entry needs a name and value.')
            try:
                number = decimal_value(entry.get(field))
            except ValueError as exc:
                raise serializers.ValidationError(str(exc)) from exc
            if number < 0 or (field == 'percentage' and number > 100):
                raise serializers.ValidationError('Enter a positive amount or a tax percentage between 0 and 100.')
            result.append({**entry, field: float(number)})
        return result

    def validate_charges(self, value):
        return self._validate_adjustments(value, 'amount')

    def validate_taxes(self, value):
        return self._validate_adjustments(value, 'percentage')

    def validate(self, attrs):
        if self.instance and attrs.get('type', self.instance.type) != self.instance.type:
            raise serializers.ValidationError({'type': 'Create a linked document to change its type.'})
        if self.instance and 'expected_updated_at' in attrs and attrs['expected_updated_at'] != self.instance.updated_at:
            raise EditConflict()
        contact = attrs.get('contact', self.instance.contact if self.instance else None)
        reference = attrs.get('reference', self.instance.reference if self.instance else None)
        if reference:
            if self.instance and reference.pk == self.instance.pk:
                raise serializers.ValidationError({'reference': 'A document cannot reference itself.'})
            if reference.contact_id and contact and reference.contact_id != contact.pk:
                raise serializers.ValidationError({'reference': 'Choose a reference for the same contact.'})
            visited = set()
            ancestor = reference
            while ancestor and ancestor.pk not in visited:
                if self.instance and ancestor.pk == self.instance.pk:
                    raise serializers.ValidationError({'reference': 'This reference would create a circular document flow.'})
                visited.add(ancestor.pk)
                ancestor = ancestor.reference
        if attrs.get('total_amount') is not None and attrs['total_amount'] < 0:
            raise serializers.ValidationError({'total_amount': 'Enter a positive document amount.'})
        if attrs.get('discount', 0) < 0:
            raise serializers.ValidationError({'discount': 'Discount cannot be negative.'})
        if 'discount' in attrs and 'discount_percentage' not in attrs:
            # Legacy clients explicitly entering a currency discount select amount mode.
            attrs['discount_percentage'] = None
        kind = attrs.get('type', self.instance.type if self.instance else '')
        values = {field: attrs.get(field, getattr(self.instance, field, None) if self.instance else None)
                  for field in ('line_items', 'charges', 'taxes', 'discount', 'discount_percentage', 'tax_mode', 'supply_category')}
        if values['tax_mode'] == 'item' and (kind not in ('bill', 'invoice', 'cn', 'dn', 'quotation', 'po', 'pi') or not values['line_items']):
            raise serializers.ValidationError({'tax_mode': 'Per-item tax requires item details on a bill, invoice, note, quotation or order.'})
        if values['tax_mode'] == 'item' and any(item.get('amount') is None for item in values['line_items'] or []):
            raise serializers.ValidationError({'line_items': 'Every item needs an amount for per-item tax.'})
        if values['line_items'] and kind != 'interest':
            from .calculations import document_totals
            try:
                totals = document_totals(values)
            except (ValueError, TypeError, ArithmeticError) as exc:
                raise serializers.ValidationError({'taxes': str(exc)}) from exc
            serializers.DecimalField(max_digits=15, decimal_places=2, min_value=Decimal('0')).run_validation(totals['total'])
            if values['discount_percentage'] is not None:
                attrs['discount'] = totals['discount']
            no_tax = ('nil_rated', 'exempt', 'non_gst')
            for item in values['line_items']:
                item_taxes = (item.get('taxes') or []) if values['tax_mode'] == 'item' else (values['taxes'] or [])
                if (item.get('supply_category') or values['supply_category']) in no_tax and any(decimal_value(tax.get('percentage')) > 0 for tax in item_taxes):
                    raise serializers.ValidationError({'line_items': 'Nil-rated, exempt and non-GST items cannot have positive tax rates. Use per-item tax for mixed classifications.'})
            if values['supply_category'] in no_tax and totals['tax_total']:
                raise serializers.ValidationError({'supply_category': 'This classification cannot include tax. Clear taxes or change the classification.'})
        if values['discount_percentage'] is not None and not values['line_items']:
            raise serializers.ValidationError({'discount_percentage': 'Percentage discount requires item details.'})
        return attrs


def command_data(serializer):
    data = dict(serializer.validated_data)
    for field in ('contact', 'consignee', 'reference', 'payment_account', 'document'):
        if field in data:
            data[field] = data[field].pk if data[field] else None
    return data


class PaymentCommandSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=15, decimal_places=2, min_value=Decimal('0.01'))
    payment_account = serializers.PrimaryKeyRelatedField(queryset=PaymentAccount.objects.filter(is_active=True), required=False, allow_null=True)
    document = serializers.PrimaryKeyRelatedField(queryset=Document.objects.filter(is_active=True), required=False, allow_null=True)
    date = serializers.DateField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    is_expense = serializers.BooleanField(required=False, default=False)
    line_items = serializers.JSONField(required=False, default=list)
    interest_lines = serializers.JSONField(required=False, default=list)

    def validate_line_items(self, value):
        return DocumentWriteSerializer().validate_line_items(value)

    def validate_interest_lines(self, value):
        rows = DocumentWriteSerializer().validate_line_items(value)
        for row in rows:
            row.setdefault('type', 'charge')
        return rows


class TransferCommandSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=15, decimal_places=2, min_value=Decimal('0.01'))
    from_account = serializers.PrimaryKeyRelatedField(queryset=PaymentAccount.objects.filter(is_active=True))
    to_account = serializers.PrimaryKeyRelatedField(queryset=PaymentAccount.objects.filter(is_active=True))
    date = serializers.DateField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate(self, attrs):
        if attrs['from_account'].pk == attrs['to_account'].pk:
            raise serializers.ValidationError({'to_account':'Choose a different destination account.'})
        return attrs


class StandaloneInterestSerializer(serializers.Serializer):
    contact = serializers.PrimaryKeyRelatedField(queryset=Contact.objects.filter(is_active=True),required=False,allow_null=True)
    reference = serializers.PrimaryKeyRelatedField(queryset=Document.objects.filter(is_active=True),required=False,allow_null=True)
    date = serializers.DateField(required=False)
    toggle = serializers.ChoiceField(choices=['we_pay','we_receive'],default='we_receive')
    line_items = serializers.JSONField()

    def validate_line_items(self, value):
        rows=DocumentWriteSerializer().validate_line_items(value)
        if not rows or any(row.get('amount',0)<=0 for row in rows):
            raise serializers.ValidationError('Add a positive charge or discount.')
        return rows

    def validate(self, attrs):
        reference=attrs.get('reference')
        contact=attrs.get('contact')
        if reference and reference.contact_id and (not contact or reference.contact_id!=contact.pk):
            raise serializers.ValidationError({'reference':'Choose a reference for this contact.'})
        return attrs
