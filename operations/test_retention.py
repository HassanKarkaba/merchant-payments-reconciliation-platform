import os
import json
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.core.exceptions import ValidationError,PermissionDenied
from django.db import connection,IntegrityError
from django.test import TransactionTestCase,SimpleTestCase
from django.utils import timezone
from accounts.models import User
from merchants.models import Merchant,Membership
from reconciliation.models import StatementImport
from .models import Control,Operation,PurgeReceipt,Notification,Outbox,Export,ApprovalReceipt
from .retention import backup_inventory,cleanup_transient,propose_purge,approve_purge,purge_blockers
from .audit import record

class BackupInventoryTests(SimpleTestCase):
    def test_expiry_requires_explicit_matching_store_and_never_follows_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with self.assertRaises(ValidationError):backup_inventory(root,settings.MPRP_ENVIRONMENT_ID)
            (root/'.mprp-backup-root').write_text(str(settings.MPRP_ENVIRONMENT_ID))
            dump=root/'main-demo.dump';dump.write_bytes(b'synthetic backup')
            self.assertFalse(backup_inventory(root,settings.MPRP_ENVIRONMENT_ID)['empty'])
            before=(timezone.now()-timedelta(days=31)).timestamp();os.utime(dump,(before,before))
            result=backup_inventory(root,settings.MPRP_ENVIRONMENT_ID,expire=True)
            self.assertTrue(result['empty']);self.assertFalse(dump.exists())
            (root/'unsafe.dump').symlink_to('/tmp')
            with self.assertRaises(ValidationError):backup_inventory(root,settings.MPRP_ENVIRONMENT_ID,expire=True)

