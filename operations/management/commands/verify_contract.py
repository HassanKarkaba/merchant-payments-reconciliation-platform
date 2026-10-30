from django.core.management import call_command
from django.core.management.base import BaseCommand
class Command(BaseCommand):
    def handle(self,*args,**options):
        call_command('check');call_command('makemigrations',check=True,dry_run=True);self.stdout.write('Source, model and migration checks complete; this does not assert all acceptance cases passed.')
