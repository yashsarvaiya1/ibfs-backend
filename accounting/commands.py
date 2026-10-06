"""Validated inputs for the existing quick-entry document workflow."""
from decimal import Decimal
from rest_framework import serializers
from rest_framework.exceptions import APIException
from shared.models import PaymentAccount
from inventory.models import Product
from .models import Document
from .calculations import decimal_value, money


class EditConflict(APIException):
    status_code = 409
    default_detail = 'This document changed while you were editing. Reload it before saving.'


class DocumentWriteSerializer(serializers.ModelSerializer):
    payment_account = serializers.PrimaryKeyRelatedField(queryset=PaymentAccount.objects.filter(is_active=True), required=False, allow_null=True)
    expected_updated_at = serializers.DateTimeField(required=False, write_only=True)

    class Meta:
        model = Document
        fields = ['type', 'doc_id', 'contact', 'consignee', 'reference', 'line_items',
            'total_amount', 'charges', 'taxes', 'discount', 'date', 'due_date',
            'payment_terms', 'attachment_urls', 'notes', 'payment_account', 'expected_updated_at']
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
                for key in ('quantity', 'rate', 'amount'):
                    if row.get(key) is not None:
                        number = decimal_value(row[key])
                        if number < 0 or number > Decimal('9999999999999.99'):
                            raise ValueError('Line values must be between 0 and 9,999,999,999,999.99.')
                        row[key] = float(money(number))
                if row.get('amount') is None and row.get('quantity') is not None and row.get('rate') is not None:
                    row['amount'] = float(money(decimal_value(row['quantity']) * decimal_value(row['rate'])))
                if row.get('product_id'):
                    row['product_id'] = int(row['product_id'])
                    product_ids.add(row['product_id'])
            except (ValueError, TypeError) as exc:
                raise serializers.ValidationError(str(exc)) from exc
            if row.get('type') not in (None, 'charge', 'discount'):
                raise serializers.ValidationError('Choose charge or discount for interest lines.')
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
