from django.core.management.base import BaseCommand,CommandError
from django.db import transaction
from operations.models import Operation,Job
from operations.domain import enqueue
from operations.audit import record
class Command(BaseCommand):
    help='Reconcile accepted keys without creating replacement economic identities.'
    def add_arguments(self,parser):
        parser.add_argument('--limit',type=int,default=50)
        parser.add_argument('--include-queued',action='store_true')
        parser.add_argument('--operator')
        parser.add_argument('--reason')
    def handle(self,*args,**options):
        if options['include_queued'] and (not options['operator'] or not options['reason']):raise CommandError('Resuming reviewed unsent work requires --operator and --reason')
        states=['DISPATCHING','UNKNOWN']+(['QUEUED'] if options['include_queued'] else [])
        count=0
        for op in Operation.objects.filter(status__in=states).order_by('created_at')[:min(50,max(1,options['limit']))]:
            with transaction.atomic():
                kind='financial_dispatch' if op.status=='QUEUED' else 'financial_resolve'
                enqueue(kind,op.merchant,{'operation_id':str(op.pk)})
                record(options['operator'],op.merchant,'recovery.operation_queued',op.pk,{'kind':kind},reason=options['reason'] or 'Bounded authoritative recovery of accepted responsibility')
                count+=1
        self.stdout.write(f'{count} accepted operations queued under their existing keys.')
