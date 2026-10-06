from django.core.management.base import BaseCommand
from accounting.cron import cleanup_temp_pdfs
from upload.cron import cleanup_orphaned_uploads


class Command(BaseCommand):
    help='Clean expired temporary PDFs and old unreferenced uploads. Run from a host scheduler.'

    def handle(self,*args,**options):
        cleanup_temp_pdfs()
        cleanup_orphaned_uploads()
        self.stdout.write('Maintenance completed.')
