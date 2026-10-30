import uuid
from unittest.mock import patch
from django.test import TransactionTestCase
from accounts.models import User
from merchants.models import Merchant,Membership
from .models import Notification,Outbox,Job
from .notifications import announce
from .domain import mail

class NotificationAtomicityTests(TransactionTestCase):
    def setUp(self):
        self.actor=User.objects.create_user(username='notice-owner',email='notice-owner@example.test',password='Fictional notification phrase 9827',verified=True,totp_confirmed=True)
        self.merchant=Merchant.objects.create(name='Fictional Notification Co',status='ACTIVE')
        Membership.objects.create(merchant=self.merchant,user=self.actor,role='OWNER')
    def test_notice_outbox_and_job_admission_roll_back_together_then_retry_once(self):
        resource=uuid.uuid4()
        with patch('operations.domain.mail',side_effect=RuntimeError('Controlled outbox admission failure')):
            with self.assertRaises(RuntimeError):announce('dispute.reminder24',resource,merchant=self.merchant)
        self.assertEqual(Notification.objects.count(),0);self.assertEqual(Outbox.objects.count(),0);self.assertEqual(Job.objects.count(),0)
        announce('dispute.reminder24',resource,merchant=self.merchant)
        announce('dispute.reminder24',resource,merchant=self.merchant)
        self.assertEqual(Notification.objects.count(),1);self.assertEqual(Outbox.objects.count(),1);self.assertEqual(Job.objects.filter(kind='notification_send').count(),1)
    def test_mail_does_not_leave_an_unowned_outbox_when_job_creation_fails(self):
        with patch('operations.domain.enqueue',side_effect=RuntimeError('Controlled job admission failure')):
            with self.assertRaises(RuntimeError):mail(self.actor.email,'MPRP test notice','Fictional body',merchant=self.merchant)
        self.assertEqual(Outbox.objects.count(),0);self.assertEqual(Job.objects.count(),0)
        mail(self.actor.email,'MPRP test notice','Fictional body',merchant=self.merchant)
        self.assertEqual(Outbox.objects.count(),1);self.assertEqual(Job.objects.filter(kind='notification_send').count(),1)
