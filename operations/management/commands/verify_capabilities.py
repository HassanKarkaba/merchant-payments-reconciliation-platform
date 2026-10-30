import json
from django.core.management.base import BaseCommand,CommandError
from operations.health import capabilities
class Command(BaseCommand):
    def add_arguments(self,parser):parser.add_argument('--require-all',action='store_true')
    def handle(self,*args,**options):
        result=capabilities();self.stdout.write(json.dumps(result,sort_keys=True))
        if options['require_all'] and not all(result.values()):raise CommandError('One or more required capabilities are unavailable')
