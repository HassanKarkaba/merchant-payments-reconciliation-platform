import json
import uuid
from django.core.management.base import BaseCommand,CommandError
from django.db import connection,transaction


class Command(BaseCommand):
    help='Resume bounded deletion of one eligible retired simulator namespace using the dedicated local purge role'

    def add_arguments(self,parser):
        parser.add_argument('--provider-account-id',required=True)
        parser.add_argument('--max-batches',type=int,default=20)

    def handle(self,*args,**options):
        try:
            account=uuid.UUID(options['provider_account_id'])
        except ValueError:
            raise CommandError('Canonical provider account UUID required') from None
        if not 1<=options['max_batches']<=1000:raise CommandError('Batch bound must be1 through1000')
        with connection.cursor() as cursor:
            cursor.execute('SELECT session_user')
            if cursor.fetchone()[0]!='mprp_purge':raise CommandError('Connect explicitly as the separate local mprp_purge role')
        for _ in range(options['max_batches']):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute('SELECT public.sim_purge_retired_namespace(%s)',[account])
                    result=cursor.fetchone()[0]
                    if isinstance(result,str):result=json.loads(result)
            if result.get('purge_complete'):break
        self.stdout.write(json.dumps(result,sort_keys=True))
