from django.core.management.base import BaseCommand
from django.db import transaction
from accounts.models import User
from accounts.domain import encrypt,decrypt
from operations.models import Outbox
from operations.audit import record
class Command(BaseCommand):
    help='Reencrypt retained factor and expiring delivery secrets using the configured active key.'
    def add_arguments(self,parser):parser.add_argument('--operator',required=True);parser.add_argument('--reason',required=True)
    def handle(self,*args,**options):
        count=0
        for pk in User.objects.exclude(totp_secret='').values_list('pk',flat=True).iterator():
            with transaction.atomic():
                user=User.objects.select_for_update().get(pk=pk);user.totp_secret=encrypt(decrypt(user.totp_secret));user.save(update_fields=['totp_secret']);count+=1
        for pk in Outbox.objects.exclude(encrypted_body='').values_list('pk',flat=True).iterator():
            with transaction.atomic():
                out=Outbox.objects.select_for_update().get(pk=pk);out.encrypted_body=encrypt(decrypt(out.encrypted_body));out.save(update_fields=['encrypted_body']);count+=1
        record(options['operator'],None,'secrets.reencrypted',summary={'records':count},reason=options['reason']);self.stdout.write(f'Reencrypted {count} retained secret records without exposing their values.')
