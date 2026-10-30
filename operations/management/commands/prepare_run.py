import time
from django.core.management.base import BaseCommand,CommandError
from operations.recovery import prepare_run
from operations.provider import ProviderError
class Command(BaseCommand):
    help='Prepare one shared full-run epoch; wait finitely for the independent simulator.'
    def add_arguments(self,parser):parser.add_argument('--new-run',action='store_true')
    def handle(self,*args,**options):
        deadline=time.monotonic()+115;new_run=options['new_run']
        while True:
            try:
                control=prepare_run(new_run,deadline=deadline)
                self.stdout.write(f'Prepared epoch {control.dispatch_epoch}; recovery hold={control.restore_hold}')
                return
            except ProviderError:
                # The selected run token was persisted before HTTP; never invent
                # another token when an acknowledgment is lost.
                new_run=False
                if time.monotonic()>=deadline:raise CommandError('Simulator pairing unavailable within 120 seconds; accepted state is retained') from None
                time.sleep(1)