class RetentionAndPurgeTests(TransactionTestCase):
    def setUp(self):
        self.operator=User.objects.create_user(username='purge-ops',email='purge-ops@example.test',password='test-only-password-123',platform_role='OPERATIONS',verified=True,totp_confirmed=True)
        self.checker=User.objects.create_user(username='purge-finance',email='purge-finance@example.test',password='test-only-password-123',platform_role='FINANCE_APPROVER',verified=True,totp_confirmed=True)
        self.merchant=Merchant.objects.create(name='Fictional Retained Co',status='CLOSED',closed_at=timezone.now()-timedelta(days=366))
        self.other=Merchant.objects.create(name='Fictional Unrelated Co',status='ACTIVE')
        Control.objects.create(environment_id=settings.MPRP_ENVIRONMENT_ID,restore_hold=False,dispatch_epoch=1)
        self.backup={'empty':True,'checked_at':timezone.now().isoformat(),'inventory_hash':'a'*64,'retained_backup_count':0}
    def test_expiry_clears_transient_bytes_and_preserves_unknown_keys(self):
        now=timezone.now()
        terminal=Operation.objects.create(merchant=self.merchant,actor=self.operator,kind='CAPTURE',resource_id=uuid.uuid4(),action_key='x'*48,fingerprint='f'*64,status='SUCCEEDED',result={'safe_message':'done'},completed_at=now-timedelta(days=91))
        unknown=Operation.objects.create(merchant=self.other,actor=self.operator,kind='REFUND',resource_id=uuid.uuid4(),action_key='y'*48,fingerprint='e'*64,status='UNKNOWN',result={'safe_message':'inspect'},completed_at=now-timedelta(days=91))
        export=Export.objects.create(requester=self.operator,merchant=self.merchant,status='READY',content=b'synthetic',expires_at=now-timedelta(seconds=1))
        cleanup_transient()
        terminal.refresh_from_db();unknown.refresh_from_db();export.refresh_from_db()
        self.assertEqual(terminal.result,{});self.assertEqual(unknown.result,{'safe_message':'inspect'});self.assertIsNone(export.content)
        self.assertTrue(Operation.objects.filter(pk=terminal.pk,provider_key=terminal.provider_key).exists())
        self.assertTrue(PurgeReceipt.objects.filter(merchant_id_safe=self.merchant.pk,status='ELIGIBLE').exists())
    def test_retention_active_work_and_inventory_changes_block_decisions(self):
        self.merchant.closed_at=timezone.now()-timedelta(days=364);self.merchant.save(update_fields=['closed_at'])
        self.assertTrue(purge_blockers(self.merchant))
        self.merchant.closed_at=timezone.now()-timedelta(days=366);self.merchant.save(update_fields=['closed_at'])
        receipt=propose_purge(self.operator,self.merchant,self.backup)
        Notification.objects.create(recipient=self.operator,merchant=self.merchant,event_key='late-notification',title='Safe notice')
        with self.assertRaises(ValidationError):approve_purge(self.checker,receipt)
        receipt=propose_purge(self.operator,self.merchant,self.backup)
        receipt=approve_purge(self.checker,receipt)
        self.assertEqual(receipt.status,'APPROVED')
        with self.assertRaises(PermissionDenied):approve_purge(self.operator,receipt)
    def test_runtime_cannot_delete_financial_dataset_or_advance_purge(self):
        Membership.objects.create(merchant=self.merchant,user=self.operator,role='OWNER',active=False)
        with self.assertRaises(IntegrityError):Membership.objects.filter(merchant=self.merchant).delete()
        receipt=propose_purge(self.operator,self.merchant,self.backup)
        with self.assertRaises(IntegrityError):PurgeReceipt.objects.filter(pk=receipt.pk).update(status='COMPLETE')
    def test_separate_purge_role_resumes_bounded_scope_and_keeps_namespace_receipt(self):
        Membership.objects.create(merchant=self.merchant,user=self.operator,role='OWNER',active=False)
        Notification.objects.create(recipient=self.operator,merchant=self.merchant,event_key='tenant-notice',title='Fictional notice')
        Notification.objects.create(recipient=self.operator,merchant=self.other,event_key='other-notice',title='Unrelated notice')
        record(self.operator,self.merchant,'fixture.retained',self.merchant.pk)
        approval=ApprovalReceipt.objects.create(actor=self.checker,merchant=self.merchant,kind='SCOPE_TEST',resource_id=uuid.uuid4(),action_key='a'*48,fingerprint='b'*64,result_ref=self.merchant.pk,result_model='merchants.merchant')
        other_approval=ApprovalReceipt.objects.create(actor=self.checker,merchant=self.other,kind='SCOPE_TEST',resource_id=uuid.uuid4(),action_key='c'*48,fingerprint='d'*64,result_ref=self.other.pk,result_model='merchants.merchant')
        with self.assertRaises(IntegrityError):ApprovalReceipt.objects.filter(pk=approval.pk).update(fingerprint='e'*64)
        receipt=approve_purge(self.checker,propose_purge(self.operator,self.merchant,self.backup))
        receipt.status='RETIRED';receipt.simulator_receipt={'retired':True,'purge_complete':True,'provider_account_id':str(self.merchant.provider_account_id),'remaining_rows':0};receipt.save(update_fields=['status','simulator_receipt'])
        with connection.cursor() as cursor:
            cursor.execute("DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='mprp_purge') THEN CREATE ROLE mprp_purge LOGIN; END IF; END $$")
            cursor.execute('GRANT EXECUTE ON FUNCTION public.mprp_purge_merchant_batch(uuid,integer) TO mprp_purge')
            with self.assertRaises(Exception):cursor.execute('SELECT public.mprp_purge_merchant_batch(%s,1)',[receipt.pk])
            cursor.execute('SET SESSION AUTHORIZATION mprp_purge')
            try:
                first=cursor.execute('SELECT public.mprp_purge_merchant_batch(%s,1)',[receipt.pk])
                progress=cursor.fetchone()[0]
                if isinstance(progress,str):progress=json.loads(progress)
                self.assertEqual(progress['status'],'PURGING')
                for _ in range(100):
                    cursor.execute('SELECT public.mprp_purge_merchant_batch(%s,1)',[receipt.pk]);progress=cursor.fetchone()[0]
                    if isinstance(progress,str):progress=json.loads(progress)
                    if progress['status']=='COMPLETE':break
            finally:cursor.execute('RESET SESSION AUTHORIZATION')
        self.assertEqual(progress['status'],'COMPLETE')
        self.assertFalse(Merchant.objects.filter(pk=self.merchant.pk).exists())
        self.assertTrue(Merchant.objects.filter(pk=self.other.pk).exists())
        self.assertTrue(Notification.objects.filter(event_key='other-notice').exists())
        self.assertFalse(ApprovalReceipt.objects.filter(pk=approval.pk).exists());self.assertTrue(ApprovalReceipt.objects.filter(pk=other_approval.pk).exists())
        receipt.refresh_from_db();self.assertEqual(receipt.status,'COMPLETE');self.assertTrue(receipt.final_audit_heads)
        with self.assertRaises(IntegrityError):Merchant.objects.create(id=self.merchant.pk,name='Forbidden retired namespace')
        with self.assertRaises(IntegrityError):Merchant.objects.create(provider_account_id=self.merchant.provider_account_id,name='Forbidden retired provider')
