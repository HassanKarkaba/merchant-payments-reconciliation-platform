import uuid
from datetime import datetime,timezone as dt_timezone
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.db import IntegrityError,transaction,connection
from django.test import TransactionTestCase,SimpleTestCase
from django.utils import timezone
from merchants.models import Merchant
from .domain import post_economic_event,fee_for,balances,ensure_accounts,observe_economic_source
from .models import Journal,JournalEntry,AccountingPeriod

class FeeArithmeticTests(SimpleTestCase):
    def test_round_half_up_and_bounds(self):
        self.assertEqual(fee_for(10000),320)
        self.assertEqual(fee_for(1500),74)
        self.assertEqual(fee_for(100),33)
        self.assertEqual(fee_for(100000000),2900030)
        for value in (True,99,100000001,100.0):
            with self.assertRaises(ValidationError):fee_for(value)

class LedgerIntegrityTests(TransactionTestCase):
    def setUp(self):
        self.merchant=Merchant.objects.create(name='Fictional Ledger Co',status='ACTIVE')
        self.now=timezone.now()
    def post(self,source,kind,amount):
        return post_economic_event(self.merchant,source,kind,amount,self.now)
    def test_accounting_mapping_and_idempotent_replay(self):
        capture=self.post('capture-1','CAPTURE',10000)
        self.post('fee-1','FEE',-320)
        self.post('refund-1','REFUND',-2500)
        self.assertEqual(self.post('capture-1','CAPTURE',10000).pk,capture.pk)
        self.assertEqual(balances(self.merchant),{'processor_clearing':7180,'bank_control':0,'merchant_payable':7180,'adjustment_suspense':0})
        self.post('settlement-1','SETTLEMENT',7180)
        self.post('payout-1','PAYOUT',-7180)
        self.assertEqual(balances(self.merchant),{code:0 for code in ('processor_clearing','bank_control','merchant_payable','adjustment_suspense')})
        self.assertEqual(Journal.objects.count(),5)
    def test_database_rejects_unbalanced_journal_at_real_commit(self):
        source=observe_economic_source(self.merchant,'bad','CAPTURE',100,self.now)
        accounts=ensure_accounts(self.merchant)
        period=AccountingPeriod.objects.create(merchant=self.merchant,month=self.now.date().replace(day=1))
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                journal=Journal.objects.create(merchant=self.merchant,period=period,source=source,purpose='CAPTURE',sequence=1,occurred_at=self.now,explanation='Invalid fixture')
                JournalEntry.objects.create(journal=journal,account=accounts['processor_clearing'],side='DEBIT',amount_minor=100)
        self.assertEqual(Journal.objects.count(),0)
    def test_database_rejects_cross_merchant_accounts_and_immutable_edits(self):
        journal=self.post('capture','CAPTURE',100)
        with self.assertRaises(IntegrityError):
            Journal.objects.filter(pk=journal.pk).update(explanation='Tamper')
        with self.assertRaises(IntegrityError):
            JournalEntry.objects.filter(journal=journal).update(amount_minor=101)
        other=Merchant.objects.create(name='Fictional Other Co')
        accounts=ensure_accounts(other)
        source=observe_economic_source(self.merchant,'cross','CAPTURE',100,self.now)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                invalid=Journal.objects.create(merchant=self.merchant,period=journal.period,source=source,purpose='CAPTURE',sequence=2,occurred_at=self.now,explanation='Cross tenant fixture')
                JournalEntry.objects.bulk_create([JournalEntry(journal=invalid,account=accounts['processor_clearing'],side='DEBIT',amount_minor=100),JournalEntry(journal=invalid,account=accounts['merchant_payable'],side='CREDIT',amount_minor=100)])
    def test_mandatory_audit_failure_rolls_back_ledger_and_source(self):
        with patch('operations.audit.record',side_effect=RuntimeError('Controlled audit failure')):
            with self.assertRaises(RuntimeError):self.post('audit-fault','CAPTURE',10000)
        self.assertFalse(Journal.objects.exists())
        self.assertFalse(self.merchant.economicsource_set.exists())
    def test_late_event_posts_first_later_open_month(self):
        occurred=datetime(2026,1,31,23,59,tzinfo=dt_timezone.utc)
        AccountingPeriod.objects.create(merchant=self.merchant,month=occurred.date().replace(day=1),state='CLOSED')
        journal=post_economic_event(self.merchant,'late','CAPTURE',100,occurred)
        self.assertEqual(str(journal.period.month),'2026-02-01')
        self.assertEqual(journal.occurred_at,occurred)
        self.assertTrue(journal.late)
        self.assertTrue(self.merchant.reconciliationexception_set.filter(category='LATE_EVENT').exists())
    def test_source_batch_assignment_is_once_and_correction_is_not_automatic_money(self):
        self.post('source','CAPTURE',100)
        source=observe_economic_source(self.merchant,'source','CAPTURE',100,self.now,batch_id='batch-1')
        self.assertEqual(source.batch_id,'batch-1')
        with self.assertRaises(ValidationError):observe_economic_source(self.merchant,'source','CAPTURE',100,self.now,batch_id='batch-2')
        self.assertIsNone(self.post('correction','CORRECTION_CREDIT',500))
        self.assertEqual(Journal.objects.count(),1)
        self.assertTrue(self.merchant.reconciliationexception_set.filter(category='UNRECOGNIZED_CORRECTION').exists())

class PostingPeriodControlTests(TransactionTestCase):
    def test_later_period_journal_before_backdated_open_period_does_not_pollute_close_controls(self):
        merchant=Merchant.objects.create(name='Fictional Period Controls')
        post_economic_event(merchant,'later-settlement','SETTLEMENT',1000,datetime(2026,10,1,tzinfo=dt_timezone.utc))
        post_economic_event(merchant,'earlier-capture','CAPTURE',1000,datetime(2026,9,30,tzinfo=dt_timezone.utc))
        from datetime import date
        self.assertEqual(balances(merchant,period_month=date(2026,9,1))['bank_control'],0)
        self.assertEqual(balances(merchant,period_month=date(2026,9,1))['processor_clearing'],1000)
