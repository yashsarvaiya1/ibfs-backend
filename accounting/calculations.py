"""Decimal calculations used by document posting and PDF presentation."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_DOWN

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


def allocate_cents(total, weights):
    """Proportional allocation that conserves the exact cent-rounded total."""
    total = money(total)
    weights = [decimal_value(value) for value in weights]
    weight_sum = sum(weights, Decimal('0'))
    if not weight_sum:
        if total:
            raise ValueError('Cannot allocate a value without positive item amounts.')
        return [Decimal('0.00') for _ in weights]
    shares = [abs(total) * value / weight_sum for value in weights]
    cents = [share.quantize(CENT, rounding=ROUND_DOWN) for share in shares]
    missing = int((abs(total) - sum(cents, Decimal('0'))) / CENT)
    for index in sorted(range(len(weights)), key=lambda i: (-(shares[i] - cents[i]), i))[:missing]:
        cents[index] += CENT
    return [value * (-1 if total < 0 else 1) for value in cents]


def item_discount(item):
    gross = money(item.get('amount'))
    discount = money(item.get('discount'))
    if item.get('discount_percentage') is not None:
        percentage = decimal_value(item['discount_percentage'])
        if not 0 <= percentage <= 100:
            raise ValueError('Item discount percentage must be between 0 and 100.')
        discount = money(gross * percentage / 100)
    if discount < 0 or discount > gross:
        raise ValueError('Item discount must be between zero and the item amount.')
    return discount


def document_totals(data, document_type=None):
    items = data.get('line_items') or []
    item_discounts = [item_discount(item) for item in items]
    subtotal = sum(((decimal_value(i.get('amount')) - discount) *
                    (-1 if document_type == 'interest' and i.get('type') == 'discount' else 1)
                    for i, discount in zip(items, item_discounts)), Decimal('0'))
    charges = sum((decimal_value(c.get('amount')) for c in data.get('charges') or []), Decimal('0'))
    percentage = data.get('discount_percentage')
    discount = decimal_value(data.get('discount'))
    if percentage is not None:
        percentage = decimal_value(percentage)
        if not 0 <= percentage <= 100:
            raise ValueError('Discount percentage must be between 0 and 100.')
        discount = money(subtotal * percentage / 100)
    taxable = money(subtotal + charges - discount)
    taxes = []
    line_details = []
    if data.get('tax_mode') == 'item':
        if data.get('taxes'):
            raise ValueError('Per-item tax cannot also include document-wide taxes.')
        bases = [money(item.get('amount')) - discount for item, discount in zip(items, item_discounts)]
        if any(base < 0 for base in bases) or taxable < 0:
            raise ValueError('Per-item taxable values cannot be negative.')
        base_sum = sum(bases, Decimal('0'))
        adjustment = taxable - base_sum
        if not base_sum and adjustment:
            raise ValueError('Add positive item values before distributing charges or discount.')
        # Distribute net charges/discount in cents; largest remainders preserve the
        # exact document base. Signed adjustments use the same deterministic order.
        shares = [abs(adjustment) * base / base_sum if base_sum else Decimal('0') for base in bases]
        cents = [value.quantize(CENT, rounding=ROUND_DOWN) for value in shares]
        missing = int((abs(adjustment) - sum(cents, Decimal('0'))) / CENT)
        for index in sorted(range(len(bases)), key=lambda i: (-(shares[i] - cents[i]), i))[:missing]:
            cents[index] += CENT
        grouped = {}
        for index, (item, base) in enumerate(zip(items, bases)):
            line_base = base + cents[index] * (-1 if adjustment < 0 else 1)
            detail = {'taxable_amount': line_base, 'taxes': []}
            for tax in item.get('taxes') or []:
                percentage = decimal_value(tax.get('percentage'))
                if not 0 <= percentage <= 100:
                    raise ValueError('Tax percentages must be between 0 and 100.')
                name = str(tax.get('name') or '').strip()
                if not name:
                    raise ValueError('Each item tax needs a component name.')
                amount = money(line_base * percentage / 100)
                detail['taxes'].append({'name': name, 'percentage': percentage, 'amount': amount})
                key = (name, percentage)
                if key not in grouped:
                    grouped[key] = {'name': name, 'percentage': percentage, 'amount': Decimal('0')}
                grouped[key]['amount'] += amount
            line_details.append(detail)
        taxes = list(grouped.values())
    else:
        for tax in data.get('taxes') or []:
            percentage = decimal_value(tax.get('percentage'))
            taxes.append({'name': tax.get('name', 'Tax'), 'percentage': percentage,
                          'amount': money(taxable * percentage / 100)})
    tax_total = sum((t['amount'] for t in taxes), Decimal('0'))
    total = money(taxable + tax_total)
    if data.get('tax_mode') != 'item' and items and taxable >= 0 and all(decimal_value(item.get('amount')) >= 0 for item in items):
        weights = [money(item.get('amount')) - discount for item, discount in zip(items, item_discounts)]
        try:
            bases = allocate_cents(taxable, weights)
            parts = [allocate_cents(tax['amount'], weights) for tax in taxes]
            line_details = [{'taxable_amount': base, 'taxes': [dict(tax, amount=parts[t][i]) for t, tax in enumerate(taxes)]} for i, base in enumerate(bases)]
        except ValueError:
            # Preserve existing document calculations; HSN allocation is unavailable.
            line_details = []
    for detail, item, line_discount in zip(line_details, items, item_discounts):
        detail['discount'] = money(line_discount)
        detail['net_amount'] = money(item.get('amount')) - line_discount
    if document_type == 'interest':
        total = abs(total)
    return {'gross_subtotal': money(subtotal + sum(item_discounts, Decimal('0'))), 'item_discount_total': sum(item_discounts, Decimal('0')), 'subtotal': money(subtotal), 'charges_total': money(charges),
            'discount': money(discount), 'taxable_amount': taxable,
            'taxes': taxes, 'tax_total': tax_total, 'total': total, 'line_details': line_details}
