from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Create configured shared database cache tables when using Django DatabaseCache.'

    def handle(self, *args, **options):
        if any(value['BACKEND'] == 'django.core.cache.backends.db.DatabaseCache' for value in settings.CACHES.values()):
            call_command('createcachetable', verbosity=0)
