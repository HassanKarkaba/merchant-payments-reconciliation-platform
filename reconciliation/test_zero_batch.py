import uuid
from django.core.exceptions import ValidationError
from django.test import TransactionTestCase
from accounts.models import User
from merchants.models import Merchant
from accounting.models import Journal
from accounting.domain import observe_economic_source,balances
from .models import StatementLine,MatchAllocation,MatchGroup
from .domain import run_matching,allocate_match,digest
from . import tests as fixtures

class ZeroBatchEvidenceTests(TransactionTestCase):
    def test_simulator_zero_statement_matches_exact_batch_despite_legacy_reference_alias(self):
        actor=User.objects.create_user(username='zero-recon',email='zero-recon@example.test',password='Fictional zero batch phrase 1937',verified=True,totp_confirmed=True,platform_role='RECONCILIATION')
        merchant=Merchant.objects.create(name='Fictional Zero Batch Co',status='ACTIVE')
        batch=uuid.uuid4()
        source=observe_economic_source(merchant,str(batch),'BATCH_ZERO',0,fixtures.START,reference=str(batch),provider_account_id=str(merchant.provider_account_id),batch_id=str(batch))
        # Exact current simulator schema, with its human-readable batch_ alias.
        row={'schema_version':'1','line_id':str(batch),'provider_account_id':str(merchant.provider_account_id),'event_type':'BATCH_ZERO','source_id':str(batch),'batch_id':str(batch),'occurred_at':fixtures.START.isoformat(),'currency':'USD','amount_minor':'0','reference':'batch_'+batch.hex}
        line=StatementLine.objects.create(merchant=merchant,kind='PROCESSOR_TRANSFER',account_id=str(merchant.provider_account_id),line_id=row['line_id'],source_id=row['source_id'],event_type='BATCH_ZERO',reference=row['reference'],batch_id=row['batch_id'],occurred_at=fixtures.START,amount_minor=0,row_hash=digest(row),canonical_row=row)
        from settlements.domain import apply_batch
        snapshot={'provider_account_id':str(merchant.provider_account_id),'currency':'USD','batch_id':str(batch),'net_minor':0,'cutoff':'zero-fixture','state':'SUCCEEDED','sources':[],'occurred_at':fixtures.START.isoformat()}
        first=apply_batch(merchant,snapshot)
        self.assertEqual(apply_batch(merchant,snapshot).pk,first.pk)
        self.assertEqual(run_matching(actor,merchant),{'matched':1,'exceptions':0})
        self.assertEqual(run_matching(actor,merchant),{'matched':0,'exceptions':0})
        self.assertEqual(MatchGroup.objects.count(),1);self.assertEqual(MatchAllocation.objects.get().amount_minor,0);self.assertEqual(Journal.objects.count(),0)
        self.assertTrue(all(value==0 for value in balances(merchant).values()))
        source.refresh_from_db();self.assertEqual(source.reference,str(batch))
        with self.assertRaises(ValidationError):allocate_match(actor,merchant,[{'line_id':str(line.pk),'source_id':str(source.pk),'amount_minor':1}])
        wrong=StatementLine.objects.create(merchant=merchant,kind='PROCESSOR_TRANSFER',account_id=str(merchant.provider_account_id),line_id='wrong_'+uuid.uuid4().hex,source_id=str(batch),event_type='BATCH_ZERO',reference='batch_'+batch.hex,batch_id=str(uuid.uuid4()),occurred_at=fixtures.START,amount_minor=0,row_hash='0'*64,canonical_row={})
        with self.assertRaises(ValidationError):allocate_match(actor,merchant,[{'line_id':str(wrong.pk),'source_id':str(source.pk),'amount_minor':0}])
        self.assertEqual(MatchGroup.objects.count(),1);self.assertEqual(Journal.objects.count(),0)
