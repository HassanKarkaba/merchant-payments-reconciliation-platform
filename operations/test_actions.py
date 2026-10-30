"""Scoped local approvals, replay, and recipient-lock coordination."""
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connections, transaction
from django.test import TestCase, TransactionTestCase
from accounts.models import User
from accounting.domain import post_economic_event
from merchants.models import Merchant, Membership
from settlements import domain as settlements
from settlements.models import Beneficiary, Payout
from .actions import ActionConflict, local_action
from .models import ApprovalReceipt, AuditEvent, Control, Job, Notification, Operation, Outbox


def fixtures():
    owner=User.objects.create_user(username=str(uuid.uuid4()),email='approval-owner@example.test',password='A fictional approval passphrase',verified=True,totp_confirmed=True)
    finance=User.objects.create_user(username=str(uuid.uuid4()),email='approval-finance@example.test',password='A fictional finance passphrase',verified=True,totp_confirmed=True)
    merchant=Merchant.objects.create(name='Fictional Approval Shop',status='ACTIVE')
    Membership.objects.create(user=owner,merchant=merchant,role='OWNER')
    Membership.objects.create(user=finance,merchant=merchant,role='FINANCE')
    proposal=settlements.propose_beneficiary(finance,merchant,settlements.expected_beneficiary_token(merchant))
    return owner,finance,merchant,proposal


def approve_beneficiary(owner,merchant,proposal,key,perform=None,payload=None):
    payload=payload or {'proposal_version':proposal.version,'decision':'APPROVE'}
    return local_action(owner,merchant,'BENEFICIARY_APPROVE',proposal.pk,key,payload,
        perform or (lambda actor,locked:settlements.approve_beneficiary(actor,locked,proposal.pk,payload['proposal_version'],True)),
        participants=(proposal.maker_id,),required_role='OWNER',
        load_result=lambda ref,actor,locked:Beneficiary.objects.get(pk=ref,merchant=locked))


