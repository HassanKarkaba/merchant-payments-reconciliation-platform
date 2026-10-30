import uuid
from datetime import timedelta
from django.test import TestCase,TransactionTestCase
from django.db import transaction,connection,DatabaseError
from django.utils import timezone
from django.core.exceptions import ValidationError
from accounts.models import User
from merchants.models import Merchant,Membership
from .models import Control,Operation,AuditEvent,Job
from .domain import admit_operation,amount_minor
from .audit import record,inspect
from .worker import claim,current_job
class OperationTests(TestCase):
    def setUp(self):
        from unittest.mock import patch
        readiness = patch('operations.health.local_admission_ready', return_value=True)
        readiness.start()
        self.addCleanup(readiness.stop)
        self.user=User.objects.create_user(username='actor',email='actor@example.test',password='A fictional strong passphrase',verified=True,totp_confirmed=True)
        self.merchant=Merchant.objects.create(name='Demo',status='ACTIVE');Membership.objects.create(merchant=self.merchant,user=self.user,role='FINANCE')
        Control.objects.create(environment_id=uuid.uuid4(),restore_hold=False,dispatch_epoch=1,worker_heartbeat=timezone.now())
    def test_exact_cents_rejects_ambiguous_input(self):
        self.assertEqual(amount_minor('100.01'),10001)
        for value in ['1e2','-1','١٠','1,000','1.001','nan','1 0']:
            with self.assertRaises(ValidationError):amount_minor(value)
    def test_idempotency_same_identity_conflict_and_hold(self):
        resource=uuid.uuid4();key='a'*32
        a=admit_operation(self.user,self.merchant,'REFUND',resource,key,{'amount_minor':100})
        b=admit_operation(self.user,self.merchant,'REFUND',resource,key,{'amount_minor':100})
        self.assertEqual(a.pk,b.pk);self.assertEqual(Job.objects.count(),1)
        with self.assertRaises(ValidationError):admit_operation(self.user,self.merchant,'REFUND',resource,key,{'amount_minor':200})
        Control.objects.update(restore_hold=True)
        with self.assertRaises(ValidationError):admit_operation(self.user,self.merchant,'REFUND',resource,'b'*32,{'amount_minor':100})
        self.assertEqual(Operation.objects.count(),1)
    def test_terminal_and_blocked_prefix_does_not_starve(self):
        Job.objects.bulk_create([Job(kind='test',status='DONE') for _ in range(60)]+[Job(kind='test',status='BLOCKED') for _ in range(60)])
        due=Job.objects.create(kind='test');self.assertEqual(claim(uuid.uuid4()).pk,due.pk)
    def test_stale_generation_cannot_finalize(self):
        item=Job.objects.create(kind='test');old=claim(uuid.uuid4());Job.objects.filter(pk=item.pk).update(lease_until=timezone.now()-timedelta(seconds=1));new=claim(uuid.uuid4())
        self.assertGreater(new.claim_generation,old.claim_generation)
        with transaction.atomic():self.assertIsNone(current_job(old));self.assertIsNotNone(current_job(new))
    def test_audit_chain_is_verifiable(self):
        record(self.user,self.merchant,'one',self.merchant.pk);record(self.user,self.merchant,'two',self.merchant.pk,{'amount_minor':123});self.assertTrue(inspect(str(self.merchant.pk)))
class DatabaseGuardTests(TransactionTestCase):
    def test_audit_direct_sql_update_and_delete_rejected(self):
        event=record(None,None,'guard_test')
        for sql in ['UPDATE operations_auditevent SET action=%s WHERE id=%s','DELETE FROM operations_auditevent WHERE id=%s']:
            with self.assertRaises(DatabaseError):
                with transaction.atomic():
                    with connection.cursor() as cursor:cursor.execute(sql,['tampered',event.pk] if sql.startswith('UPDATE') else [event.pk])

