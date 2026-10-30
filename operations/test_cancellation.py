import hashlib,secrets,time,uuid
from unittest.mock import patch
from django.test import TestCase
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
from accounts.models import User
from merchants.models import Merchant,Membership
from payments.models import CheckoutLink,Payment,Refund
from .models import Operation,ApprovalReceipt,Job,Control
from .audit import canonical
from .views import operation_version
class CancellationReceiptTests(TestCase):
    def setUp(self):
        identity=patch('operations.run_identity._bound_identity',None)
        identity.start();self.addCleanup(identity.stop)
        Control.objects.create(environment_id=settings.MPRP_ENVIRONMENT_ID,restore_hold=False,dispatch_epoch=1,simulator_instance_id=str(uuid.uuid4()))
        self.user=User.objects.create(username=str(uuid.uuid4()),email='cancel@example.test',verified=True,totp_confirmed=True)
        self.merchant=Merchant.objects.create(name='Cancellation scope',status='ACTIVE');Membership.objects.create(user=self.user,merchant=self.merchant,role='FINANCE')
        link=CheckoutLink.objects.create(merchant=self.merchant,creator=self.user,token_hash='c'*64,amount_minor=10000,capture_mode='MANUAL',expires_at=timezone.now()+timedelta(hours=1))
        self.payment=Payment.objects.create(merchant=self.merchant,link=link,reference='cancel_fixture',amount_minor=10000,authorized_minor=10000,captured_minor=10000,state='CAPTURED')
        resource=uuid.uuid4();payload={'payment_id':str(self.payment.pk),'amount_minor':1000}
        self.op=Operation.objects.create(actor=self.user,merchant=self.merchant,kind='REFUND',resource_id=resource,action_key=secrets.token_hex(32),fingerprint=hashlib.sha256(canonical(payload).encode()).hexdigest(),payload=payload)
        self.refund=Refund.objects.create(id=resource,merchant=self.merchant,payment=self.payment,operation=self.op,amount_minor=1000,reason='Synthetic cancellation',reserved=True)
        self.client.force_login(self.user,backend='accounts.domain.EmailBackend');session=self.client.session;session.update({'auth_epoch':self.user.auth_epoch,'mfa_ok':True,'absolute_start':time.time(),'last_activity':time.time()});session.save()
        self.url=f'/m/{self.merchant.pk}/operations/{self.op.pk}/cancel/'
    def test_cancel_replays_before_version_advance_and_conflict_is_409(self):
        data={'action_key':secrets.token_hex(32),'expected_version':operation_version(self.op)}
        with patch('operations.provider.request',side_effect=AssertionError('Cancellation must not call provider inline')):
            self.assertEqual(self.client.post(self.url,data).status_code,303)
            self.assertEqual(self.client.post(self.url,data).status_code,303)
            self.assertEqual(self.client.post(self.url,{**data,'expected_version':99}).status_code,409)
        self.op.refresh_from_db();self.refund.refresh_from_db()
        self.assertEqual(self.op.status,'CANCELLED');self.assertFalse(self.refund.reserved);self.assertEqual(ApprovalReceipt.objects.count(),1);self.assertEqual(Job.objects.count(),0)
    def test_sent_cancellation_retains_unknown_principal_and_one_resolution(self):
        self.op.status='UNKNOWN';self.op.attempts=1;self.op.save(update_fields=['status','attempts'])
        data={'action_key':secrets.token_hex(32),'expected_version':operation_version(self.op)}
        self.assertEqual(self.client.post(self.url,data).status_code,303);self.assertEqual(self.client.post(self.url,data).status_code,303)
        self.op.refresh_from_db();self.refund.refresh_from_db()
        self.assertEqual(self.op.status,'UNKNOWN');self.assertTrue(self.op.cancellation_requested);self.assertTrue(self.refund.reserved);self.assertEqual(Job.objects.filter(kind='financial_resolve').count(),1)
