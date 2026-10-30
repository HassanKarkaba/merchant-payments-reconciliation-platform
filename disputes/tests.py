import uuid
from datetime import timedelta
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.test import TransactionTestCase
from django.utils import timezone
from accounts.models import User
from merchants.models import Merchant,Membership
from payments.models import CheckoutLink,Payment
from accounting.models import Journal
from accounting.domain import balances
from .models import Dispute
from .domain import apply_dispute_snapshot,has_open_dispute,lost_principal,submit_response

class DisputeAccountingTests(TransactionTestCase):
    def setUp(self):
        self.merchant=Merchant.objects.create(name='Fictional Disputes Co',status='ACTIVE')
        self.actor=User.objects.create_user(username='dispute-finance',email='finance@example.test',password='test-only-password-123',verified=True,totp_confirmed=True)
        Membership.objects.create(merchant=self.merchant,user=self.actor,role='FINANCE')
        self.now=timezone.now();self.provider_payment=uuid.uuid4();self.provider_dispute=uuid.uuid4()
        link=CheckoutLink.objects.create(merchant=self.merchant,creator=self.actor,token_hash='a'*64,amount_minor=10000,capture_mode='AUTOMATIC',expires_at=self.now+timedelta(days=1),status='CONSUMED')
        self.payment=Payment.objects.create(merchant=self.merchant,link=link,reference='pay_demo_dispute',amount_minor=10000,captured_minor=10000,fee_minor=320,state='CAPTURED',provider_id=self.provider_payment)
    def source(self,identity,kind,amount):
        return {'source_id':identity,'event_type':kind,'amount_minor':amount,'occurred_at':self.now.isoformat(),'provider_account_id':str(self.merchant.provider_account_id),'reference':identity}
    def snapshot(self,state='OPEN',version=1):
        sources=[self.source('capture','CAPTURE',10000),self.source('fee','FEE',-320),self.source('dispute-open','DISPUTE_OPEN',-10000)]
        if state=='WON':sources.append(self.source('dispute-won','DISPUTE_WON',10000))
        return {'object_id':str(self.provider_dispute),'dispute_id':str(self.provider_dispute),'payment_id':str(self.provider_payment),'principal_minor':10000,'state':state,'provider_version':version,'opened_at':self.now.isoformat(),'deadline':(self.now+timedelta(days=7)).isoformat(),'sources':sources}
    def test_out_of_order_won_reconstructs_opening_and_posts_once(self):
        dispute=apply_dispute_snapshot(self.merchant,self.snapshot('WON',3))
        apply_dispute_snapshot(self.merchant,self.snapshot('OPEN',1))
        dispute.refresh_from_db()
        self.assertEqual(dispute.state,'WON');self.assertEqual(Journal.objects.count(),4)
        self.assertEqual(balances(self.merchant)['merchant_payable'],9680)
        self.assertFalse(has_open_dispute(payment=self.payment));self.assertEqual(lost_principal(self.payment),0)
    def test_lost_has_no_second_debit_and_blocks_refundable_principal(self):
        dispute=apply_dispute_snapshot(self.merchant,self.snapshot())
        self.assertTrue(has_open_dispute(merchant=self.merchant))
        apply_dispute_snapshot(self.merchant,self.snapshot('LOST',2))
        self.assertEqual(Journal.objects.count(),3);self.assertEqual(lost_principal(self.payment),10000)
        self.assertEqual(self.payment.refundable_minor,0)
    def test_response_rejected_at_exact_deadline_without_admission(self):
        dispute=apply_dispute_snapshot(self.merchant,self.snapshot())
        with patch('disputes.domain.timezone.now',return_value=dispute.deadline):
            with self.assertRaises(ValidationError):submit_response(self.actor,dispute,'Fictional evidence','b'*48,dispute.version)
        self.assertEqual(dispute.responses.count(),0)
    def test_terminal_outcome_cannot_be_replaced(self):
        apply_dispute_snapshot(self.merchant,self.snapshot('LOST',2))
        with self.assertRaises(ValidationError):apply_dispute_snapshot(self.merchant,self.snapshot('WON',3))
        self.assertEqual(Journal.objects.count(),3)
