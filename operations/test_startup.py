"""Startup inventory on real PostgreSQL; provider inventory is an explicit substitute."""
import hashlib
import uuid
from datetime import timedelta
from unittest.mock import patch
from django.conf import settings
from django.test import TestCase
from django.utils import timezone
from accounts.models import User
from merchants.models import Merchant,Membership
from payments.models import CheckoutLink,Payment,Refund
from accounting.domain import post_economic_event
from operations.models import Control,Operation,Job,AuditEvent
from operations.audit import canonical
from operations import provider,recovery


class StartupInventoryTests(TestCase):
    def setUp(self):
        self.identity_patch=patch('operations.run_identity._bound_identity',None)
        self.identity_patch.start();self.addCleanup(self.identity_patch.stop)
        self.control=Control.objects.create(environment_id=settings.MPRP_ENVIRONMENT_ID,simulator_instance_id=str(uuid.uuid4()),dispatch_epoch=3,restore_hold=False,hold_reason='',provider_ready=True,worker_heartbeat=timezone.now())
        self.merchant=Merchant.objects.create(name='Startup fixture',status='ACTIVE')
        self.actor=User.objects.create_user(username='startup_actor',email='startup@example.test',verified=True,totp_confirmed=True)
        Membership.objects.create(merchant=self.merchant,user=self.actor,role='FINANCE')
        self.items=[];self.source_high=0;self.event_high=0;self.handshakes=[]

    def remote(self,method,path,payload=None,**kwargs):
        if path=='/v1/control/handshake/':
            current=Control.objects.get(pk=1)
            self.assertTrue(current.restore_hold,'The durable hold must precede every HTTP handshake')
            self.assertFalse(current.provider_ready)
            self.handshakes.append(payload['run_token'])
            return {'schema_version':1,'policy_version':'mprp_v1','currency':'USD','sandbox':True,'environment_id':str(self.control.environment_id),'simulator_instance_id':self.control.simulator_instance_id,'dispatch_epoch':4,'acknowledged_cursor':0,'event_high_water':self.event_high,'source_high_water':self.source_high,'restored':False,'resolve_key':True}
        if path.startswith('/v1/inventory/?'):
            return {'schema_version':1,'snapshot':timezone.now().isoformat(),'operations':self.items,'next_after':None,'event_high_water':self.event_high,'source_high_water':self.source_high,'simulator_instance_id':self.control.simulator_instance_id,'environment_id':str(self.control.environment_id)}
        raise AssertionError('Unexpected substituted provider path '+path)

    def item(self,kind,payload,key=None):
        return {'operation_key':str(key or uuid.uuid4()),'kind':kind,'provider_account_id':str(self.merchant.provider_account_id),'object_id':payload.get('payment_id'),'state':'SUCCEEDED','payload_hash':hashlib.sha256(canonical(payload).encode()).hexdigest(),'payload':payload,'created_at':timezone.now().isoformat()}

    def captured_fixture(self):
        link=CheckoutLink.objects.create(merchant=self.merchant,creator=self.actor,token_hash='a'*64,amount_minor=10000,capture_mode='MANUAL',expires_at=timezone.now()+timedelta(days=1),status='CONSUMED')
        payment=Payment.objects.create(merchant=self.merchant,link=link,reference='startup_payment',amount_minor=10000,authorized_minor=10000,captured_minor=10000,fee_minor=320,state='CAPTURED')
        for kind,payload in [('authorizations',{'payment_id':str(payment.pk),'amount_minor':10000,'currency':'USD','scenario':'tok_demo_success'}),('captures',{'payment_id':str(payment.pk),'amount_minor':10000,'fee_minor':320,'currency':'USD'})]:
            item=self.item(kind,payload);self.items.append(item)
            Operation.objects.create(merchant=self.merchant,actor=self.actor,kind=recovery.PROVIDER_KINDS[kind],resource_id=payment.pk,action_key=uuid.uuid4().hex,fingerprint=item['payload_hash'],provider_key=item['operation_key'],payload=payload,status='SUCCEEDED')
        for sequence,(kind,amount) in enumerate([('CAPTURE',10000),('FEE',-320),('SETTLEMENT',9680)],1):
            post_economic_event(self.merchant,str(uuid.uuid4()),kind,amount,timezone.now(),sequence=sequence)
        self.source_high=4;self.event_high=4
        payload={'payment_id':str(payment.pk),'refund_id':str(uuid.uuid4()),'amount_minor':2500,'reason':'Startup rollback fixture','currency':'USD','provider_account_id':str(self.merchant.provider_account_id)}
        item=self.item('refunds',payload);self.items.append(item)
        return payment,item

    def test_missing_refund_admission_holds_even_when_received_cursor_equals_ack(self):
        payment,item=self.captured_fixture()
        original_token=self.control.run_token
        with patch('operations.provider.request',side_effect=self.remote):
            result=recovery.prepare_run()
        self.assertTrue(result.restore_hold)
        self.assertIn('key inventory',result.hold_reason)
        self.assertEqual(result.received_cursor,0)
        self.assertNotEqual(result.run_token,original_token)
        self.assertFalse(Operation.objects.filter(provider_key=item['operation_key']).exists())
        self.assertFalse(Refund.objects.filter(payment=payment).exists())
        self.assertEqual(AuditEvent.objects.filter(action='recovery.hold').count(),1)

    def test_known_queued_refund_is_owned_backlog_and_retains_reservation(self):
        payment,item=self.captured_fixture()
        operation=Operation.objects.create(merchant=self.merchant,actor=self.actor,kind='REFUND',resource_id=payment.pk,action_key=uuid.uuid4().hex,provider_key=item['operation_key'],fingerprint=item['payload_hash'],payload=item['payload'])
        refund=Refund.objects.create(id=item['payload']['refund_id'],merchant=self.merchant,payment=payment,operation=operation,amount_minor=2500,reason='Retained principal',reserved=True)
        with patch('operations.provider.request',side_effect=self.remote):result=recovery.prepare_run()
        operation.refresh_from_db();refund.refresh_from_db()
        self.assertFalse(result.restore_hold)
        self.assertEqual(operation.status,'UNKNOWN')
        self.assertTrue(refund.reserved)
        self.assertEqual(str(operation.provider_key),item['operation_key'])
        self.assertTrue(Job.objects.filter(kind='financial_resolve',payload__operation_id=str(operation.pk)).exists())

    def test_known_unknown_key_does_not_create_false_global_hold(self):
        payment,item=self.captured_fixture()
        operation=Operation.objects.create(merchant=self.merchant,actor=self.actor,kind='REFUND',resource_id=payment.pk,action_key=uuid.uuid4().hex,provider_key=item['operation_key'],fingerprint=item['payload_hash'],payload=item['payload'],status='UNKNOWN')
        with patch('operations.provider.request',side_effect=self.remote):result=recovery.prepare_run()
        self.assertFalse(result.restore_hold)
        operation.refresh_from_db();self.assertEqual(operation.status,'UNKNOWN')

    def test_explicit_operator_hold_survives_clean_inventory(self):
        Control.objects.filter(pk=1).update(restore_hold=True,hold_reason='Explicit operator inventory review')
        with patch('operations.provider.request',side_effect=self.remote):result=recovery.prepare_run()
        self.assertTrue(result.restore_hold)
        self.assertEqual(result.hold_reason,'Explicit operator inventory review')

    def test_failed_handshake_keeps_hold_and_retry_reuses_the_same_token(self):
        with patch('operations.provider.request',side_effect=provider.ProviderUncertain('Controlled unavailable handshake')):
            with self.assertRaises(provider.ProviderError):recovery.prepare_run()
        self.control.refresh_from_db();token=self.control.run_token
        self.assertTrue(self.control.restore_hold)
        self.assertEqual(self.control.hold_reason,recovery.STARTUP_HOLD)
        with patch('operations.provider.request',side_effect=self.remote):result=recovery.prepare_run()
        self.assertFalse(result.restore_hold)
        self.assertEqual(result.run_token,token)
