"""Cohort probes recover from transient DB faults without adopting a new run."""
import threading
import uuid
from unittest.mock import patch
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import TransactionTestCase
from .models import Control
from . import run_identity
from .management.commands import run_worker


class WorkerLoopRecoveryTests(TransactionTestCase):
    def setUp(self):
        identity=patch('operations.run_identity._bound_identity',None)
        identity.start();self.addCleanup(identity.stop)
        self.control=Control.objects.create(environment_id=settings.MPRP_ENVIRONMENT_ID,dispatch_epoch=1,simulator_instance_id=str(uuid.uuid4()),restore_hold=False)

    def test_all_lanes_recover_then_stop_on_replaced_cohort_without_rebinding(self):
        original=run_worker._read_control
        lock=threading.Lock();faulted=set();progressed=set();changed=False
        original_token=str(self.control.run_token)
        def transient():
            name=threading.current_thread().name
            with lock:
                if name not in faulted:
                    faulted.add(name)
                    raise OperationalError('Controlled transient cohort query failure')
            return original()
        def healthy_tick(owner,lane='any'):
            nonlocal changed
            with lock:
                progressed.add(threading.current_thread().name)
                if len(progressed)==4 and not changed:
                    Control.objects.filter(pk=1).update(run_token=uuid.uuid4(),dispatch_epoch=2)
                    changed=True
            return False
        with patch('operations.management.commands.run_worker._read_control',side_effect=transient),patch('operations.management.commands.run_worker.tick',side_effect=healthy_tick),patch('operations.management.commands.run_worker.poll_events'),patch('operations.management.commands.run_worker.schedule_maintenance'),patch('payments.domain.automatic_capture_candidates',return_value=[]),patch('operations.management.commands.run_worker.signal.signal'),patch('operations.diagnostics.emit') as emit:
            call_command('run_worker',verbosity=0)
        self.assertEqual(len(faulted),4)
        self.assertEqual(len(progressed),4)
        self.assertTrue(changed)
        self.assertEqual(run_identity._bound_identity.run_token,original_token)
        self.assertNotEqual(str(Control.objects.get(pk=1).run_token),original_token)
        self.assertFalse(any(thread.name.startswith('mprp-') and thread.is_alive() for thread in threading.enumerate()))
        probes=[call for call in emit.call_args_list if call.args[0]=='worker_cohort_probe_failed']
        self.assertEqual(len(probes),4)
        self.assertTrue(all(call.kwargs=={'error_class':'OperationalError'} for call in probes))

    def test_unexpected_lane_exit_stops_the_entire_pool(self):
        with patch('operations.management.commands.run_worker.tick',side_effect=KeyboardInterrupt),patch('operations.management.commands.run_worker.poll_events'),patch('operations.management.commands.run_worker.schedule_maintenance'),patch('payments.domain.automatic_capture_candidates',return_value=[]),patch('operations.management.commands.run_worker.signal.signal'),patch('operations.diagnostics.emit') as emit:
            with self.assertRaises(CommandError):
                call_command('run_worker',verbosity=0)
        self.assertTrue(any(call.args[0]=='worker_lane_failed' for call in emit.call_args_list))
        self.assertFalse(any(thread.name.startswith('mprp-') and thread.is_alive() for thread in threading.enumerate()))

    def test_once_keeps_database_failures_fail_fast(self):
        with patch('operations.management.commands.run_worker.schedule_maintenance',side_effect=OperationalError('Controlled once failure')),patch('operations.management.commands.run_worker.signal.signal'):
            with self.assertRaises(OperationalError):
                call_command('run_worker',once=True,verbosity=0)
