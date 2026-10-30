"""A lease that expires during result recording cannot commit local truth."""
import copy
import secrets
import uuid
from datetime import timedelta
from unittest.mock import patch
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TransactionTestCase
from accounts.models import User
from accounting.domain import apply_economic_event
from accounting.models import Journal
from merchants.models import Merchant, Membership
from payments import domain as payments
from payments.models import CheckoutLink, Payment
from .models import Control, Inbox, Job, Operation
from .worker import claim, database_now, run_job, tick


class CompletionLeaseTests(TransactionTestCase):
    def setUp(self):
        identity=patch('operations.run_identity._bound_identity',None)
        identity.start();self.addCleanup(identity.stop)
        readiness=patch('operations.health.local_admission_ready',return_value=True)
        readiness.start();self.addCleanup(readiness.stop)
        now=database_now()
        self.control=Control.objects.create(environment_id=settings.MPRP_ENVIRONMENT_ID,dispatch_epoch=1,simulator_instance_id=str(uuid.uuid4()),restore_hold=False,provider_ready=True,provider_checked_at=now,worker_heartbeat=now)
        self.actor=User.objects.create_user(username=str(uuid.uuid4()),email='completion@example.test',password='Fictional completion lease passphrase',verified=True,totp_confirmed=True)
        self.merchant=Merchant.objects.create(name='Fictional Lease Shop',status='ACTIVE')
        Membership.objects.create(user=self.actor,merchant=self.merchant,role='FINANCE')
        link=CheckoutLink.objects.create(merchant=self.merchant,creator=self.actor,token_hash=secrets.token_hex(32),amount_minor=10000,capture_mode='MANUAL',status='CONSUMED',expires_at=now+timedelta(days=1))
        self.payment=Payment.objects.create(merchant=self.merchant,link=link,reference='lease_'+uuid.uuid4().hex,amount_minor=10000,authorized_minor=10000,captured_minor=10000,fee_minor=320,state='CAPTURED',provider_version=2,authorization_expires_at=now+timedelta(days=7))
        self.sources=[]
        for sequence,kind,amount in [(1,'CAPTURE',10000),(2,'FEE',-320)]:
            source_id=str(uuid.uuid4())
            source={'id':source_id,'source_id':source_id,'sequence':sequence,'event_type':kind,'kind':kind,'object_id':str(self.payment.pk),'payment_id':str(self.payment.pk),'provider_account_id':str(self.merchant.provider_account_id),'currency':'USD','amount_minor':amount,'occurred_at':now.isoformat(),'reference':kind.lower()+'_'+source_id,'dependencies':[] if sequence==1 else [self.sources[0]['source_id']],'batch_id':None}
            self.sources.append(source)
            apply_economic_event(self.merchant,source)
        self.refund,self.operation=payments.refund(self.actor,self.merchant,self.payment.pk,2500,'Fictional lease refund',self.payment.version,secrets.token_hex(32))
        source_id=str(uuid.uuid4())
        self.sources.append({'id':source_id,'source_id':source_id,'sequence':3,'event_type':'REFUND','kind':'REFUND','object_id':str(self.refund.pk),'payment_id':str(self.payment.pk),'provider_account_id':str(self.merchant.provider_account_id),'currency':'USD','amount_minor':-2500,'occurred_at':now.isoformat(),'reference':'refund_'+source_id,'dependencies':[self.sources[0]['source_id']],'batch_id':None})
        self.snapshot={'object_type':'payment','object_id':str(self.payment.pk),'payment_id':str(self.payment.pk),'provider_id':str(self.payment.pk),'provider_account_id':str(self.merchant.provider_account_id),'currency':'USD','authorized_minor':10000,'captured_minor':10000,'refunded_minor':2500,'fee_minor':320,'state':'CAPTURED','version':3,'provider_version':3,'authorization_expires_at':(now+timedelta(days=7)).isoformat(),'dispute_id':None,'dispute_state':None,'sources':self.sources}

    def response(self, mutation=True):
        result={'schema_version':1,'policy_version':'mprp_v1','environment_id':str(self.control.environment_id),'simulator_instance_id':self.control.simulator_instance_id,'source_high_water':3,'snapshot':copy.deepcopy(self.snapshot)}
        if mutation:result.update(operation_key=str(self.operation.provider_key),state='SUCCEEDED',reason='',payload_hash=self.operation.fingerprint)
        return result

    def test_expiry_after_posting_rolls_back_and_next_resolver_posts_once(self):
        original=payments.complete_operation
        def expire_after_posting(operation,snapshot):
            result=original(operation,snapshot)
            Job.objects.filter(payload__operation_id=str(operation.pk),status='CLAIMED').update(lease_until=database_now()-timedelta(microseconds=1))
            return result
        with patch('operations.worker.provider.request',return_value=self.response()),patch('payments.domain.complete_operation',side_effect=expire_after_posting):
            self.assertTrue(tick(uuid.uuid4()))
        self.operation.refresh_from_db();self.refund.refresh_from_db();self.payment.refresh_from_db()
        self.assertEqual(self.operation.status,'UNKNOWN')
        self.assertTrue(self.refund.reserved)
        self.assertEqual(self.refund.status,'QUEUED')
        self.assertEqual(self.payment.refunded_minor,0)
        self.assertEqual(Journal.objects.filter(purpose='REFUND').count(),0)
        job=Job.objects.get(payload__operation_id=str(self.operation.pk))
        self.assertEqual(job.kind,'financial_resolve')
        self.assertEqual(job.status,'READY')
        Job.objects.filter(pk=job.pk).update(due_at=database_now()-timedelta(microseconds=1))
        with patch('operations.worker.provider.request',return_value=self.response()) as request:
            self.assertTrue(tick(uuid.uuid4()))
            self.assertTrue(any(call.args[1]=='/v1/resolve-key/' for call in request.call_args_list))
        self.operation.refresh_from_db();self.refund.refresh_from_db();self.payment.refresh_from_db()
        self.assertEqual(self.operation.status,'SUCCEEDED')
        self.assertFalse(self.refund.reserved)
        self.assertEqual(self.refund.status,'SUCCEEDED')
        self.assertEqual(self.payment.refunded_minor,2500)
        self.assertEqual(Journal.objects.filter(purpose='REFUND').count(),1)

    def test_callback_expiry_rolls_back_inbox_and_economic_posting(self):
        # Observe the provider independently while the original operation is
        # awaiting lookup; only the due callback may run in this case.
        Operation.objects.filter(pk=self.operation.pk).update(status='UNKNOWN')
        Job.objects.filter(payload__operation_id=str(self.operation.pk)).update(status='BLOCKED')
        event_id=str(uuid.uuid4())
        Inbox.objects.create(event_id=event_id,payload_hash='a'*64,payload={'provider_account_id':str(self.merchant.provider_account_id),'object_id':str(self.payment.pk),'object_type':'payment'})
        item=Job.objects.create(kind='callback_apply',payload={'event_id':event_id})
        job=claim(uuid.uuid4(),lane='recovery')
        self.assertEqual(job.pk,item.pk)
        original=payments.apply_snapshot
        def expire_after_posting(merchant,snapshot,actor=None):
            result=original(merchant,snapshot,actor=actor)
            Job.objects.filter(pk=job.pk).update(lease_until=database_now()-timedelta(microseconds=1))
            return result
        with patch('operations.worker.provider.request',return_value=self.response(False)),patch('payments.domain.apply_snapshot',side_effect=expire_after_posting):
            with self.assertRaises(ValidationError):run_job(job)
        self.payment.refresh_from_db();self.refund.refresh_from_db()
        self.assertEqual(self.payment.refunded_minor,0)
        self.assertTrue(self.refund.reserved)
        self.assertEqual(Inbox.objects.get(pk=event_id).status,'PENDING')
        self.assertEqual(Journal.objects.filter(purpose='REFUND').count(),0)
        with patch('operations.worker.provider.request',return_value=self.response(False)):
            run_job(job)
            run_job(job)
        self.assertEqual(Inbox.objects.get(pk=event_id).status,'DONE')
        self.assertEqual(Journal.objects.filter(purpose='REFUND').count(),1)
