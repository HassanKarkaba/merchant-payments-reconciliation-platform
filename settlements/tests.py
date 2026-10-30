import uuid
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone
from accounts.models import User
from accounting.domain import post_economic_event
from merchants.models import Merchant, Membership
from operations.models import Control
from . import domain
from .models import Beneficiary, Payout


class PayoutControlTests(TestCase):
    def setUp(self):
        from unittest.mock import patch
        readiness = patch('operations.health.local_admission_ready', return_value=True)
        readiness.start()
        self.addCleanup(readiness.stop)
        self.finance = User.objects.create_user(username='payout_finance', email='finance@example.test', password='fictional password 12345', verified=True, totp_confirmed=True)
        self.owner = User.objects.create_user(username='payout_owner', email='owner@example.test', password='fictional password 12345', verified=True, totp_confirmed=True)
        self.merchant = Merchant.objects.create(name='Fictional Shop', status='ACTIVE')
        Membership.objects.create(merchant=self.merchant, user=self.finance, role='FINANCE')
        Membership.objects.create(merchant=self.merchant, user=self.owner, role='OWNER')
        Control.objects.create(environment_id=settings.MPRP_ENVIRONMENT_ID, restore_hold=False, dispatch_epoch=1, worker_heartbeat=timezone.now())
        at = timezone.now()
        for kind, amount in [('CAPTURE', 10000), ('FEE', -320), ('SETTLEMENT', 9680)]:
            post_economic_event(self.merchant, str(uuid.uuid4()), kind, amount, at)

    def active_beneficiary(self):
        proposal = domain.propose_beneficiary(self.finance, self.merchant, domain.expected_beneficiary_token(self.merchant))
        return domain.approve_beneficiary(self.owner, self.merchant, proposal.pk, proposal.version, step_up_verified=True)

    def test_beneficiary_exact_scope_and_distinct_checker(self):
        other = Merchant.objects.create(name='Other Fictional Shop')
        with self.assertRaises(ValidationError):
            domain.propose_beneficiary(self.finance, self.merchant, domain.expected_beneficiary_token(other))
        proposal = domain.propose_beneficiary(self.owner, self.merchant, domain.expected_beneficiary_token(self.merchant))
        with self.assertRaises(PermissionDenied):
            domain.approve_beneficiary(self.owner, self.merchant, proposal.pk, proposal.version, step_up_verified=True)
        self.assertFalse(Beneficiary.objects.filter(status='ACTIVE').exists())

    def test_two_payout_approvals_cannot_overreserve_settled_funds(self):
        beneficiary = self.active_beneficiary()
        first = domain.propose_payout(self.finance, self.merchant, 7000, beneficiary.version, 'First fictional payout', uuid.uuid4())
        second = domain.propose_payout(self.finance, self.merchant, 7000, beneficiary.version, 'Second fictional payout', uuid.uuid4())
        domain.approve_payout(self.owner, self.merchant, first.pk, first.version, step_up_verified=True)
        with self.assertRaises(ValidationError):
            domain.approve_payout(self.owner, self.merchant, second.pk, second.version, step_up_verified=True)
        self.assertEqual(domain.balances_and_availability(self.merchant)['available_payout'], 2680)
        self.assertEqual(Payout.objects.filter(reserved=True).count(), 1)

    def test_unsettled_clearing_cannot_fund_payout(self):
        another = Merchant.objects.create(name='Unsettled Fictional Shop', status='ACTIVE')
        Membership.objects.create(merchant=another, user=self.finance, role='FINANCE')
        post_economic_event(another, str(uuid.uuid4()), 'CAPTURE', 10000, timezone.now())
        self.assertEqual(domain.balances_and_availability(another)['available_payout'], 0)

    def test_unsent_cancel_releases_reservation(self):
        beneficiary = self.active_beneficiary()
        payout = domain.propose_payout(self.finance, self.merchant, 9680, beneficiary.version, 'Fictional payout', uuid.uuid4())
        domain.approve_payout(self.owner, self.merchant, payout.pk, payout.version, step_up_verified=True)
        domain.cancel_payout(self.finance, self.merchant, payout.pk)
        payout.refresh_from_db()
        self.assertEqual(payout.status, 'CANCELLED')
        self.assertFalse(payout.reserved)
        self.assertEqual(domain.balances_and_availability(self.merchant)['available_payout'], 9680)

    def test_beneficiary_change_invalidates_unsent_payout(self):
        beneficiary = self.active_beneficiary()
        payout = domain.propose_payout(self.finance, self.merchant, 9680, beneficiary.version, 'Fictional payout', uuid.uuid4())
        domain.approve_payout(self.owner, self.merchant, payout.pk, payout.version, step_up_verified=True)
        replacement = domain.propose_beneficiary(self.finance, self.merchant, domain.expected_beneficiary_token(self.merchant))
        domain.approve_beneficiary(self.owner, self.merchant, replacement.pk, replacement.version, step_up_verified=True)
        payout.refresh_from_db()
        self.assertEqual(payout.status, 'NEEDS_REAPPROVAL')
        self.assertFalse(payout.reserved)

    def test_batch_first_fee_conflict_has_no_accounting_effect(self):
        from accounting.models import EconomicSource, Journal
        from .models import SettlementBatch
        batch_id = uuid.uuid4()
        payment_id = uuid.uuid4()
        now = timezone.now().isoformat()
        def source(kind, amount, batch):
            source_id = str(uuid.uuid4())
            return {'source_id': source_id, 'id': source_id, 'event_type': kind, 'amount_minor': amount, 'currency': 'USD', 'provider_account_id': str(self.merchant.provider_account_id), 'occurred_at': now, 'reference': kind.lower() + '_' + source_id, 'payment_id': str(payment_id), 'object_id': str(payment_id), 'batch_id': str(batch) if batch else None}
        snapshot = {'object_type': 'settlement', 'object_id': str(batch_id), 'batch_id': str(batch_id), 'provider_account_id': str(self.merchant.provider_account_id), 'currency': 'USD', 'net_minor': 9001, 'cutoff': now, 'state': 'COMPLETED', 'occurred_at': now, 'sources': [source('CAPTURE', 10000, batch_id), source('FEE', -999, batch_id), source('SETTLEMENT', 9001, None)]}
        before = (EconomicSource.objects.count(), Journal.objects.count())
        with self.assertRaises(ValidationError) as caught:
            domain.apply_batch(self.merchant, snapshot)
        self.assertEqual(caught.exception.code, 'fee_mismatch')
        self.assertEqual((EconomicSource.objects.count(), Journal.objects.count()), before)
        self.assertFalse(SettlementBatch.objects.exists())
