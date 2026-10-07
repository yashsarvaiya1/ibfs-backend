import io
import tempfile
from pathlib import Path
from datetime import date
from django.test import SimpleTestCase, TestCase, override_settings
from django.template.loader import render_to_string
from unittest.mock import patch
from rest_framework.test import APIClient
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from .models import Document
from .services import _build_document_context
from .printing import media_data_url, format_money
from shared.models import Settings
from upload.utils import process_upload


class DocumentPrintTests(SimpleTestCase):
    def test_fast_document_preserves_posted_amount(self):
        doc = Document(type='invoice', doc_id='INV-1', date=date.today(), total_amount=500)
        context = _build_document_context(doc, Settings())
        self.assertEqual(context['grand_total'], '500.00')
        self.assertEqual(context['amount_in_words'], 'Rupees Five Hundred Only')

    def test_interest_discount_and_amount_agree(self):
        doc = Document(type='interest', date=date.today(), line_items=[
            {'name': 'Charge', 'amount': 100, 'type': 'charge'},
            {'name': 'Discount', 'amount': 20, 'type': 'discount'}], total_amount=80)
        context = _build_document_context(doc, Settings())
        self.assertEqual(context['grand_total'], '80.00')
        self.assertFalse(context['has_adjustment'])
        self.assertTrue(context['line_items'][1]['is_discount'])

    def test_income_uses_source_and_simple_amount_columns(self):
        doc = Document(type='income', line_items=[{'name': 'Salary', 'amount': 200}], total_amount=200)
        context = _build_document_context(doc, Settings())
        self.assertEqual(context['party_label'], 'Income source')
        self.assertTrue(context['is_simple_line_type'])
        self.assertEqual(context['grand_total'], '200.00')
        self.assertFalse(context['has_adjustment'])

    def test_indian_grouping(self):
        self.assertEqual(format_money('1234567.89'), '12,34,567.89')

    def test_branding_preserves_transparency(self):
        buffer = io.BytesIO()
        Image.new('RGBA', (30, 20), (0, 0, 0, 0)).save(buffer, 'PNG')
        file = SimpleUploadedFile('sign.png', buffer.getvalue(), content_type='image/png')
        data, path = process_upload(file, 'settings')
        self.assertTrue(path.endswith('.png'))
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.getpixel((0, 0))[3], 0)

    def test_media_is_embedded_and_cannot_escape_uploads(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=root):
            folder = Path(root) / 'uploads' / 'settings'
            folder.mkdir(parents=True)
            Image.new('RGB', (10, 10), 'white').save(folder / 'header.png')
            self.assertTrue(media_data_url('uploads/settings/header.png').startswith('data:image/png;base64,'))
            self.assertIsNone(media_data_url('../outside.png'))
            self.assertIsNone(media_data_url('missing.png'))

    def test_malformed_upload_returns_validation_error(self):
        file = SimpleUploadedFile('bad.png', b'not an image', content_type='image/png')
        with self.assertRaises(ValueError):
            process_upload(file, 'settings')

    def test_empty_preview_does_not_invent_business_or_amounts(self):
        doc = Document(type='invoice', doc_id='', date=None, total_amount=None)
        profile = Settings()
        context = _build_document_context(doc, profile)
        html = render_to_string('accounting/document_print.html', {'documents': [context], 'print_settings': profile})
        self.assertFalse(context['has_total'])
        for invented in ('Sample', 'Contact not specified', 'Rupees ', 'Date:', 'Authorised signatory', '0.00'):
            self.assertNotIn(invented, html)

    def test_missing_line_amount_is_derived_only_when_inputs_exist(self):
        doc = Document(type='invoice', line_items=[{'name': 'Known', 'rate': 12, 'quantity': 2}, {'name': 'Unknown'}])
        context = _build_document_context(doc, Settings())
        self.assertEqual(context['line_items'][0]['amount_display'], '24.00')
        self.assertEqual(context['line_items'][1]['amount_display'], '')
        self.assertFalse(context['has_line_totals'])
        self.assertFalse(context['has_total'])
        doc.line_items = doc.line_items[:1]
        context = _build_document_context(doc, Settings())
        self.assertTrue(context['has_total'])
        self.assertEqual(context['grand_total'], '24.00')

    def test_prints_explicit_reverse_charge_no_without_defaulting_missing_values(self):
        profile = Settings()
        doc = Document(type='invoice', place_of_supply='Maharashtra (27)', reverse_charge=False)
        html = render_to_string('accounting/document_print.html', {'documents': [_build_document_context(doc, profile)], 'print_settings': profile})
        self.assertIn('Maharashtra (27)', html)
        self.assertIn('Reverse charge</span><strong>No</strong>', html)


class PrintPreviewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user('print-review'))

    @patch('accounting.services._render_playwright_pdf', return_value=b'%PDF-review')
    def test_preview_uses_latest_saved_document(self, render):
        Document.objects.create(type='invoice', doc_id='REAL-42', date=date(2026, 10, 6), total_amount=720)
        response = self.client.get('/api/settings/print-preview/?type=invoice')
        self.assertEqual(response.status_code, 200)
        html = render.call_args.args[0]
        self.assertIn('REAL-42', html)
        self.assertIn('720.00', html)
        self.assertNotIn('Sample', html)

    @patch('accounting.services._render_playwright_pdf', return_value=b'%PDF-review')
    def test_empty_preview_and_template_choice(self, render):
        response = self.client.patch('/api/settings/', {'print_template': 'classic'}, format='json')
        self.assertEqual(response.status_code, 200)
        response = self.client.get('/api/settings/print-preview/?type=bill')
        self.assertEqual(response.status_code, 200)
        html = render.call_args.args[0]
        self.assertIn('print-page classic', html)
        self.assertNotIn('Rupees ', html)
        self.assertNotIn('Sample', html)
        self.assertEqual(self.client.patch('/api/settings/', {'print_template': 'invalid'}, format='json').status_code, 400)
