"""Request/result and recovery approval boundaries; provider I/O is explicitly substituted."""
import hashlib
import uuid
from unittest.mock import patch
from django.conf import settings
from django.core.exceptions import PermissionDenied,ValidationError
from django.test import TestCase
from accounts.models import User
from merchants.models import Merchant
from operations.models import Control,RecoveryInventory,Inbox,Operation
from operations.audit import canonical
from operations import provider,recovery


class RecoveryApprovalTests(TestCase):
    def setUp(self):
        self.maker=User.objects.create_user(username='recovery_maker',email='recovery-maker@example.test',password=uuid.uuid4().hex,verified=True,totp_confirmed=True,platform_role='OPERATIONS')
        self.checker=User.objects.create_user(username='recovery_checker',email='recovery-checker@example.test',password=uuid.uuid4().hex,verified=True,totp_confirmed=True,platform_role='FINANCE_APPROVER')
        self.control=Control.objects.create(environment_id=settings.MPRP_ENVIRONMENT_ID,simulator_instance_id=str(uuid.uuid4()),dispatch_epoch=3,restore_hold=True,received_cursor=2)
        self.proposal=RecoveryInventory.objects.create(maker=self.maker,environment_id=self.control.environment_id,simulator_instance_id=self.control.simulator_instance_id,dispatch_epoch=3,event_high_water=2,source_high_water=4,received_cursor=2,summary={'verified_sources':4},findings=[],fingerprint='a'*64,reason='Isolated restoration test')
        self.remote={'simulator_instance_id':self.control.simulator_instance_id,'dispatch_epoch':3,'event_high_water':2,'source_high_water':4,'restored':False}

    def test_distinct_checker_releases_current_reconciled_inventory(self):
        with patch('operations.provider.request',return_value=self.remote):
            recovery.approve_release(self.proposal.pk,self.checker)
        self.control.refresh_from_db();self.proposal.refresh_from_db()
        self.assertFalse(self.control.restore_hold)
        self.assertEqual(self.proposal.status,'APPROVED')
        self.assertEqual(self.proposal.checker,self.checker)
        self.assertFalse(self.control.provider_ready)

    def test_self_approval_and_changed_provider_cutoff_rejected(self):
        self.maker.platform_role='FINANCE_APPROVER';self.maker.save(update_fields=['platform_role'])
        with self.assertRaises(PermissionDenied):recovery.approve_release(self.proposal.pk,self.maker)
        with patch('operations.provider.request',return_value=dict(self.remote,source_high_water=5)):
            with self.assertRaises(ValidationError):recovery.approve_release(self.proposal.pk,self.checker)
        self.control.refresh_from_db();self.assertTrue(self.control.restore_hold)

    def test_unapplied_received_events_prevent_release(self):
        Inbox.objects.create(event_id=str(uuid.uuid4()),payload_hash='b'*64,payload={},cursor=2)
        with patch('operations.provider.request',return_value=self.remote):
            with self.assertRaises(ValidationError):recovery.approve_release(self.proposal.pk,self.checker)
        self.control.refresh_from_db();self.assertTrue(self.control.restore_hold)

    def test_revocation_after_preliminary_auth_prevents_commit(self):
        def remote_after_revocation(*args,**kwargs):
            User.objects.filter(pk=self.checker.pk).update(platform_role='AUDITOR')
            return self.remote
        with patch('operations.provider.request',side_effect=remote_after_revocation):
            with self.assertRaises(PermissionDenied):recovery.approve_release(self.proposal.pk,self.checker)
        self.control.refresh_from_db();self.assertTrue(self.control.restore_hold)

    def test_strict_result_binds_database_key_and_canonical_payload(self):
        merchant=Merchant.objects.create(name='Recovery scope fixture')
        payload={'amount_minor':10000,'payment_id':str(uuid.uuid4())}
        operation=Operation.objects.create(merchant=merchant,actor=self.maker,kind='CAPTURE',resource_id=payload['payment_id'],action_key='a'*32,payload=payload,fingerprint=hashlib.sha256(canonical(payload).encode()).hexdigest())
        result={'schema_version':1,'policy_version':'mprp_v1','environment_id':str(self.control.environment_id),'simulator_instance_id':self.control.simulator_instance_id,'operation_key':str(operation.provider_key),'payload_hash':operation.fingerprint,'state':'FAILED','reason':'Definitive no-effect rejection','snapshot':None,'source_high_water':4}
        self.assertIs(provider.validate_result(result,operation),result)
        for modified in [dict(result,environment_id=str(uuid.uuid4())),dict(result,simulator_instance_id=str(uuid.uuid4())),dict(result,payload_hash='f'*64),dict(result,unexpected=True)]:
            with self.assertRaises(provider.ProviderUncertain):provider.validate_result(modified,operation)


from concurrent.futures import ThreadPoolExecutor
import threading
import time
from django.db import close_old_connections,connection,transaction
from django.test import TransactionTestCase
from operations.models import AuditHead
from operations.audit import record,inspect


class AuditLockOrderTests(TransactionTestCase):
    def test_waiting_receipt_never_holds_head_before_merchant(self):
        merchant=Merchant.objects.create(name='Audit lock-order fixture')
        record(None,merchant,'audit.fixture')
        ready=threading.Event()
        state={}
        def receipt():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    state['pid']=cursor.fetchone()[0]
                ready.set()
                record(None,merchant,'audit.receipt')
            finally:
                close_old_connections()
        future=None
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                with transaction.atomic():
                    Merchant.objects.select_for_update().get(pk=merchant.pk)
                    future=pool.submit(receipt)
                    self.assertTrue(ready.wait(10))
                    deadline=time.monotonic()+10
                    blocked=False
                    while time.monotonic()<deadline:
                        with connection.cursor() as cursor:
                            cursor.execute('SELECT cardinality(pg_blocking_pids(%s))>0',[state['pid']])
                            blocked=cursor.fetchone()[0]
                        if blocked:break
                        time.sleep(.01)
                    self.assertTrue(blocked,'The receipt must reach an actual database lock wait')
                    # If the receipt took AuditHead first this NOWAIT fails,
                    # exposing the same inversion as callback/result recording.
                    AuditHead.objects.select_for_update(nowait=True).get(stream=str(merchant.pk))
                    record(None,merchant,'audit.financial_result')
            finally:
                if future:future.result(timeout=10)
        self.assertTrue(inspect(str(merchant.pk)))
