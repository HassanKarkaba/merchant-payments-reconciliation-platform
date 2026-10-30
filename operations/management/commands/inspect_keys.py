import json
from django.core.management.base import BaseCommand
from operations.recovery import inventory
class Command(BaseCommand):
    def handle(self,*args,**options):self.stdout.write(json.dumps(inventory(),default=str))
