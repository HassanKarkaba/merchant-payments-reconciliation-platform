import uuid
from django.core.exceptions import ValidationError
from django.test import TransactionTestCase
from accounts.models import User
from merchants.models import Merchant
from accounting.domain import post_economic_event
from accounting.models import AccountingPeriod,Journal
from operations.models import AuditEvent
from .models import StatementLine,ReconciliationException,MatchAllocation
from .domain import allocate_match,propose_reopen,approve_reopen,update_exception,reverse_match,close_snapshot
from . import tests as fixtures

class LateEventResolutionTests(TransactionTestCase):
    def setUp(self):
        self.actor=User.objects.create_user(username='late-recon',email='late-recon@example.test',password='Fictional late review phrase 4917',verified=True,totp_confirmed=True,platform_role='RECONCILIATION')
        self.checker=User.objects.create_user(username='late-checker',email='late-checker@example.test',password='Fictional late check phrase 4917',verified=True,totp_confirmed=True,platform_role='FINANCE_APPROVER')
        self.merchant=Merchant.objects.create(name='Fictional Late Truth Co',status='ACTIVE')
        self.period=AccountingPeriod.objects.create(merchant=self.merchant,month=fixtures.START.date(),state='CLOSED')
    def late(self,kind,amount):
        journal=post_economic_event(self.merchant,'late-'+kind.lower(),kind,amount,fixtures.START,reference='late-'+kind.lower(),batch_id='batch-late')
        self.assertTrue(journal.late)
        return journal.source,ReconciliationException.objects.get(category='LATE_EVENT',source_reference=journal.source.source_id)
    def reopen(self):
        self.period=approve_reopen(self.checker,propose_reopen(self.actor,self.period,'Review identified late authoritative evidence'))
    def line(self,source,kind,amount):
        return StatementLine.objects.create(merchant=self.merchant,kind=kind,account_id='bank_demo_'+self.merchant.provider_account_id.hex if kind=='BANK' else str(self.merchant.provider_account_id),line_id='late-line-'+uuid.uuid4().hex,source_id=source.source_id,event_type='BANK_TRANSFER' if kind=='BANK' else source.event_type,reference=source.reference,batch_id='batch-late',occurred_at=source.occurred_at,amount_minor=amount,row_hash='0'*64,canonical_row={})
    def match(self,source,line):
        return allocate_match(self.actor,self.merchant,[{'line_id':str(line.pk),'source_id':str(source.pk),'amount_minor':line.amount_minor}])
    def test_missing_or_partial_transfer_layers_cannot_resolve_nonzero_late_event(self):
        source,exception=self.late('SETTLEMENT',1000)
        with self.assertRaises(ValidationError):update_exception(self.actor,exception,'resolve',reason='A note cannot waive money',proof='arbitrary caller proof')
        self.reopen();self.match(source,self.line(source,'PROCESSOR_TRANSFER',1000))
        with self.assertRaises(ValidationError):update_exception(self.actor,exception,'resolve',proof='processor evidence alone')
        self.match(source,self.line(source,'BANK',400))
        with self.assertRaises(ValidationError):update_exception(self.actor,exception,'resolve',proof='partial bank evidence')
        exception.refresh_from_db();self.assertEqual(exception.state,'OPEN');self.assertEqual(Journal.objects.count(),1)
        monetary=ReconciliationException.objects.create(merchant=self.merchant,category='AMOUNT_DIFFERENCE',source_reference='unrelated',amount_minor=1000)
        with self.assertRaises(ValidationError):update_exception(self.actor,monetary,'resolve',proof='reason-only closure')
    def test_exact_posted_evidence_resolves_and_reversal_still_blocks_cutoff_coverage(self):
        source,exception=self.late('CAPTURE',10000)
        self.reopen();group=self.match(source,self.line(source,'PROCESSOR_ECONOMIC',10000))
        exception=update_exception(self.actor,exception,'resolve',reason='Reviewed posted and matched source',proof='untrusted text is not authority')
        self.assertEqual(exception.state,'RESOLVED');self.assertEqual(Journal.objects.count(),1)
        proof=exception.history[-1]['verified_evidence'][0]
        self.assertEqual(proof['source_id'],source.source_id)
        self.assertEqual(proof['journal_ids'],[str(Journal.objects.get().pk)])
        self.assertEqual(proof['layers']['PROCESSOR_ECONOMIC']['match_group_ids'],[str(group.pk)])
        audit=AuditEvent.objects.filter(action='exception.resolve',resource_id=str(exception.pk)).first()
        self.assertEqual(audit.summary['verified_evidence'],exception.history[-1]['verified_evidence'])
        before=close_snapshot(self.period,[])
        self.assertFalse(any('through cutoff lacks complete matched evidence' in message for message in before['blockers']))
        reverse_match(self.actor,group,'Correct the evidence allocation')
        after=close_snapshot(self.period,[])
        self.assertTrue(any(source.source_id in message and 'through cutoff lacks complete matched evidence' in message for message in after['blockers']))
        exception.refresh_from_db();self.assertEqual(exception.state,'RESOLVED');self.assertEqual(Journal.objects.count(),1)
        self.assertEqual(MatchAllocation.objects.filter(group__status='MATCHED').count(),0)
