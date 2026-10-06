"""Read-only document GST, following IBFS posting rather than cash/stock timing.

Purchase GST is a book value, never a claim of eligible ITC. Portal/2B matching,
tax payments and filing-period adjustments are outside this document register.
"""
import re
import logging
from io import BytesIO
from itertools import islice
from tempfile import SpooledTemporaryFile
from datetime import date
from decimal import Decimal

from django.utils import timezone
from django.http import FileResponse
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination

from .calculations import document_totals, money
from .models import Document

logger = logging.getLogger(__name__)

COMPONENTS = ('cgst', 'sgst', 'igst', 'utgst', 'cess', 'unsplit_gst', 'other_tax')
BUCKETS = ('output', 'purchase', 'rcm_output', 'rcm_purchase', 'review')
MEASURES = ('taxable_amount', *COMPONENTS, 'gst_total')


class ReportPeriodSerializer(serializers.Serializer):
    fy = serializers.IntegerField(required=False, min_value=1900, max_value=9998)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)

    def validate(self, attrs):
        today = timezone.localdate()
        year = attrs.get('fy', today.year if today.month >= 4 else today.year - 1)
        start = attrs.get('date_from', date(year, 4, 1))
        end = attrs.get('date_to', date(year + 1, 3, 31))
        if start > end:
            raise serializers.ValidationError({'date_to': 'End date must be on or after start date.'})
        return {'date_from': start, 'date_to': end}


class CADocumentFilterSerializer(ReportPeriodSerializer):
    types = serializers.ListField(child=serializers.ChoiceField(choices=Document.TYPE_CHOICES), required=False)
    as_of = serializers.DateTimeField(required=False)

    def validate(self, attrs):
        return {**super().validate(attrs), 'types': attrs.get('types'),
                'as_of': attrs.get('as_of', timezone.now())}


class CAPackSerializer(CADocumentFilterSerializer):
    ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False)
    excluded_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False)
    expected_count = serializers.IntegerField(min_value=1, required=False)

    def validate(self, attrs):
        if 'ids' in attrs and 'excluded_ids' in attrs:
            raise serializers.ValidationError('Choose explicit documents or exclusions, not both.')
        return {**super().validate(attrs), **{key: attrs[key] for key in ('ids', 'excluded_ids', 'expected_count') if key in attrs}}


class CADocumentPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 100


def ca_documents(filters):
    query = (Document.objects.filter(is_active=True,
             date__range=(filters['date_from'], filters['date_to']), created_at__lte=filters['as_of'])
             .select_related('contact', 'consignee').order_by('date', 'pk'))
    if filters.get('types') is not None:
        query = query.filter(type__in=filters['types'])
    return query


def generate_ca_pack(documents, request):
    """Reuse document layouts in bounded batches, preserving each document's pages."""
    from pypdf import PdfWriter
    from .services import generate_bulk_documents_pdf
    writer = PdfWriter()
    output = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode='w+b')
    try:
        iterator = documents.iterator(chunk_size=100)
        while batch := list(islice(iterator, 100)):
            pdf, _ = generate_bulk_documents_pdf(batch, request)
            writer.append(BytesIO(pdf))
        writer.write(output)
        output.seek(0)
        return output
    except Exception:
        output.close()
        raise
    finally:
        writer.close()


def period_for(request):
    serializer = ReportPeriodSerializer(data=request.query_params)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data['date_from'], serializer.validated_data['date_to']


def tax_component(name):
    # Accept saved labels such as "CGST 9%", but never infer a split from GSTIN.
    label = str(name or '').strip().upper()
    if label in ('GST CESS', 'COMPENSATION CESS'):
        return 'cess'
    match = re.fullmatch(r'(CGST|SGST|IGST|UTGST|CESS|GST)(?:\s*[\d.]+\s*%?)?', label)
    if not match:
        return 'other_tax'
    return 'unsplit_gst' if match[1] == 'GST' else match[1].lower()


def blank_totals():
    return {key: Decimal('0.00') for key in MEASURES}


def amounts_as_text(amounts):
    return {key: str(money(value)) for key, value in amounts.items()}


