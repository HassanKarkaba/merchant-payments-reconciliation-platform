"""Export snapshot/publication races against real PostgreSQL connections."""
import csv
import io
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase
from accounts.models import User
from accounting.domain import post_economic_event
from merchants.models import Merchant, Membership
from operations.models import AuditEvent, Export, Job
from operations import reports
from operations.worker import claim, database_now


class ExportPublicationTests(TransactionTestCase):
    def setUp(self):
        self.actor = User.objects.create_user(username=str(uuid.uuid4()), email='export-race@example.test', password='Fictional export race passphrase', verified=True, totp_confirmed=True)
        self.merchant = Merchant.objects.create(name='Fictional Snapshot Shop', status='ACTIVE')
        self.membership = Membership.objects.create(user=self.actor, merchant=self.merchant, role='FINANCE')
        post_economic_event(self.merchant, 'snapshot_initial', 'CAPTURE', 10000, database_now())
        self.export = Export.objects.create(requester=self.actor, merchant=self.merchant, kind='ledger')
        Job.objects.create(merchant=self.merchant, kind='export_build', payload={'export_id': str(self.export.pk)})
        self.job = claim(uuid.uuid4())
        self.thread_pids = []
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_backend_pid()')
            self.main_pid = cursor.fetchone()[0]

    def in_connection(self, function):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_backend_pid()')
                self.thread_pids.append(cursor.fetchone()[0])
            return function()
        finally:
            connections.close_all()

    def reclaim(self):
        Job.objects.filter(pk=self.job.pk).update(lease_until=database_now() - timedelta(microseconds=1))
        replacement = claim(uuid.uuid4())
        self.assertEqual(replacement.pk, self.job.pk)
        self.assertGreater(replacement.claim_generation, self.job.claim_generation)
        return replacement

    def assert_separate_connection(self):
        self.assertTrue(self.thread_pids)
        self.assertTrue(all(pid != self.main_pid for pid in self.thread_pids))

    def test_trading_commits_during_readonly_snapshot_and_old_cutoff_publishes(self):
        reached = threading.Event()
        resume = threading.Event()
        original = reports._write_ledger
        def controlled(export, output, sequence):
            reached.set()
            if not resume.wait(15):
                raise TimeoutError('Snapshot test barrier was not released')
            return original(export, output, sequence)
        with patch('operations.reports._write_ledger', side_effect=controlled), ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.in_connection, lambda: reports.build_export(self.export.pk, job=self.job))
            try:
                self.assertTrue(reached.wait(10))
                # This commits while the other connection still owns its RR
                # snapshot. Publication must use a new transaction afterward.
                post_economic_event(self.merchant, 'snapshot_later', 'CAPTURE', 20000, database_now())
            finally:
                resume.set()
            self.assertTrue(future.result(timeout=20))
        self.assert_separate_connection()
        self.export.refresh_from_db();self.merchant.refresh_from_db()
        self.assertEqual(self.export.status, 'READY')
        self.assertEqual(self.export.cutoff_sequence, 1)
        self.assertEqual(self.merchant.ledger_sequence, 2)
        data = bytes(self.export.content)
        self.assertIn(b'snapshot_initial', data)
        self.assertNotIn(b'snapshot_later', data)
        rows = list(csv.reader(io.StringIO(data.decode('utf-8'))))
        self.assertEqual(len(rows[2:]), 2)
        self.assertEqual([int(row[-1]) for row in rows[2:]], [10000, 10000])
        self.assertEqual(self.export.filters['snapshot']['economic_source_count'], 1)
        self.assertEqual(AuditEvent.objects.filter(action='export.generated', resource_id=str(self.export.pk)).count(), 1)

    def test_stale_generation_cannot_replace_or_clear_ready_winner(self):
        reached = threading.Event()
        resume = threading.Event()
        original = reports._publish_snapshot
        def controlled(snapshot, job):
            if job.claim_generation == self.job.claim_generation:
                reached.set()
                if not resume.wait(15):
                    raise TimeoutError('Publication test barrier was not released')
            return original(snapshot, job)
        with patch('operations.reports._publish_snapshot', side_effect=controlled), ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.in_connection, lambda: reports.build_export(self.export.pk, job=self.job))
            try:
                self.assertTrue(reached.wait(10))
                replacement = self.reclaim()
                post_economic_event(self.merchant, 'winner_later', 'CAPTURE', 20000, database_now())
                self.assertTrue(reports.build_export(self.export.pk, job=replacement))
                self.export.refresh_from_db()
                winner = (bytes(self.export.content), self.export.generated_at, self.export.expires_at, self.export.cutoff_sequence)
            finally:
                resume.set()
            with self.assertRaises(reports.StaleExportWorker):
                future.result(timeout=20)
        self.assert_separate_connection()
        self.export.refresh_from_db()
        self.assertEqual(self.export.status, 'READY')
        self.assertEqual((bytes(self.export.content), self.export.generated_at, self.export.expires_at, self.export.cutoff_sequence), winner)
        self.assertIn(b'winner_later', bytes(self.export.content))
        self.assertEqual(AuditEvent.objects.filter(action='export.generated', resource_id=str(self.export.pk)).count(), 1)
        self.assertFalse(AuditEvent.objects.filter(action='export.build_failed', resource_id=str(self.export.pk)).exists())

    def test_late_old_builder_failure_cannot_erase_ready_winner(self):
        reached = threading.Event()
        resume = threading.Event()
        original = reports._write_ledger
        def controlled(export, output, sequence):
            if threading.current_thread().name.startswith('old-export'):
                reached.set()
                if not resume.wait(15):
                    raise TimeoutError('Failure test barrier was not released')
                raise ValueError('Controlled old builder failure')
            return original(export, output, sequence)
        with patch('operations.reports._write_ledger', side_effect=controlled), ThreadPoolExecutor(max_workers=1, thread_name_prefix='old-export') as executor:
            future = executor.submit(self.in_connection, lambda: reports.build_export(self.export.pk, job=self.job))
            try:
                self.assertTrue(reached.wait(10))
                replacement = self.reclaim()
                self.assertTrue(reports.build_export(self.export.pk, job=replacement))
                self.export.refresh_from_db()
                winner = bytes(self.export.content)
            finally:
                resume.set()
            with self.assertRaises(ValueError):
                future.result(timeout=20)
        self.assert_separate_connection()
        self.export.refresh_from_db()
        self.assertEqual(self.export.status, 'READY')
        self.assertEqual(bytes(self.export.content), winner)
        self.assertFalse(AuditEvent.objects.filter(action='export.build_failed', resource_id=str(self.export.pk)).exists())

    def test_revocation_before_publication_prevents_bytes(self):
        reached = threading.Event()
        resume = threading.Event()
        original = reports._publish_snapshot
        def controlled(snapshot, job):
            reached.set()
            if not resume.wait(15):
                raise TimeoutError('Authority test barrier was not released')
            return original(snapshot, job)
        with patch('operations.reports._publish_snapshot', side_effect=controlled), ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.in_connection, lambda: reports.build_export(self.export.pk, job=self.job))
            try:
                self.assertTrue(reached.wait(10))
                Membership.objects.filter(pk=self.membership.pk).update(active=False)
            finally:
                resume.set()
            # The domain deliberately conceals a no-longer-authorized tenant.
            from django.http import Http404
            with self.assertRaises((PermissionDenied, Http404)):
                future.result(timeout=20)
        self.assert_separate_connection()
        self.export.refresh_from_db()
        self.assertNotEqual(self.export.status, 'READY')
        self.assertIsNone(self.export.content)
        self.assertFalse(AuditEvent.objects.filter(action='export.generated', resource_id=str(self.export.pk)).exists())

    def test_mandatory_audit_failure_never_publishes_bytes(self):
        with patch('operations.reports.record', side_effect=RuntimeError('Controlled mandatory audit failure')):
            with self.assertRaises(RuntimeError):
                reports.build_export(self.export.pk, job=self.job)
        self.export.refresh_from_db()
        self.assertEqual(self.export.status, 'QUEUED')
        self.assertIsNone(self.export.content)
        self.assertIsNone(self.export.generated_at)
        self.assertFalse(AuditEvent.objects.filter(action='export.generated', resource_id=str(self.export.pk)).exists())

    def test_failed_current_duplicate_job_never_clears_ready_bytes(self):
        reached = threading.Event()
        resume = threading.Event()
        original = reports._write_ledger
        def controlled(export, output, sequence):
            if threading.current_thread().name.startswith('old-export'):
                reached.set()
                if not resume.wait(15):
                    raise TimeoutError('Duplicate job test barrier was not released')
                raise ValueError('Controlled current duplicate builder failure')
            return original(export, output, sequence)
        with patch('operations.reports._write_ledger', side_effect=controlled), ThreadPoolExecutor(max_workers=1, thread_name_prefix='old-export') as executor:
            future = executor.submit(self.in_connection, lambda: reports.build_export(self.export.pk, job=self.job))
            try:
                self.assertTrue(reached.wait(10))
                duplicate = Job.objects.create(merchant=self.merchant, kind='export_build', payload={'export_id': str(self.export.pk)})
                duplicate_claim = claim(uuid.uuid4())
                self.assertEqual(duplicate_claim.pk, duplicate.pk)
                self.assertTrue(reports.build_export(self.export.pk, job=duplicate_claim))
                self.export.refresh_from_db()
                winner = bytes(self.export.content)
                self.assertTrue(Job.objects.filter(pk=self.job.pk, status='CLAIMED', lease_until__gt=database_now()).exists())
            finally:
                resume.set()
            with self.assertRaises(ValueError):
                future.result(timeout=20)
        self.assert_separate_connection()
        self.assertTrue(Job.objects.filter(pk=self.job.pk, status='CLAIMED', lease_until__gt=database_now()).exists())
        self.export.refresh_from_db()
        self.assertEqual(self.export.status, 'READY')
        self.assertEqual(bytes(self.export.content), winner)
        self.assertFalse(AuditEvent.objects.filter(action='export.build_failed', resource_id=str(self.export.pk)).exists())
