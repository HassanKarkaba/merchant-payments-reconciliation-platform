import json
import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand,CommandError
from operations.retention import cleanup_transient
from operations.audit import record
class Command(BaseCommand):
    help='Expire transient bytes and mark eligible closed merchants; never delete financial history.'
    def add_arguments(self,parser):
        parser.add_argument('--environment-id',type=uuid.UUID,required=True)
        parser.add_argument('--operator',required=True)
        parser.add_argument('--reason',required=True)
        parser.add_argument('--backup-root')
    def handle(self,*args,**options):
        if str(options['environment_id'])!=str(settings.MPRP_ENVIRONMENT_ID):raise CommandError('Explicit environment identity does not match.')
        try:counts=cleanup_transient(options['backup_root'])
        except ValidationError as exc:raise CommandError('; '.join(exc.messages)) from None
        record(options['operator'],None,'retention.cleanup',summary=counts,reason=options['reason'][:2000])
        self.stdout.write(json.dumps(counts,sort_keys=True,default=str))
