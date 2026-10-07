"""IBFS's simple document workflow, shared by creation, edits and printing.

Records represent obligations; actuals represent cash or physical movement.
Enabling challans moves inventory responsibility away from bills/invoices.
Orders and quotations remain non-posting reference documents.
"""
from decimal import Decimal

FINANCIAL_SIGNS = {
    'bill': Decimal('1'), 'invoice': Decimal('-1'),
    'cn': Decimal('1'), 'dn': Decimal('-1'),
}
STOCK_SIGNS = dict(FINANCIAL_SIGNS)
NON_POSTING_TYPES = {'po', 'pi', 'quotation'}
CASH_ONLY_TYPES = {'expense', 'income'}
SIMPLE_LINE_TYPES = {'interest', *CASH_ONLY_TYPES, 'cash_payment_voucher', 'cash_receipt_voucher'}
OUTGOING_TYPES = {'bill', 'cn', 'cash_payment_voucher'}


def stock_direction(document_type, reference_type=None):
    """Unknown/non-stock documents never silently default to stock IN."""
    return STOCK_SIGNS.get(reference_type if document_type == 'challan' else document_type)


def stock_mode(document_type, settings, reference_type=None):
    if stock_direction(document_type, reference_type) is None:
        return 'none'
    if document_type != 'challan' and settings.enable_challan:
        return 'none'
    return 'actual' if settings.auto_stock else 'record'
