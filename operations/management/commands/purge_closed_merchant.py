import getpass
import json
import os
import uuid
from django.conf import settings
from django.core.exceptions import ValidationError,PermissionDenied
from django.core.management.base import BaseCommand,CommandError
from accounts.models import User
from accounts.domain import consume_totp
from merchants.models import Merchant
from operations.models import PurgeReceipt
from operations import retention
from operations.provider import ProviderError

class Command(BaseCommand):
    help='Propose, independently approve, retire, or resume a complete closed-merchant purge.'
    def add_arguments(self,parser):
        parser.add_argument('merchant_id',type=uuid.UUID)
        parser.add_argument('--phase',choices=['propose','approve','retire','execute','inspect'],required=True)
        parser.add_argument('--environment-id',type=uuid.UUID,required=True)
        parser.add_argument('--actor-email')
        parser.add_argument('--operator')
        parser.add_argument('--reason')
        parser.add_argument('--backup-root')
        parser.add_argument('--max-batches',type=int,default=100)
        parser.add_argument('--batch-size',type=int,default=500)
    def actor(self,email):
        if not email:raise CommandError('--actor-email is required for this decision.')
        user=User.objects.filter(email=email.strip().lower()).first()
        password=os.environ.get('MPRP_PURGE_ACTOR_PASSWORD') or getpass.getpass('Platform account password: ')
        code=os.environ.get('MPRP_PURGE_ACTOR_TOTP') or getpass.getpass('Fresh authenticator code: ')
        if not user or not user.is_active or not user.check_password(password) or not consume_totp(user,code):
            raise CommandError('Current password and a fresh authenticator code are required.')
        return user
    def handle(self,*args,**options):
        if str(options['environment_id'])!=str(settings.MPRP_ENVIRONMENT_ID):raise CommandError('Explicit environment identity does not match this database.')
        phase=options['phase'];merchant_id=options['merchant_id']
        if phase!='inspect' and not options['reason']:raise CommandError('--reason is required for every purge decision or transition.')
        if phase in ['retire','execute'] and not options['operator']:raise CommandError('--operator identifies the privileged local actor for retirement and purge execution.')
        try:
            if phase=='propose':
                if not options['reason'] or not options['backup_root']:raise CommandError('--reason and an explicitly mounted --backup-root are required.')
                actor=self.actor(options['actor_email'])
                merchant=Merchant.objects.get(pk=merchant_id)
                backup=retention.backup_inventory(options['backup_root'],settings.MPRP_ENVIRONMENT_ID)
                receipt=retention.propose_purge(actor,merchant,backup,options['reason'])
            else:
                receipt=PurgeReceipt.objects.get(merchant_id_safe=merchant_id,environment_id=settings.MPRP_ENVIRONMENT_ID)
                if phase=='approve':receipt=retention.approve_purge(self.actor(options['actor_email']),receipt,options['reason'])
                elif phase in ['retire','execute']:
                    if not options['reason'] or not options['backup_root']:raise CommandError('--reason and the configured mounted --backup-root are required before irreversible retirement or purge.')
                    backup=retention.backup_inventory(options['backup_root'],settings.MPRP_ENVIRONMENT_ID)
                    if not backup['empty']:raise CommandError('Retained backups still contain the tenant dataset.')
                    if receipt.status not in ['PURGING','COMPLETE']:receipt=retention.retire_provider_namespace(receipt,backup)
                    if phase=='execute' and receipt.status!='COMPLETE':
                        receipt=retention.refresh_provider_purge(receipt)
                        result=retention.execute_purge_batches(receipt,os.environ.get('MPRP_PURGE_DATABASE_URL'),options['batch_size'],options['max_batches'],operator=options['operator'],reason=options['reason'])
                        self.stdout.write(json.dumps(result,sort_keys=True,default=str));return
            self.stdout.write(json.dumps({'receipt_id':str(receipt.pk),'merchant_id':str(receipt.merchant_id_safe),'provider_account_id':str(receipt.provider_account_id),'status':receipt.status,'stage':receipt.stage,'inventory_hash':receipt.inventory_hash,'counts':receipt.dataset_counts,'deleted_counts':receipt.deleted_counts,'simulator':receipt.simulator_receipt},sort_keys=True,default=str))
        except (ValidationError,PermissionDenied,ProviderError) as exc:
            raise CommandError('; '.join(exc.messages) if isinstance(exc,ValidationError) else str(exc)) from None
        except (Merchant.DoesNotExist,PurgeReceipt.DoesNotExist):raise CommandError('No matching merchant or purge receipt exists in this environment.') from None
