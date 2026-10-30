import os
import secrets
import uuid
import pyotp
from pathlib import Path
from django.core.management.base import BaseCommand,CommandError
from django.db import transaction
from django.conf import settings
from accounts.models import User
from accounts.domain import encrypt
from merchants.models import Merchant,Membership
from operations.audit import record
from operations.recovery import prepare_run
from operations import provider
class Command(BaseCommand):
    help='Idempotently seed fictional users; generated credentials go only to a private local file.'
    def add_arguments(self,parser):parser.add_argument('--output',required=True)
    def handle(self,*args,**options):
        output=Path(options['output'])
        if output.exists():
            self.stdout.write('Access file exists; no credentials or records overwritten.');return
        prepare_run()
        identities=[('owner','OWNER',''),('finance','FINANCE',''),('analyst','ANALYST',''),('operations',None,'OPERATIONS'),('reconciler',None,'RECONCILIATION'),('approver',None,'FINANCE_APPROVER'),('auditor',None,'AUDITOR')]
        lines=['MPRP SANDBOX — fictional generated access only','Sign in at '+settings.PUBLIC_ORIGIN+'. Use the authenticator secret in a TOTP app.','Passwords and authenticator seeds must remain in this ignored owner-readable file.','']
        with transaction.atomic():
            merchant,_=Merchant.objects.get_or_create(id=uuid.UUID('00000000-0000-4000-8000-000000000001'),defaults={'name':'Juniper Goods','status':'ACTIVE'})
            merchant.bank_account_id='bank_demo_'+merchant.provider_account_id.hex;merchant.save(update_fields=['bank_account_id'])
            for name,role,platform in identities:
                email=name+'@example.test'
                user=User.objects.filter(email=email).first()
                if user:raise CommandError('Existing users require their retained access file; refusing to replace credentials')
                password=secrets.token_urlsafe(24);secret=pyotp.random_base32()
                user=User(email=email,username=str(uuid.uuid4()),verified=True,platform_role=platform,totp_secret=encrypt(secret),totp_confirmed=True)
                user.set_password(password);user.save()
                if role:Membership.objects.create(user=user,merchant=merchant,role=role)
                lines.extend([email+' | '+(role or platform),'Password: '+password,'Authenticator secret: '+secret,''])
                record(user,merchant if role else None,'demo.user_created',user.pk)
            record(None,merchant,'demo.merchant_created',merchant.pk)
        provider.request('POST','/v1/accounts/',provider.envelope(merchant,'account-'+merchant.pk.hex,{'merchant_id':str(merchant.pk)}))
        output.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as file:file.write('\n'.join(lines))
        self.stdout.write('Created fictional merchant and role accounts. Access written to private local file.')
