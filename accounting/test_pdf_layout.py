"""Exercise the actual Chromium/PDF boundary, including continuation placeholders."""
import io
from datetime import date
from django.test import SimpleTestCase
from django.template.loader import render_to_string
from pypdf import PdfReader
from shared.models import Settings
from .models import Document
from .services import _build_document_context, _render_playwright_pdf


class PDFPaginationTests(SimpleTestCase):
    def render(self, template, count):
        profile = Settings(company_name='Saved business', print_template=template, signatory_name='Saved signatory')
        document = Document(type='bill', doc_id='BILL-LAYOUT-42', date=date(2026, 10, 6),
            line_items=[{'name': f'Item {i + 1:03d}', 'description': 'Saved item description',
                         'quantity': 2, 'rate': 150, 'amount': 300} for i in range(count)],
            taxes=[{'name': 'CGST', 'percentage': 9}, {'name': 'SGST', 'percentage': 9}],
            total_amount=count * 354)
        html = render_to_string('accounting/document_print.html', {
            'documents': [_build_document_context(document, profile)], 'print_settings': profile})
        return [page.extract_text() for page in PdfReader(io.BytesIO(_render_playwright_pdf(html))).pages]

    def test_continuation_amount_and_words_placeholders_are_blank_until_final_items(self):
        for template in ('classic', 'modern'):
            with self.subTest(template=template):
                pages = self.render(template, 32)
                self.assertGreater(len(pages), 1)
                for text in pages:
                    self.assertIn('amount in words', text.lower())
                    self.assertIn('Total INR', text)
                    self.assertIn('Subtotal', text)
                    self.assertIn('BILL-LAYOUT-42', text)
                    self.assertIn('Saved signatory', text)
                for text in pages[:-1]:
                    for value in ('11,328.00', '9,600.00', '864.00', 'Rupees '):
                        self.assertNotIn(value, text)
                self.assertIn('Item 032', pages[-1])
                self.assertIn('11,328.00', pages[-1])
                self.assertIn('Rupees ', pages[-1])
                self.assertEqual('\n'.join(pages).count('11,328.00'), 1)
                for i in range(32):
                    self.assertEqual('\n'.join(pages).count(f'Item {i + 1:03d}'), 1)

    def test_short_bill_keeps_items_words_and_total_on_one_page(self):
        pages = self.render('modern', 3)
        self.assertEqual(len(pages), 1)
        self.assertIn('Item 003', pages[0])
        self.assertIn('1,062.00', pages[0])
        self.assertIn('Rupees ', pages[0])
