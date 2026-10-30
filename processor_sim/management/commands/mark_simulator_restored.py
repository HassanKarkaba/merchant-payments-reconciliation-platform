import json
from django.core.management.base import BaseCommand,CommandError
from processor_sim.domain import mark_restored


class Command(BaseCommand):
    help='Fence old dispatches and mark the independent simulator as restored for mandatory inventory review'
    def add_arguments(self,parser):
        parser.add_argument('--operator',required=True)
        parser.add_argument('--reason',required=True)
    def handle(self,*args,**options):
        try:result=mark_restored(options['operator'],options['reason'])
        except Exception as error:raise CommandError(str(error)) from None
        self.stdout.write(json.dumps(result,sort_keys=True))
