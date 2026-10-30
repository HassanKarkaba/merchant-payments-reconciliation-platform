from getpass import getpass
from django.core.management.base import BaseCommand, CommandError
from operations.recovery import authenticate_staff, approve_release


class Command(BaseCommand):
    help='Approve one reconciled recovery inventory using a distinct current finance identity'

    def add_arguments(self,parser):
        parser.add_argument('--inventory-id',required=True)
        parser.add_argument('--email',required=True)

    def handle(self,*args,**options):
        try:
            checker=authenticate_staff(options['email'],getpass('Finance approver password: '),getpass('Fresh authenticator code: '),{'FINANCE_APPROVER'})
            approve_release(options['inventory_id'],checker)
        except Exception as error:
            raise CommandError(str(error)) from None
        self.stdout.write('Recovery hold released for the reviewed inventory. Worker readiness must refresh before money admission.')