def gst_document_row(doc):
    values = blank_totals()
    issues = []
    bucket = 'output' if doc.type in ('invoice', 'cn') else 'purchase'
    sign = -1 if doc.type in ('cn', 'dn') else 1
    excluded = False
    if doc.type == 'expense':
        issues.append('Expense tax needs CA classification; excluded from sales/purchase GST.')
        excluded = True
    if doc.type in ('cn', 'dn'):
        expected = 'invoice' if doc.type == 'cn' else 'bill'
        reference = doc.reference
        if not reference or not reference.is_active or reference.type != expected or reference.contact_id != doc.contact_id:
            issues.append(f'Link this note to an active {expected} for the same contact; tax excluded from normal totals.')
            excluded = True
        elif doc.reverse_charge != reference.reverse_charge:
            issues.append('Reverse-charge flag differs from the referenced document; tax needs review.')
            excluded = True
    if doc.reverse_charge is True:
        bucket = f'rcm_{bucket}'

    taxable = None
    tax_details = []
    if not doc.line_items:
        issues.append('Item details are missing; taxable value and GST cannot be calculated from the total alone.')
        excluded = True
    else:
        try:
            if any(not isinstance(item, dict) or item.get('amount') is None for item in doc.line_items):
                raise ValueError('Missing saved line amount')
            totals = document_totals({'line_items': doc.line_items, 'charges': doc.charges,
                                      'discount': doc.discount, 'taxes': doc.taxes, 'tax_mode': doc.tax_mode}, doc.type)
            taxable = totals['taxable_amount'] * sign
            values['taxable_amount'] = taxable
            if totals['taxable_amount'] < 0 or any(t['percentage'] < 0 or t['percentage'] > 100 for t in totals['taxes']):
                issues.append('Negative taxable value or invalid tax percentage; check the saved details.')
                excluded = True
            for tax in totals['taxes']:
                component = tax_component(tax['name'])
                amount = tax['amount'] * sign
                values[component] += amount
                tax_details.append({'name': tax['name'], 'percentage': str(tax['percentage']),
                                    'component': component, 'amount': str(amount)})
            values['gst_total'] = sum((values[key] for key in COMPONENTS if key != 'other_tax'), Decimal('0'))
            if doc.total_amount is None or money(doc.total_amount) != totals['total']:
                issues.append('Saved total differs from the item/tax calculation or is missing; tax needs review.')
                excluded = True
            if values['unsplit_gst']:
                issues.append('GST label has no component split; specify CGST/SGST or IGST before filing.')
            if values['other_tax']:
                issues.append('Unrecognised tax label; shown as other tax, excluded from GST.')
            if values['gst_total']:
                if doc.reverse_charge is None:
                    issues.append('Reverse charge is unspecified; book summary treats it as normal GST pending review.')
                if not doc.place_of_supply:
                    issues.append('Place of supply is missing; verify tax components before filing.')
                if bucket.endswith('purchase') and not (doc.contact and doc.contact.gstin):
                    issues.append('Supplier GSTIN is missing; purchase GST does not establish ITC eligibility.')
                if values['igst'] and (values['cgst'] or values['sgst'] or values['utgst']):
                    issues.append('IGST and local GST components occur together; check tax labels.')
                    excluded = True
                if values['sgst'] and values['utgst']:
                    issues.append('Both SGST and UTGST occur together; check tax labels.')
                    excluded = True
                if values['cgst'] != values['sgst'] + values['utgst']:
                    issues.append('CGST and state/territory GST amounts differ; verify the saved tax split.')
        except (ValueError, TypeError, AttributeError, ArithmeticError):
            issues.append('Saved item/tax data cannot be calculated; review the document.')
            values = blank_totals()
            taxable = None
            tax_details = []
            excluded = True
    return {
        'id': doc.pk, 'doc_id': doc.doc_id, 'type': doc.type, 'date': doc.date.isoformat(),
        'contact': str(doc.contact) if doc.contact else '',
        'gstin': doc.contact.gstin if doc.contact else None,
        'tax_mode': doc.tax_mode, 'supply_category': doc.supply_category,
        'supplier_invoice_number': doc.supplier_invoice_number,
        'reference': doc.reference.doc_id if doc.reference else None,
        'place_of_supply': doc.place_of_supply, 'reverse_charge': doc.reverse_charge,
        'total_amount': str(doc.total_amount) if doc.total_amount is not None else None,
        'bucket': 'review' if excluded else bucket, 'normal_bucket': bucket,
        'adjustment_sign': sign, 'taxable_amount': str(taxable) if taxable is not None else None,
        'amounts': amounts_as_text(values), 'tax_details': tax_details, 'issues': issues,
    }