class LocalApprovalTests(TestCase):
    def setUp(self):
        identity=patch('operations.run_identity._bound_identity',None)
        identity.start();self.addCleanup(identity.stop)
        Control.objects.create(environment_id=settings.MPRP_ENVIRONMENT_ID,dispatch_epoch=1,simulator_instance_id=str(uuid.uuid4()),restore_hold=False)
        self.owner,self.finance,self.merchant,self.proposal=fixtures()

    def test_equivalent_replay_precedes_changed_maker_authority(self):
        key=secrets.token_hex(32)
        first=approve_beneficiary(self.owner,self.merchant,self.proposal,key)
        Membership.objects.filter(user=self.finance,merchant=self.merchant).update(active=False)
        repeated=approve_beneficiary(self.owner,self.merchant,self.proposal,key)
        self.assertEqual(first.pk,repeated.pk)
        self.assertTrue(repeated._local_action_replayed)
        self.assertEqual(ApprovalReceipt.objects.count(),1)
        self.assertEqual(AuditEvent.objects.filter(action='beneficiary.approved').count(),1)
        self.assertFalse(Operation.objects.exists())

    def test_conflicting_payload_never_performs_another_approval(self):
        key=secrets.token_hex(32)
        approve_beneficiary(self.owner,self.merchant,self.proposal,key)
        with self.assertRaises(ActionConflict), patch('settlements.domain.approve_beneficiary') as operation:
            approve_beneficiary(self.owner,self.merchant,self.proposal,key,payload={'proposal_version':2,'decision':'APPROVE'})
        operation.assert_not_called()
        self.assertEqual(ApprovalReceipt.objects.count(),1)

    def test_invalid_or_failed_work_leaves_no_receipt_or_partial_approval(self):
        before=Outbox.objects.count()
        def fail_after_effect(actor,locked):
            settlements.approve_beneficiary(actor,locked,self.proposal.pk,self.proposal.version,True)
            raise ValidationError('Controlled local approval failure')
        with self.assertRaises(ValidationError):
            approve_beneficiary(self.owner,self.merchant,self.proposal,secrets.token_hex(32),perform=fail_after_effect)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status,'PROPOSED')
        self.assertFalse(ApprovalReceipt.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(action='beneficiary.approved').exists())
        self.assertEqual(Outbox.objects.count(),before)

    def test_current_actor_revocation_prevents_retained_result(self):
        key=secrets.token_hex(32)
        approve_beneficiary(self.owner,self.merchant,self.proposal,key)
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            approve_beneficiary(self.owner,self.merchant,self.proposal,key)
        self.assertEqual(ApprovalReceipt.objects.count(),1)

    def test_response_expiry_keeps_the_financial_key_tombstone(self):
        key=secrets.token_hex(32)
        approve_beneficiary(self.owner,self.merchant,self.proposal,key)
        receipt=ApprovalReceipt.objects.get()
        with patch('operations.actions._now',return_value=receipt.created_at+timedelta(days=90)):
            with self.assertRaises(ActionConflict):
                approve_beneficiary(self.owner,self.merchant,self.proposal,key)
        self.assertEqual(ApprovalReceipt.objects.count(),1)
        self.assertEqual(AuditEvent.objects.filter(action='beneficiary.approved').count(),1)

    def test_payout_http_approval_and_cancel_replays_do_not_repeat_effects(self):
        readiness=patch('operations.health.local_admission_ready',return_value=True)
        readiness.start();self.addCleanup(readiness.stop)
        Control.objects.update_or_create(pk=1,defaults={'environment_id':settings.MPRP_ENVIRONMENT_ID,'restore_hold':False,'dispatch_epoch':1})
        beneficiary=settlements.approve_beneficiary(self.owner,self.merchant,self.proposal.pk,self.proposal.version,True)
        from operations.worker import database_now
        for kind,amount in [('CAPTURE',10000),('FEE',-320),('SETTLEMENT',9680)]:
            post_economic_event(self.merchant,str(uuid.uuid4()),kind,amount,database_now())
        payout=settlements.propose_payout(self.finance,self.merchant,2000,beneficiary.version,'Fictional approved payout',secrets.token_hex(32))
        self.client.force_login(self.owner,backend='accounts.domain.EmailBackend')
        session=self.client.session
        session.update({'auth_epoch':self.owner.auth_epoch,'mfa_ok':True,'absolute_start':time.time(),'last_activity':time.time(),'step_up_at':time.time(),'step_up_reference':'test-step-up'})
        session.save()
        body={'action_key':secrets.token_hex(32),'proposal_version':payout.version,'step_up_reference':'test-step-up'}
        url=f'/m/{self.merchant.pk}/payouts/{payout.pk}/approve/'
        self.assertEqual(self.client.post(url,body).status_code,303)
        payout.refresh_from_db();version=payout.version;operation=payout.operation_id
        session=self.client.session;session['step_up_at']=0;session.save()
        self.assertEqual(self.client.post(url,body).status_code,303)
        payout.refresh_from_db()
        self.assertEqual(payout.version,version)
        self.assertEqual(payout.operation_id,operation)
        self.assertTrue(payout.reserved)
        self.assertEqual(Operation.objects.filter(kind='PAYOUT').count(),1)
        self.assertEqual(ApprovalReceipt.objects.count(),1)
        self.assertEqual(self.client.post(url,{**body,'proposal_version':version}).status_code,409)
        cancel_url=f'/m/{self.merchant.pk}/payouts/{payout.pk}/cancel/'
        cancellation={'action_key':secrets.token_hex(32),'expected_version':version}
        Control.objects.filter(pk=1).update(restore_hold=True)
        self.assertEqual(self.client.post(cancel_url,cancellation).status_code,503)
        payout.refresh_from_db()
        self.assertTrue(payout.reserved)
        self.assertEqual(payout.status,'APPROVED')
        self.assertEqual(ApprovalReceipt.objects.count(),1)
        Control.objects.filter(pk=1).update(restore_hold=False)
        self.assertEqual(self.client.post(cancel_url,cancellation).status_code,303)
        payout.refresh_from_db();cancelled_version=payout.version
        self.assertEqual(self.client.post(cancel_url,cancellation).status_code,303)
        payout.refresh_from_db()
        self.assertEqual(payout.version,cancelled_version)
        self.assertFalse(payout.reserved)
        self.assertEqual(payout.status,'CANCELLED')
        self.assertEqual(ApprovalReceipt.objects.count(),2)
        self.assertEqual(Operation.objects.filter(kind='PAYOUT').count(),1)
        self.assertEqual(self.client.post(cancel_url,{**cancellation,'expected_version':cancelled_version}).status_code,409)


