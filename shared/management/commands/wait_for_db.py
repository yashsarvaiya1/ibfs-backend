import time
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.utils import OperationalError, InterfaceError


class Command(BaseCommand):
    help='Wait for the configured database before startup.'

    def add_arguments(self,parser):
        parser.add_argument('--timeout',type=int,default=60)

    def handle(self,*args,**options):
        deadline=time.monotonic()+options['timeout']
        while True:
            try:
                connection.ensure_connection()
                self.stdout.write('Database ready.')
                return
            except (OperationalError,InterfaceError):
                connection.close()
                if time.monotonic()>=deadline:
                    raise CommandError('Database did not become ready within the startup timeout.')
                time.sleep(2)
