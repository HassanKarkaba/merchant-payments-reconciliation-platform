import uuid
from datetime import timedelta
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from accounts.models import User
from merchants.models import Merchant, Membership
from operations.models import Control, Operation
from . import domain
from .forms import CheckoutForm, LinkForm, parse_amount
from .models import CheckoutLink, Payment, Refund


class ExactAmountTests(SimpleTestCase):
    def test_ascii_decimal_conversion_and_rejection(self):
        for text, expected in [('1', 100), ('1.0', 100), (' 100.01 ', 10001), ('1000000.00', 100000000), ('0.01', 1)]:
            self.assertEqual(parse_amount(text), expected)
        for text in ['1e2', '-1', '+1', '1,000', '1.001', '١.٠٠', '1 00', '', 'NaN', '1000000.01']:
            with self.subTest(text=text), self.assertRaises(ValidationError):
                parse_amount(text)

    def test_unknown_fields_and_real_account_like_customer_input(self):
        form = LinkForm({'amount': '100', 'capture_mode': 'AUTOMATIC', 'expiry_minutes': '1440', 'merchant_id': str(uuid.uuid4())})
        self.assertFalse(form.is_valid())
        checkout = CheckoutForm({'attempt_key': uuid.uuid4(), 'scenario': 'tok_demo_success', 'customer_name': '4111 1111 1111 1111'})
        self.assertFalse(checkout.is_valid())


class PaymentAdmissionTests(TestCase):
    def setUp(self):
        from unittest.mock import patch
        readiness = patch('operations.health.local_admission_ready', return_value=True)
        readiness.start()
        self.addCleanup(readiness.stop)
        self.actor = User.objects.create_user(username='fictional_finance', email='finance@example.test', password='fictional password 12345', verified=True, totp_confirmed=True)
        self.merchant = Merchant.objects.create(name='Fictional Merchant', status='ACTIVE')
        Membership.objects.create(user=self.actor, merchant=self.merchant, role='FINANCE')
        Control.objects.create(environment_id=settings.MPRP_ENVIRONMENT_ID, restore_hold=False, dispatch_epoch=1, worker_heartbeat=timezone.now())

    def admitted_payment(self, state='AUTHORIZED'):
        link, _ = domain.create_link(self.actor, self.merchant, 10000, 'MANUAL')
        return Payment.objects.create(merchant=self.merchant, link=link, reference='pay_' + uuid.uuid4().hex, amount_minor=10000, authorized_minor=10000, authorization_expires_at=timezone.now() + timedelta(days=7), state=state, captured_minor=10000 if state == 'CAPTURED' else 0, fee_minor=320 if state == 'CAPTURED' else 0)

    def test_checkout_one_link_one_payment_and_scoped_replay(self):
        link, token = domain.create_link(self.actor, self.merchant, 10000, 'MANUAL')
        session = domain.get_checkout_session(link, 'browser-one')
        payment, operation = domain.confirm_checkout(token, session.pk, 'browser-one', session.attempt_key, 'tok_demo_success', 'Fictional Customer')
        same, replay = domain.confirm_checkout(token, session.pk, 'browser-one', session.attempt_key, 'tok_demo_success', 'Fictional Customer')
        self.assertEqual(payment.pk, same.pk)
        self.assertEqual(operation.pk, replay.pk)
        other = domain.get_checkout_session(link, 'browser-two')
        with self.assertRaises(ValidationError):
            domain.confirm_checkout(token, other.pk, 'browser-two', other.attempt_key, 'tok_demo_success')
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Operation.objects.count(), 1)
        link.refresh_from_db()
        self.assertEqual(link.status, 'CONSUMED')

    def test_invalid_checkout_does_not_consume_link_or_financial_key(self):
        link, token = domain.create_link(self.actor, self.merchant, 10000, 'MANUAL')
        session = domain.get_checkout_session(link, 'browser')
        with self.assertRaises(ValidationError):
            domain.confirm_checkout(token, session.pk, 'browser', session.attempt_key, 'tok_live_unsupported')
        link.refresh_from_db()
        self.assertEqual(link.status, 'ACTIVE')
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(Operation.objects.exists())
        domain.confirm_checkout(token, session.pk, 'browser', session.attempt_key, 'tok_demo_success')
        self.assertEqual(Payment.objects.count(), 1)

    def test_capture_is_final_and_replay_has_one_operation(self):
        payment = self.admitted_payment()
        key = uuid.uuid4()
        op = domain.capture(self.actor, self.merchant, payment.pk, 4000, payment.version, key)
        same = domain.capture(self.actor, self.merchant, payment.pk, 4000, payment.version, key)
        self.assertEqual(op.pk, same.pk)
        self.assertEqual(op.payload['fee_minor'], 146)
        with self.assertRaises(ValidationError):
            domain.capture(self.actor, self.merchant, payment.pk, 4000, payment.version, uuid.uuid4())
        with self.assertRaises(ValidationError):
            domain.void(self.actor, self.merchant, payment.pk, payment.version, uuid.uuid4())
        self.assertEqual(Operation.objects.count(), 1)

    def test_refund_reservations_conserve_principal_and_replay(self):
        payment = self.admitted_payment('CAPTURED')
        key = uuid.uuid4()
        item, op = domain.refund(self.actor, self.merchant, payment.pk, 6000, 'Fictional return', payment.version, key)
        replay, same = domain.refund(self.actor, self.merchant, payment.pk, 6000, 'Fictional return', payment.version, key)
        self.assertEqual((item.pk, op.pk), (replay.pk, same.pk))
        with self.assertRaises(ValidationError):
            domain.refund(self.actor, self.merchant, payment.pk, 6000, 'Second return', payment.version, uuid.uuid4())
        self.assertEqual(payment.refundable_minor, 4000)
        op.status = 'UNKNOWN'
        op.save(update_fields=['status'])
        self.assertEqual(payment.refundable_minor, 4000)
        self.assertEqual(Refund.objects.count(), 1)
        domain.fail_operation(op, 'Authoritative no-effect tombstone')
        self.assertEqual(payment.refundable_minor, 10000)

    def test_revoked_creator_cannot_dispatch_unsent_work(self):
        payment = self.admitted_payment()
        op = domain.capture(self.actor, self.merchant, payment.pk, 10000, payment.version, uuid.uuid4())
        Membership.objects.filter(user=self.actor, merchant=self.merchant).update(active=False)
        with self.assertRaises(PermissionDenied):
            domain.prepare_operation(op)
        self.assertFalse(Refund.objects.exists())