def gst_documents(start, end):
    return (Document.objects.filter(is_active=True, date__range=(start, end))
            .filter(type__in=('invoice', 'bill', 'cn', 'dn', 'expense'))
            .exclude(type='expense', taxes=[])
            .select_related('contact', 'reference').order_by('date', 'pk'))


def gst_report(start, end, *, page=1, page_size=50, review_only=False):
    totals = {bucket: blank_totals() for bucket in BUCKETS}
    monthly = {}
    rows = []
    count = issue_count = matched_count = 0
    for doc in gst_documents(start, end).iterator(chunk_size=1000):
        row = gst_document_row(doc)
        count += 1
        issue_count += bool(row['issues'])
        month = row['date'][:7]
        if month not in monthly:
            monthly[month] = {bucket: blank_totals() for bucket in BUCKETS}
        for key in MEASURES:
            value = Decimal(row['amounts'][key])
            totals[row['bucket']][key] += value
            monthly[month][row['bucket']][key] += value
        if not review_only or row['issues']:
            matched_count += 1
            if (page - 1) * page_size < matched_count <= page * page_size:
                rows.append(row)
    return {'date_from': start.isoformat(), 'date_to': end.isoformat(),
            'basis': 'Active document dates; CN reduces sales and DN reduces purchases in IBFS. Payments and challans do not post GST again.',
            'count': matched_count, 'document_count': count, 'review_count': issue_count, 'page': page, 'page_size': page_size,
            'totals': {bucket: amounts_as_text(value) for bucket, value in totals.items()},
            'difference': amounts_as_text({key: totals['output'][key] - totals['purchase'][key] for key in MEASURES}),
            'months': [{'month': month, 'totals': {bucket: amounts_as_text(value) for bucket, value in buckets.items()}}
                       for month, buckets in sorted(monthly.items())], 'results': rows}


