import io
import tempfile
from pathlib import Path
from datetime import date
from django.test import SimpleTestCase, override_settings
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
