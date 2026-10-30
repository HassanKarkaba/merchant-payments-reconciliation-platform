from django.core.management.base import BaseCommand,CommandError
from django.db import transaction
from accounts.models import User
from operations.audit import record
class Command(BaseCommand):
    def add_arguments(self,parser):parser.add_argument('--email',required=True);parser.add_argument('--operator',required=True);parser.add_argument('--reason',required=True)
    def handle(self,*args,**options):
        with transaction.atomic():
            user=User.objects.select_for_update().get(email=options['email']);user.auth_epoch+=1;user.totp_secret='';user.totp_confirmed=False;user.recovery_hashes=[];user.save();record(options['operator'],None,'mfa.local_recovery',user.pk,reason=options['reason'])
        self.stdout.write('Sessions revoked; password and authenticator reenrollment required.')