class ReportViewSet(viewsets.ViewSet):
    @action(detail=False, methods=['get'])
    def ca_documents(self, request):
        data = request.query_params.dict()
        if 'types' in data:
            data['types'] = [value for value in data['types'].split(',') if value]
        serializer = CADocumentFilterSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        filters = serializer.validated_data
        pagination = CADocumentPagination()
        page = pagination.paginate_queryset(ca_documents(filters), request, view=self)
        response = pagination.get_paginated_response([
            {'id': doc.pk, 'doc_id': doc.doc_id, 'type': doc.type, 'date': doc.date.isoformat(),
             'contact_name': str(doc.contact) if doc.contact else None,
             'total_amount': str(doc.total_amount) if doc.total_amount is not None else None}
            for doc in page])
        response.data['as_of'] = filters['as_of'].isoformat()
        return response

    @action(detail=False, methods=['post'])
    def ca_export(self, request):
        serializer = CAPackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        filters = serializer.validated_data
        docs = ca_documents(filters)
        if 'ids' in filters:
            # An explicit empty selection must never export every document.
            docs = docs.filter(pk__in=filters['ids'])
        else:
            docs = docs.exclude(pk__in=filters.get('excluded_ids', []))
        count = docs.count()
        if not count:
            return Response({'error': 'No documents selected for this period.'}, status=400)
        expected = filters.get('expected_count', len(set(filters['ids'])) if 'ids' in filters else count)
        if expected != count or ('as_of' in request.data and docs.filter(updated_at__gt=filters['as_of']).exists()):
            return Response({'error': 'Documents changed since selection. Refresh the list and review the selection again.'}, status=409)
        try:
            file = generate_ca_pack(docs, request)
        except Exception:
            logger.exception('CA document PDF generation failed')
            return Response({'error': 'Could not generate the CA PDF. Try a smaller date range or retry.'}, status=500)
        response = FileResponse(file, as_attachment=True, content_type='application/pdf',
            filename=f"CA_Documents_{filters['date_from']}_{filters['date_to']}.pdf")
        response['X-Document-Count'] = str(count)
        return response

    @action(detail=False, methods=['get'])
    def gst(self, request):
        start, end = period_for(request)
        page_field = serializers.IntegerField(min_value=1)
        size_field = serializers.IntegerField(min_value=1, max_value=100)
        page = page_field.run_validation(request.query_params.get('page', 1))
        size = size_field.run_validation(request.query_params.get('page_size', 50))
        review_only = serializers.BooleanField().run_validation(request.query_params.get('review_only', False))
        return Response(gst_report(start, end, page=page, page_size=size, review_only=review_only))

    @action(detail=False, methods=['get'])
    def gst_export(self, request):
        from .report_exports import csv_response, gst_csv_rows, report_pdf
        start, end = period_for(request)
        format = serializers.ChoiceField(choices=['csv', 'pdf']).run_validation(request.query_params.get('export_format', 'csv'))
        review = serializers.BooleanField().run_validation(request.query_params.get('review_only', False))
        if format == 'pdf':
            return report_pdf(start, end, review_only=review)
        return csv_response(gst_csv_rows(start, end, review), f'GST_Book_Register_{start}_{end}.csv')

    @action(detail=False, methods=['get'])
    def hsn(self, request):
        from .report_exports import hsn_report
        return Response(hsn_report(*period_for(request)))

    @action(detail=False, methods=['get'])
    def hsn_export(self, request):
        from .report_exports import csv_response, hsn_csv_rows, hsn_report, report_pdf
        start, end = period_for(request)
        format = serializers.ChoiceField(choices=['csv', 'pdf']).run_validation(request.query_params.get('export_format', 'csv'))
        if format == 'pdf': return report_pdf(start, end, kind='hsn')
        return csv_response(hsn_csv_rows(hsn_report(start, end)), f'HSN_Book_Summary_{start}_{end}.csv')

    @action(detail=False, methods=['get'])
    def allocation_review(self, request):
        from .report_exports import allocation_review
        start, end = period_for(request)
        page = serializers.IntegerField(min_value=1).run_validation(request.query_params.get('page', 1))
        size = serializers.IntegerField(min_value=1, max_value=100).run_validation(request.query_params.get('page_size', 50))
        return Response(allocation_review(start, end, page, size))

    @action(detail=False, methods=['get'])
    def comparison_template(self, request):
        from .comparisons import BANK_COLUMNS, PURCHASE_COLUMNS
        from .report_exports import csv_response
        kind = serializers.ChoiceField(choices=['bank', 'purchase']).run_validation(request.query_params.get('kind', 'bank'))
        return csv_response([BANK_COLUMNS if kind == 'bank' else PURCHASE_COLUMNS], f'{kind}_comparison_template.csv')

    @action(detail=False, methods=['post'])
    def compare_csv(self, request):
        from .comparisons import BANK_COLUMNS, PURCHASE_COLUMNS, bank_comparison, purchase_comparison, read_csv
        from shared.models import PaymentAccount
        kind = serializers.ChoiceField(choices=['bank', 'purchase']).run_validation(request.data.get('kind'))
        serializer = ReportPeriodSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        start, end = serializer.validated_data['date_from'], serializer.validated_data['date_to']
        rows = read_csv(request.FILES.get('file'), BANK_COLUMNS if kind == 'bank' else PURCHASE_COLUMNS)
        if kind == 'bank':
            account = serializers.PrimaryKeyRelatedField(queryset=PaymentAccount.objects.filter(is_active=True)).run_validation(request.data.get('account'))
            return Response(bank_comparison(rows, account, start, end))
        return Response(purchase_comparison(rows, start, end))
