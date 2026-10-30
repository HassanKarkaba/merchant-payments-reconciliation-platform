from getpass import getpass
from django.core.management.base import BaseCommand, CommandError
from operations.recovery import authenticate_staff, reconcile_inventory


class Command(BaseCommand):
    help='Reconcile the complete immutable provider-key inventory before separate release approval'

    def add_arguments(self,parser):
        parser.add_argument('--email',required=True)
        parser.add_argument('--reason',required=True)

    def handle(self,*args,**options):
        try:
            actor=authenticate_staff(options['email'],getpass('Maker password: '),getpass('Fresh authenticator code: '),{'OPERATIONS','RECONCILIATION'})
            inventory=reconcile_inventory(actor,options['reason'])
        except Exception as error:
            raise CommandError(str(error)) from None
        self.stdout.write(f'Inventory {inventory.pk}: {inventory.status}; unresolved={len(inventory.findings)}. Financial hold remains until separate authenticated finance approval.')
