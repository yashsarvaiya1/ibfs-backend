"""Self-contained, network-independent presentation for IBFS documents."""
import base64
import mimetypes
from pathlib import Path
from decimal import Decimal
from django.conf import settings
from .calculations import decimal_value, document_totals, money
from .workflows import SIMPLE_LINE_TYPES
from .models import Document


def media_data_url(relative_path):
    if not relative_path:
        return None
    root = Path(settings.MEDIA_ROOT).resolve()
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root / 'uploads') or not path.is_file():
        return None
    mime = mimetypes.guess_type(path.name)[0]
    if mime not in {'image/png', 'image/jpeg', 'image/webp'} or path.stat().st_size > 20 * 1024 * 1024:
        return None
    return f'data:{mime};base64,{base64.b64encode(path.read_bytes()).decode("ascii")}'


def format_money(value):
    value = money(value)
    whole, fraction = f'{abs(value):.2f}'.split('.')
    if len(whole) > 3:
        tail, whole = whole[-3:], whole[:-3]
        groups = []
        while whole:
            groups.insert(0, whole[-2:])
            whole = whole[:-2]
        whole = ','.join(groups + [tail])
    return ('-' if value < 0 else '') + whole + '.' + fraction


def _integer_words(value):
    small = ('Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen '
             'Fourteen Fifteen Sixteen Seventeen Eighteen Nineteen').split()
    tens = ['', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy', 'Eighty', 'Ninety']
    if value < 20:
        return small[value]
    if value < 100:
        return tens[value // 10] + (' ' + small[value % 10] if value % 10 else '')
    for scale, label in ((10_000_000, 'Crore'), (100_000, 'Lakh'), (1000, 'Thousand'), (100, 'Hundred')):
        if value >= scale:
            return _integer_words(value // scale) + ' ' + label + (' ' + _integer_words(value % scale) if value % scale else '')


def amount_in_words(value):
    value = abs(money(value))
    rupees = int(value)
    paise = int((value - rupees) * 100)
    return 'Rupees ' + _integer_words(rupees) + (' and ' + _integer_words(paise) + ' Paise' if paise else '') + ' Only'


def document_context(document, app_settings, contact_display):
    data = {key: getattr(document, key) for key in ('line_items', 'charges', 'taxes', 'discount', 'discount_percentage', 'tax_mode')}
    items = []
    calculation_items = []
    for item in document.line_items or []:
        row = dict(item)
        amount = item.get('amount')
        if amount is None and item.get('rate') is not None and item.get('quantity') is not None:
            amount = decimal_value(item['rate']) * decimal_value(item['quantity'])
        calculation_items.append({**item, 'amount': amount})
        row['amount_display'] = format_money(amount) if amount is not None else ''
        row['rate_display'] = format_money(item['rate']) if item.get('rate') is not None else ''
        row['quantity_display'] = format(decimal_value(item['quantity']), 'f').rstrip('0').rstrip('.') if '.' in str(item.get('quantity', '')) else item.get('quantity', '')
        if item.get('supply_category'):
            row['supply_label'] = dict(Document._meta.get_field('supply_category').choices).get(item['supply_category'], str(item['supply_category']))
        row['is_discount'] = document.type == 'interest' and item.get('type') == 'discount'
        items.append(row)
    data['line_items'] = calculation_items
    totals = document_totals(data, document.type)
    if document.tax_mode == 'item':
        for row, detail in zip(items, totals['line_details']):
            row['item_tax_details'] = [{**tax, 'amount_display': format_money(tax['amount'])} for tax in detail['taxes']]
    has_line_totals = bool(calculation_items) and all(item['amount'] is not None for item in calculation_items)
    # Fast entry and historical explicit totals remain authoritative.
    total = money(document.total_amount) if document.total_amount is not None else totals['total']
    has_total = document.total_amount is not None or has_line_totals
    adjustment = money(total - totals['total']) if has_line_totals else Decimal('0')
    taxes = [{**tax, 'amount_display': format_money(tax['amount'])} for tax in totals['taxes']]
    charges = [{**charge, 'amount_display': format_money(charge.get('amount'))} for charge in document.charges or []]
    vendor = document.type in {'bill', 'po', 'dn'}
    return {
        'document': document, 'settings': app_settings,
        'header_image': media_data_url(app_settings.header_image),
        'sign_image': media_data_url(app_settings.sign_image),
        'contact': contact_display(document.contact), 'consignee': contact_display(document.consignee),
        'line_items': items, 'charges': charges, 'taxes': taxes,
        'subtotal': format_money(totals['subtotal']), 'taxable_amount': format_money(totals['taxable_amount']),
        'discount': format_money(totals['discount']), 'tax_total': format_money(totals['tax_total']),
        'grand_total': format_money(total), 'adjustment': format_money(adjustment),
        'has_adjustment': adjustment != 0, 'amount_in_words': amount_in_words(total),
        'has_total': has_total,
        'has_line_totals': has_line_totals,
        'doc_type_label': document.get_type_display(),
        'is_simple_line_type': document.type in SIMPLE_LINE_TYPES,
        'is_challan': document.type == 'challan', 'is_vendor_doc': vendor,
        'party_label': 'Supplier' if vendor else 'Bill to',
        'business_label': 'Buyer' if vendor else 'Issued by',
    }
