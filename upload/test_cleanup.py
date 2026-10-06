import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from django.test import TestCase,override_settings
from accounting.models import Document
from .cron import cleanup_orphaned_uploads


class CleanupTests(TestCase):
    def test_reference_read_failure_does_not_delete_files(self):
        with tempfile.TemporaryDirectory() as root,override_settings(MEDIA_ROOT=root):
            file=Path(root,'uploads/documents/old.txt');file.parent.mkdir(parents=True);file.write_text('old')
            os.utime(file,(time.time()-10*86400,)*2)
            with patch('upload.cron._get_all_referenced_paths',side_effect=RuntimeError('database unavailable')):
                with self.assertLogs('upload.cron',level='ERROR'):cleanup_orphaned_uploads()
            self.assertTrue(file.exists())

    def test_archived_documents_keep_attachments(self):
        with tempfile.TemporaryDirectory() as root,override_settings(MEDIA_ROOT=root):
            file=Path(root,'uploads/documents/saved.txt');file.parent.mkdir(parents=True);file.write_text('saved')
            os.utime(file,(time.time()-10*86400,)*2)
            Document.objects.create(type='bill',doc_id='ARCHIVED',date='2026-01-01',is_active=False,attachment_urls=['uploads/documents/saved.txt'])
            cleanup_orphaned_uploads()
            self.assertTrue(file.exists())
