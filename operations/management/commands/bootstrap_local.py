from django.core.management.base import BaseCommand,CommandError
class Command(BaseCommand):
    def handle(self,*args,**kwargs):raise CommandError('Run scripts/bootstrap_local.py before loading Django; use sh scripts/local.sh init.')