class ApprovalConnectionTests(TransactionTestCase):
    def setUp(self):
        identity=patch('operations.run_identity._bound_identity',None)
        identity.start();self.addCleanup(identity.stop)
        Control.objects.create(environment_id=settings.MPRP_ENVIRONMENT_ID,dispatch_epoch=1,simulator_instance_id=str(uuid.uuid4()),restore_hold=False)
        self.owner,self.finance,self.merchant,self.proposal=fixtures()

    def test_same_key_two_connections_commit_one_receipt_and_approval(self):
        key=secrets.token_hex(32);barrier=threading.Barrier(3)
        def participant():
            close_old_connections()
            try:
                owner=User.objects.get(pk=self.owner.pk)
                merchant=Merchant.objects.get(pk=self.merchant.pk)
                proposal=Beneficiary.objects.get(pk=self.proposal.pk)
                barrier.wait(timeout=10)
                result=approve_beneficiary(owner,merchant,proposal,key)
                return result.pk,result._local_action_replayed
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures=[executor.submit(participant) for _ in range(2)]
            barrier.wait(timeout=10)
            results=[future.result(timeout=15) for future in futures]
        self.assertEqual({result[0] for result in results},{self.proposal.pk})
        self.assertEqual(sorted(result[1] for result in results),[False,True])
        self.assertEqual(ApprovalReceipt.objects.count(),1)
        self.assertEqual(AuditEvent.objects.filter(action='beneficiary.approved').count(),1)

    def test_notice_commit_does_not_take_recipient_user_lock_after_merchant(self):
        from operations.worker import database_now,run_job
        recipient=User.objects.create_user(username=str(uuid.uuid4()),email='other-owner@example.test',password='A fictional notice passphrase',verified=True,totp_confirmed=True)
        Membership.objects.create(user=recipient,merchant=self.merchant,role='OWNER')
        held=threading.Event();original=settlements.record
        def at_audit(*args,**kwargs):
            held.set()
            return original(*args,**kwargs)
        def propose():
            close_old_connections()
            try:
                finance=User.objects.get(pk=self.finance.pk)
                merchant=Merchant.objects.get(pk=self.merchant.pk)
                return settlements.propose_beneficiary(finance,merchant,settlements.expected_beneficiary_token(merchant)).pk
            finally:connections.close_all()
        with patch('settlements.domain.record',side_effect=at_audit),ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                current=User.objects.select_for_update().get(pk=recipient.pk)
                current.is_active=False;current.save(update_fields=['is_active'])
                future=executor.submit(propose)
                self.assertTrue(held.wait(10))
                # The proposal transaction owns Merchant and creates its notices.
                # A deferred recipient FK would invert against this User lock.
                Merchant.objects.select_for_update().get(pk=self.merchant.pk)
            proposal_id=future.result(timeout=15)
        self.assertTrue(Notification.objects.filter(recipient=recipient,event_key='beneficiary.proposed:'+str(proposal_id)).exists())
        out=Outbox.objects.filter(recipient=recipient.email,subject__startswith='MPRP activity:').latest('created_at')
        job=Job.objects.get(kind='notification_send',payload__outbox_id=str(out.pk))
        with patch('operations.worker.send_mail') as mail:
            run_job(job)
            mail.assert_not_called()
        out.refresh_from_db()
        self.assertEqual(out.status,'CANCELLED')
        self.assertEqual(out.encrypted_body,'')
