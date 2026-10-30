import signal
import threading
from django.core.management.base import BaseCommand, CommandError
from processor_sim.worker import run_scheduler


class Command(BaseCommand):
    help = 'Run the independent synthetic provider scheduler and callback outbox'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--interval', type=float, default=0.5)

    def handle(self, *args, **options):
        if not 0.1 <= options['interval'] <= 10:
            raise CommandError('Interval must be from 0.1 through 10 seconds')
        stopping = threading.Event()
        def stop(signum, frame):
            stopping.set()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        run_scheduler(stopping, options['interval'], options['once'])
