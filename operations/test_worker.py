import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch
from django.db import close_old_connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone
from accounts.models import User
from merchants.models import Merchant, Membership
from payments.models import CheckoutLink, Payment, Refund
from .models import Control, Job, Operation
from .worker import claim, current_job, database_now, dispatch, refresh_lease


class WorkerSchedulingTests(TransactionTestCase):
    def merchant(self, name):
        return Merchant.objects.create(name=name, status='ACTIVE')

    def test_distinct_merchant_round_robin_before_fifty_row_bound(self):
        older = self.merchant('Fictional busy merchant')
        later = self.merchant('Fictional later merchant')
        now = timezone.now()
        Job.objects.bulk_create([Job(kind='unit', merchant=older, due_at=now) for _ in range(100)])
        healthy = Job.objects.create(kind='unit', merchant=later, due_at=now + timedelta(microseconds=1))
        first = claim(uuid.uuid4(), lane='normal')
        self.assertEqual(first.merchant_id, older.pk)
        Job.objects.filter(pk=first.pk).update(status='DONE')
        second = claim(uuid.uuid4(), lane='normal')
        self.assertEqual(second.pk, healthy.pk)

    def test_three_normal_claims_preserve_one_recovery_slot(self):
        for index in range(4):
            Job.objects.create(kind='unit', merchant=self.merchant(f'Fictional {index}'))
        recovery = Job.objects.create(kind='callback_apply', payload={'event_id': 'unit-no-effect'})
        for _ in range(3):
            self.assertIsNotNone(claim(uuid.uuid4(), lane='normal'))
        self.assertIsNone(claim(uuid.uuid4(), lane='normal'))
        self.assertEqual(claim(uuid.uuid4(), lane='recovery').pk, recovery.pk)
        self.assertIsNone(claim(uuid.uuid4(), lane='any'))
        self.assertEqual(Job.objects.filter(status='CLAIMED').count(), 4)

    def test_expired_owner_cannot_renew_or_finalize_after_other_connection_claims(self):
        job = Job.objects.create(kind='unit', merchant=self.merchant('Fictional leased merchant'))
        stale = claim(uuid.uuid4())
        Job.objects.filter(pk=job.pk).update(lease_until=database_now() - timedelta(microseconds=1))
        barrier = threading.Barrier(2)
        def reclaim():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                return claim(uuid.uuid4())
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(reclaim)
            barrier.wait(timeout=5)
            current = future.result(timeout=10)
        self.assertEqual(current.pk, stale.pk)
        self.assertGreater(current.claim_generation, stale.claim_generation)
        self.assertFalse(refresh_lease(stale))
        with transaction.atomic():
            self.assertIsNone(current_job(stale))
            self.assertIsNotNone(current_job(current))

    def test_lease_expiry_equality_rejects_renewal(self):
        Job.objects.create(kind='unit', merchant=self.merchant('Fictional equality merchant'))
        job = claim(uuid.uuid4())
        exact = timezone.now()
        Job.objects.filter(pk=job.pk).update(lease_until=exact)
        with patch('operations.worker.database_now', return_value=exact):
            self.assertFalse(refresh_lease(job))
            replacement = claim(uuid.uuid4())
        self.assertEqual(replacement.pk, job.pk)
        self.assertEqual(replacement.claim_generation, job.claim_generation + 1)

    def test_uncertainty_twenty_four_hour_boundary_preserves_refund_reservation(self):
        merchant = self.merchant('Fictional uncertain merchant')
        actor = User.objects.create_user(username='worker_actor', email='worker@example.test', password='A fictional worker passphrase', verified=True, totp_confirmed=True)
        Membership.objects.create(user=actor, merchant=merchant, role='FINANCE')
        link = CheckoutLink.objects.create(merchant=merchant, creator=actor, token_hash='a' * 64, amount_minor=10000, capture_mode='MANUAL', expires_at=timezone.now() + timedelta(days=1))
        payment = Payment.objects.create(merchant=merchant, link=link, reference='unit_uncertain', amount_minor=10000, authorized_minor=10000, captured_minor=10000, state='CAPTURED')
        exact = timezone.now()
        op = Operation.objects.create(merchant=merchant, actor=actor, kind='REFUND', resource_id=payment.pk, action_key='b' * 32, fingerprint='c' * 64, status='UNKNOWN', result={'uncertain_since': (exact - timedelta(hours=24)).isoformat()})
        refund = Refund.objects.create(merchant=merchant, payment=payment, operation=op, amount_minor=2500, reason='Fictional refund', reserved=True)
        Control.objects.create(environment_id=uuid.uuid4(), dispatch_epoch=1)
        item = Job.objects.create(kind='financial_resolve', merchant=merchant, payload={'operation_id': str(op.pk)})
        job = claim(uuid.uuid4(), lane='recovery')
        with patch('operations.worker.database_now', return_value=exact), patch('operations.worker.provider.request') as request:
            dispatch(job)
            request.assert_not_called()
        item.refresh_from_db();op.refresh_from_db();refund.refresh_from_db()
        self.assertEqual(item.status, 'NEEDS_REVIEW')
        self.assertEqual(op.status, 'UNKNOWN')
        self.assertTrue(refund.reserved)

    def test_terminal_operation_prefix_is_excluded_before_claim_bound(self):
        merchant = self.merchant('Fictional cancelled prefix')
        now = timezone.now()
        for index in range(55):
            op = Operation.objects.create(merchant=merchant, kind='CAPTURE', resource_id=uuid.uuid4(), action_key=f'{index:032d}', fingerprint='d' * 64, status='CANCELLED')
            Job.objects.create(kind='financial_dispatch', merchant=merchant, due_at=now, payload={'operation_id': str(op.pk)})
        healthy = Job.objects.create(kind='unit', merchant=merchant, due_at=now + timedelta(microseconds=1))
        self.assertEqual(claim(uuid.uuid4()).pk, healthy.pk)

    def test_missing_admission_is_reviewed_and_holds_new_money(self):
        from .worker import tick
        now = timezone.now()
        Control.objects.create(environment_id=uuid.uuid4(), restore_hold=False, dispatch_epoch=1, provider_ready=True, provider_checked_at=now)
        job = Job.objects.create(kind='financial_dispatch', merchant=self.merchant('Fictional restored namespace'), payload={'operation_id': str(uuid.uuid4())})
        with patch('operations.worker.provider.request') as request:
            self.assertTrue(tick(uuid.uuid4()))
            request.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, 'NEEDS_REVIEW')
        self.assertTrue(Control.objects.get(pk=1).restore_hold)

    def test_unsupported_job_version_is_reviewed_without_dispatch(self):
        from .worker import tick
        job = Job.objects.create(kind='financial_dispatch', schema_version=2, merchant=self.merchant('Fictional future schema'), payload={'operation_id': str(uuid.uuid4())})
        with patch('operations.worker.provider.request') as request:
            self.assertTrue(tick(uuid.uuid4()))
            request.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, 'NEEDS_REVIEW')
        self.assertIn('Unsupported retained schema', job.last_error)