class ClosureBoundaryTests(TestCase):
    def setUp(self):
        import time
        self.operator=User.objects.create_user(username='closure_operator',email='operations@example.test',password='Fictional closure passphrase',verified=True,totp_confirmed=True,platform_role='OPERATIONS')
        self.owner=User.objects.create_user(username='closure_owner',email='owner@example.test',password='Fictional owner passphrase',verified=True,totp_confirmed=True)
        self.finance=User.objects.create_user(username='closure_finance',email='finance@example.test',password='Fictional finance passphrase',verified=True,totp_confirmed=True)
        self.merchant=Merchant.objects.create(name='Closure Fictional Shop',status='ACTIVE')
        Membership.objects.create(merchant=self.merchant,user=self.owner,role='OWNER')
        Membership.objects.create(merchant=self.merchant,user=self.finance,role='FINANCE')
        self.client.force_login(self.operator,backend='accounts.domain.EmailBackend')
        session=self.client.session
        session.update({'auth_epoch':self.operator.auth_epoch,'mfa_ok':True,'absolute_start':time.time(),'last_activity':time.time()})
        session.save()

    def test_empty_eligible_closure_revokes_links_and_preserves_owner_history(self):
        from payments.domain import create_link
        link,_=create_link(self.owner,self.merchant,10000,'MANUAL')
        self.merchant.status='CLOSING';self.merchant.save(update_fields=['status'])
        response=self.client.post(f'/operations/merchants/{self.merchant.pk}/close/',{'reason':'Fictional business closure'})
        self.assertEqual(response.status_code,303)
        self.merchant.refresh_from_db();link.refresh_from_db()
        self.assertEqual(self.merchant.status,'CLOSED')
        self.assertEqual(link.status,'REVOKED')
        self.assertFalse(Membership.objects.get(merchant=self.merchant,user=self.finance).active)
        self.assertTrue(Membership.objects.get(merchant=self.merchant,user=self.owner).active)

    def test_direct_source_inventory_blocks_without_an_exception_record(self):
        from accounting.domain import post_economic_event
        from operations.lifecycle import closure_blockers
        from reconciliation.models import ReconciliationException
        post_economic_event(self.merchant,str(uuid.uuid4()),'CAPTURE',10000,timezone.now())
        self.assertFalse(ReconciliationException.objects.filter(merchant=self.merchant).exists())
        self.assertTrue(any('statement evidence' in reason for reason in closure_blockers(self.merchant)))

    def test_current_platform_role_is_rechecked_after_preliminary_permission(self):
        from unittest.mock import patch
        self.merchant.status='CLOSING';self.merchant.save(update_fields=['status'])
        def revoke_during_preliminary_check(*args):
            User.objects.filter(pk=self.operator.pk).update(platform_role='AUDITOR')
            return self.operator
        with patch('operations.views.require_platform',side_effect=revoke_during_preliminary_check):
            response=self.client.post(f'/operations/merchants/{self.merchant.pk}/close/',{'reason':'Fictional business closure'})
        self.assertEqual(response.status_code,403)
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.status,'CLOSING')

class DiagnosticTests(TestCase):
    def test_failed_diagnostic_sink_never_blocks_cleanup(self):
        from unittest.mock import patch
        from .diagnostics import emit,sink_failure_count
        before=sink_failure_count()
        with patch('logging.Logger.warning',side_effect=OSError('sink unavailable')):emit('test',error_class='OSError',password='must never be logged')
        self.assertEqual(sink_failure_count(),before+1)

class AdmissionResponseTests(TestCase):
    def test_unready_capture_is_503_without_consuming_action(self):
        import secrets,time
        from payments.models import CheckoutLink,Payment
        user=User.objects.create(username=str(uuid.uuid4()),email='unready@example.test',verified=True,totp_confirmed=True)
        merchant=Merchant.objects.create(name='Readiness boundary',status='ACTIVE');Membership.objects.create(user=user,merchant=merchant,role='FINANCE')
        link=CheckoutLink.objects.create(merchant=merchant,creator=user,token_hash='f'*64,amount_minor=10000,capture_mode='MANUAL',expires_at=timezone.now()+timedelta(hours=1))
        payment=Payment.objects.create(merchant=merchant,link=link,reference='unready_capture',amount_minor=10000,authorized_minor=10000,state='AUTHORIZED',authorization_expires_at=timezone.now()+timedelta(days=7))
        self.client.force_login(user,backend='accounts.domain.EmailBackend');session=self.client.session;session.update({'auth_epoch':user.auth_epoch,'mfa_ok':True,'absolute_start':time.time(),'last_activity':time.time()});session.save()
        response=self.client.post(f'/m/{merchant.pk}/payments/{payment.pk}/capture/',{'action_key':secrets.token_hex(32),'expected_version':payment.version,'amount':'100.00'})
        self.assertEqual(response.status_code,503);self.assertEqual(Operation.objects.count(),0);self.assertEqual(Job.objects.count(),0)

class NotificationAuthorityTests(TestCase):
    def test_revoked_member_cannot_retrieve_or_receive_merchant_notice(self):
        from .domain import notify
        from .notifications import visible_notifications
        user=User.objects.create(username=str(uuid.uuid4()),email='notification@example.test',verified=True)
        merchant=Merchant.objects.create(name='Notice scope');member=Membership.objects.create(user=user,merchant=merchant,role='FINANCE')
        notify(user,'Financial result',merchant=merchant,event_key='one')
        self.assertEqual(visible_notifications(user).count(),1)
        member.active=False;member.save(update_fields=['active'])
        self.assertEqual(visible_notifications(user).count(),0)
        self.assertIsNone(notify(user,'Later result',merchant=merchant,event_key='two'))
