from django.core.management.base import BaseCommand
from operations.recovery import hold
class Command(BaseCommand):
    def add_arguments(self,parser):parser.add_argument('--operator',required=True);parser.add_argument('--reason',required=True)
    def handle(self,*args,**options):hold(options['operator'],options['reason']);self.stdout.write('RESTORE_HOLD active')
