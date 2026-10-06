"""Decimal calculations used by document posting and PDF presentation."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal('0.01')


def decimal_value(value, default='0'):
    try:
        result = Decimal(str(default if value in (None, '') else value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError('Enter a valid number.') from exc
    if not result.is_finite():
        raise ValueError('Enter a finite number.')
    return result


def money(value):
    return decimal_value(value).quantize(CENT, rounding=ROUND_HALF_UP)


def document_totals(data, document_type=None):
    items = data.get('line_items') or []
    subtotal = sum((decimal_value(i.get('amount')) *
                    (-1 if document_type == 'interest' and i.get('type') == 'discount' else 1)
                    for i in items), Decimal('0'))
    charges = sum((decimal_value(c.get('amount')) for c in data.get('charges') or []), Decimal('0'))
    discount = decimal_value(data.get('discount'))
    taxable = money(subtotal + charges - discount)
    taxes = []
    for tax in data.get('taxes') or []:
        percentage = decimal_value(tax.get('percentage'))
        taxes.append({'name': tax.get('name', 'Tax'), 'percentage': percentage,
                      'amount': money(taxable * percentage / 100)})
    tax_total = sum((t['amount'] for t in taxes), Decimal('0'))
    total = money(taxable + tax_total)
    if document_type == 'interest':
        total = abs(total)
    return {'subtotal': money(subtotal), 'charges_total': money(charges),
            'discount': money(discount), 'taxable_amount': taxable,
            'taxes': taxes, 'tax_total': tax_total, 'total': total}
