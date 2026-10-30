import getpass
import uuid
from django.core.management.base import BaseCommand,CommandError
from accounts.models import User
from accounts.domain import normalize_email
from django.contrib.auth.password_validation import validate_password
from operations.audit import record
class Command(BaseCommand):
    def add_arguments(self,parser):
        parser.add_argument('--email',required=True);parser.add_argument('--role',choices=['OPERATIONS','RECONCILIATION','FINANCE_APPROVER','AUDITOR'],required=True);parser.add_argument('--operator',required=True);parser.add_argument('--reason',required=True)
    def handle(self,*args,**options):
        email=normalize_email(options['email'])
        if User.objects.filter(email=email).exists():raise CommandError('Account exists; refusing to replace authority')
        password=getpass.getpass('New password: ');validate_password(password)
        from django.db import transaction
        with transaction.atomic():
            user=User(email=email,username=str(uuid.uuid4()),verified=True,platform_role=options['role']);user.set_password(password);user.save();record(options['operator'],None,'staff.bootstrapped',user.pk,{'role':options['role']},reason=options['reason'])
        self.stdout.write('Staff account created. TOTP enrollment is required at first login.')
