"""Factor consumption races across real PostgreSQL connections."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import pyotp
from django.contrib.auth.hashers import make_password
from django.db import close_old_connections, connections
from django.test import TransactionTestCase
from django.utils import timezone
from .domain import consume_recovery, consume_totp, encrypt
from .models import User


class FactorConsumptionConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.secret = pyotp.random_base32()
        self.user = User.objects.create_user(
            username=str(uuid.uuid4()), email='factor-race@example.test',
            password='A fictional factor race passphrase', verified=True,
            totp_confirmed=True, totp_secret=encrypt(self.secret),
        )

    def race(self, operation, code):
        barrier = threading.Barrier(3)
        user_id = self.user.pk
        def participant():
            close_old_connections()
            try:
                # Obtain a distinct request-side user instance and connection.
                user = User.objects.get(pk=user_id)
                barrier.wait(timeout=10)
                return operation(user, code)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(participant) for _ in range(2)]
            barrier.wait(timeout=10)
            return [future.result(timeout=15) for future in futures]

    def test_same_totp_counter_is_consumed_once_across_connections(self):
        fixed = timezone.now()
        counter = int(fixed.timestamp()) // 30
        code = pyotp.TOTP(self.secret).at(counter * 30)
        with patch('accounts.domain.timezone.now', return_value=fixed):
            results = self.race(consume_totp, code)
        self.assertEqual(sorted(results), [False, True])
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_totp_counter, counter)

    def test_same_recovery_code_is_consumed_once_across_connections(self):
        code = 'demo-recovery-6ec94774e801c0f2'
        self.user.recovery_hashes = [make_password(code)]
        self.user.save(update_fields=['recovery_hashes'])
        initial_epoch = self.user.auth_epoch
        results = self.race(consume_recovery, code)
        self.assertEqual(sorted(results), [False, True])
        self.user.refresh_from_db()
        self.assertEqual(self.user.recovery_hashes, [])
        self.assertEqual(self.user.auth_epoch, initial_epoch + 1)
