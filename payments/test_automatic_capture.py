import hashlib
import uuid
from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from accounts.models import User
from merchants.models import Merchant, Membership
from operations.models import Operation, Job
from .domain import automatic_capture_candidates, ensure_automatic_capture, fail_operation
from .models import CheckoutLink, Payment


class AutomaticCaptureEligibilityTests(TestCase):
    def setUp(self):
        self.actor=User.objects.create_user(username=str(uuid.uuid4()),email='automatic-race@example.test',password='A fictional automatic capture passphrase',verified=True,totp_confirmed=True)
        self.merchant=Merchant.objects.create(name='Fictional Automatic Shop',status='ACTIVE')
        Membership.objects.create(merchant=self.merchant,user=self.actor,role='FINANCE')

    def payment(self):
        identity=uuid.uuid4()
        link=CheckoutLink.objects.create(merchant=self.merchant,creator=self.actor,token_hash=hashlib.sha256(str(identity).encode()).hexdigest(),amount_minor=10000,capture_mode='AUTOMATIC',status='CONSUMED',expires_at=timezone.now()+timedelta(days=1))
        return Payment.objects.create(merchant=self.merchant,link=link,reference='auto_'+identity.hex,amount_minor=10000,authorized_minor=10000,state='AUTHORIZED',auto_capture_pending=True,authorization_expires_at=timezone.now()+timedelta(days=7))

    def operation(self,payment,status):
        return Operation.objects.create(merchant=self.merchant,actor=self.actor,kind='CAPTURE',resource_id=payment.pk,action_key=uuid.uuid4().hex,fingerprint='a'*64,status=status)

    def test_unknown_capture_prefix_is_excluded_before_the_fifty_row_bound(self):
        for _ in range(55):
            self.operation(self.payment(),'UNKNOWN')
        healthy=self.payment()
        self.assertEqual([payment.pk for payment in automatic_capture_candidates()], [healthy.pk])

    def test_cancelled_capture_clears_eligibility_and_does_not_restart(self):
        payment=self.payment();operation=self.operation(payment,'CANCELLED')
        fail_operation(operation,'Provably unsent cancellation',cancelled=True)
        payment.refresh_from_db()
        self.assertFalse(payment.auto_capture_pending)
        self.assertIsNone(ensure_automatic_capture(payment))
        self.assertFalse(automatic_capture_candidates().exists())
        self.assertEqual(Operation.objects.count(),1)
        self.assertFalse(Job.objects.filter(kind='financial_dispatch').exists())
