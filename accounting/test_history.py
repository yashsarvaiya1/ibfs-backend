from django.test import TestCase
from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework.test import APIClient
from rest_framework.exceptions import ValidationError
from .models import Document, DocumentRevision
from .document_updates import update_document
from .services import process_document_create, process_document_delete


class DocumentHistoryTests(TestCase):
    def test_revisions_are_transactional_and_skip_noop_saves(self):
        doc = process_document_create('invoice', {'line_items': [{'name': 'A', 'amount': 100}]})
        self.assertEqual(doc.revisions.count(), 1)
        doc.save(); self.assertEqual(doc.revisions.count(), 1)
        updated = update_document(doc, {'line_items': [{'name': 'A', 'amount': 120}]})
        self.assertEqual(updated.revisions.first().snapshot['total_amount'], '120.00')
        self.assertIn('line_items', updated.revisions.first().changed_fields)
        before = updated.revisions.count()
        try:
            with transaction.atomic():
                update_document(updated, {'notes': 'Rolled back'})
                raise RuntimeError()
        except RuntimeError: pass
        self.assertEqual(updated.revisions.count(), before)

    def test_legacy_first_edit_retains_known_baseline(self):
        doc = Document.objects.create(type='invoice', doc_id='LEGACY', date='2026-04-01', total_amount=100)
        doc.revisions.all().delete()  # Simulate a document that existed before revision tracking.
        updated = update_document(doc, {'notes': 'Correction'})
        self.assertEqual(updated.revisions.last().event, 'baseline')
        self.assertIsNone(updated.revisions.last().snapshot['notes'])
        self.assertEqual(updated.revisions.first().snapshot['notes'], 'Correction')

    def test_archived_history_is_readable_but_cannot_repost(self):
        client = APIClient(); client.force_authenticate(get_user_model().objects.create_user('history'))
        doc = process_document_create('invoice', {'line_items': [{'name': 'A', 'amount': 100}]})
        process_document_delete(doc, 'manual')
        self.assertEqual(client.get(f'/api/documents/{doc.pk}/').status_code, 200)
        response = client.get(f'/api/documents/{doc.pk}/history/')
        self.assertEqual(response.data['results'][0]['event'], 'archived')
        self.assertEqual(client.patch(f'/api/documents/{doc.pk}/?is_active=false', {'notes': 'unsafe'}, format='json').status_code, 404)
        with self.assertRaises(ValidationError): update_document(doc, {'notes': 'unsafe'})
        self.assertFalse(doc.transactions.exists())
