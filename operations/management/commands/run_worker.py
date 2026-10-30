"""A bounded local pool with a dedicated recovery lane and durable leases."""
import signal
import threading
import time
import uuid
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, DatabaseError, InterfaceError
from operations.worker import tick, poll_events, schedule_maintenance


def _read_control():
    from operations.models import Control
    return Control.objects.filter(pk=1).first()


class Command(BaseCommand):
    help = 'Run at most four durable tasks, reserving one lane for recovery, callbacks, and dispute duties.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--max-jobs', type=int, default=0)

    def handle(self, *args, **options):
        if options['max_jobs'] < 0:
            raise CommandError('--max-jobs must be zero or positive.')
        from operations.run_identity import bind_process_identity,RunIdentityError
        try:bind_process_identity()
        except RunIdentityError as error:raise CommandError(str(error)) from None
        stopping = threading.Event()
        def stop(*unused):
            stopping.set()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

        if options['once']:
            schedule_maintenance()
            tick(uuid.uuid4())
            return

        count_lock = threading.Lock()
        state = {'completed': 0, 'active_tickets': 0, 'fatal': False}
        max_jobs = options['max_jobs']

        def loop(lane):
            owner = uuid.uuid4()
            last_maintenance = 0.0
            try:
                while not stopping.is_set():
                    from operations.run_identity import is_current
                    try:
                        control = _read_control()
                    except (DatabaseError, InterfaceError) as exc:
                        from operations.diagnostics import emit
                        emit('worker_cohort_probe_failed', error_class=type(exc).__name__)
                        connections.close_all()
                        stopping.wait(.5)
                        continue
                    if not is_current(control,bind_if_unbound=False):
                        stopping.set();break
                    if lane == 'recovery' and time.monotonic() - last_maintenance >= 5:
                        try:
                            poll_events()
                            schedule_maintenance()
                            # Interrupted authorization completion retains this
                            # durable eligibility flag and deterministic key.
                            from payments.models import Payment
                            from payments.domain import ensure_automatic_capture, automatic_capture_candidates
                            for payment in automatic_capture_candidates():
                                ensure_automatic_capture(payment)
                        except Exception as exc:
                            from operations.diagnostics import emit
                            emit('worker_maintenance_failed', error_class=type(exc).__name__)
                            # Every accepted duty remains in PostgreSQL; the
                            # next bounded maintenance pass inspects it again.
                            connections.close_all()
                        last_maintenance = time.monotonic()
                    with count_lock:
                        if max_jobs and state['completed'] >= max_jobs:
                            stopping.set()
                            break
                        if max_jobs and state['completed'] + state['active_tickets'] >= max_jobs:
                            ticket = False
                        else:
                            state['active_tickets'] += 1
                            ticket = True
                    if not ticket:
                        stopping.wait(.1)
                        continue
                    worked = False
                    try:
                        worked = tick(owner, lane=lane)
                    except Exception as exc:
                        from operations.diagnostics import emit
                        emit('worker_iteration_failed', error_class=type(exc).__name__)
                        connections.close_all()
                    finally:
                        with count_lock:
                            state['active_tickets'] -= 1
                            state['completed'] += int(worked)
                    if not worked:
                        stopping.wait(.25)
            except BaseException as exc:
                from operations.diagnostics import emit
                emit('worker_lane_failed', error_class=type(exc).__name__)
                with count_lock:
                    state['fatal'] = True
                stopping.set()
            finally:
                connections.close_all()

        lanes = ['recovery', 'normal', 'normal', 'normal']
        threads = [threading.Thread(target=loop, args=(lane,), name='mprp-' + lane + '-' + str(index), daemon=True) for index, lane in enumerate(lanes)]
        for thread in threads:
            thread.start()
        try:
            while not stopping.wait(.25):
                if not all(thread.is_alive() for thread in threads):
                    with count_lock:
                        state['fatal'] = True
                    stopping.set()
                    break
        finally:
            stopping.set()
            deadline = time.monotonic() + 25
            for thread in threads:
                thread.join(timeout=max(0, deadline - time.monotonic()))
            connections.close_all()
            if any(thread.is_alive() for thread in threads):
                self.stderr.write('Shutdown budget reached; unfinished durable claims will be reclaimed after lease expiry.')

        if state['fatal']:
            raise CommandError('A worker lane exited unexpectedly; the entire pool stopped and durable claims remain recoverable.')
