"""Dependency recovery must preserve durable duties and process stop semantics."""
from unittest.mock import Mock, patch
from django.db import InterfaceError, OperationalError
from django.test import SimpleTestCase
from processor_sim.worker import run_scheduler


class SchedulerRecoveryTests(SimpleTestCase):
    def test_transient_database_failure_reconnects_and_later_ticks_resume(self):
        stopping = Mock()
        stopping.is_set.side_effect = [False, False, False, False, True]
        secret = 'postgresql://private-user:private-password@private-db/private-data'
        with patch('processor_sim.worker.tick', side_effect=[OperationalError(secret), None, InterfaceError(secret), None]) as tick, \
             patch('processor_sim.worker.connections.close_all') as close, \
             self.assertLogs('processor_sim.worker', level='WARNING') as logs:
            run_scheduler(stopping, interval=0.5)
        self.assertEqual(tick.call_count, 4)
        self.assertEqual([call.args[0] for call in stopping.wait.call_args_list], [1, 0.5, 1, 0.5])
        self.assertEqual(close.call_count, 3)
        self.assertNotIn(secret, '\n'.join(logs.output))
        self.assertIn('OperationalError', logs.output[0])
        self.assertIn('InterfaceError', logs.output[1])

    def test_repeated_dependency_failure_has_capped_interruptible_backoff(self):
        stopping = Mock()
        stopping.is_set.side_effect = [False] * 7 + [True]
        with patch('processor_sim.worker.tick', side_effect=[OperationalError('unavailable')] * 6 + [None]), \
             patch('processor_sim.worker.connections.close_all'), \
             self.assertLogs('processor_sim.worker', level='WARNING'):
            run_scheduler(stopping, interval=0.5)
        self.assertEqual([call.args[0] for call in stopping.wait.call_args_list], [1, 2, 5, 10, 30, 30, 0.5])

    def test_stop_and_programming_exceptions_are_not_swallowed(self):
        for exception in (KeyboardInterrupt(), SystemExit(2), ValueError('programming failure')):
            with self.subTest(error=type(exception).__name__):
                stopping = Mock()
                stopping.is_set.return_value = False
                with patch('processor_sim.worker.tick', side_effect=exception), \
                     patch('processor_sim.worker.connections.close_all') as close, \
                     self.assertRaises(type(exception)):
                    run_scheduler(stopping)
                close.assert_called_once()
                stopping.wait.assert_not_called()

    def test_one_shot_failure_still_reports_failed_duty(self):
        stopping = Mock()
        stopping.is_set.return_value = False
        with patch('processor_sim.worker.tick', side_effect=OperationalError('unavailable')), \
             patch('processor_sim.worker.connections.close_all'), \
             self.assertRaises(OperationalError):
            run_scheduler(stopping, once=True)
        stopping.wait.assert_not_called()
