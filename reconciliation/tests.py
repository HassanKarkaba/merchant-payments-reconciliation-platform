import csv
import io
import uuid
from datetime import datetime,timedelta,timezone as dt_timezone
from unittest.mock import patch
from django.core.exceptions import ValidationError,PermissionDenied
from django.db import transaction,IntegrityError
from django.test import SimpleTestCase,TransactionTestCase
from django.utils import timezone
from accounts.models import User
from merchants.models import Merchant
from accounting.domain import post_economic_event
from accounting.models import Journal,AccountingPeriod,EconomicSource
from .models import StatementImport,StatementLine,MatchGroup,MatchAllocation,ReconciliationException
from .domain import PROCESSOR_COLUMNS,BANK_COLUMNS,parse_statement,stage_import,confirm_import,run_matching,allocate_match,update_exception,propose_adjustment,decide_adjustment,propose_close,approve_close,propose_reopen,approve_reopen

START=datetime(2026,9,1,tzinfo=dt_timezone.utc);END=datetime(2026,10,1,tzinfo=dt_timezone.utc)

def csv_bytes(rows,columns=PROCESSOR_COLUMNS):
    output=io.StringIO(newline='');writer=csv.DictWriter(output,fieldnames=columns);writer.writeheader();writer.writerows(rows);return output.getvalue().encode()

def row(account,**kw):
    result={'schema_version':'1','line_id':'line-1','provider_account_id':str(account),'event_type':'CAPTURE','source_id':'capture-1','batch_id':'batch-1','occurred_at':'2026-09-15T12:00:00Z','currency':'USD','amount_minor':'10000','reference':'capture-1'};result.update(kw);return result

class ParserBoundaryTests(SimpleTestCase):
    def parse(self,content,kind='PROCESSOR_ECONOMIC',account='account-1',count=1,net=10000):
        return parse_statement(content,kind,account,START,END,count,net,0,net)
    def test_exact_schema_direction_and_ascii_cents(self):
        rows,errors=self.parse(csv_bytes([row('account-1')]))
        self.assertFalse(errors);self.assertEqual(rows[0]['amount_minor'],'10000')
        for amount in ('+10000','1e4','100.00','１００００','-10000'):
            self.assertTrue(self.parse(csv_bytes([row('account-1',amount_minor=amount)]))[1])
        for bad in (b'PK\x03\x04z',b'%PDFz',b'\xff',b'\x00',b'"unterminated'):
            self.assertTrue(self.parse(bad)[1])
    def test_all_or_nothing_parser_detects_middle_row_and_bounds(self):
        values=[row('account-1',line_id=f'line-{i}',source_id=f'capture-{i}',reference=f'capture-{i}') for i in range(3)]
        values[1]['currency']='EUR'
        rows,errors=self.parse(csv_bytes(values),count=3,net=30000)
        self.assertTrue(errors)
        self.assertEqual(len(rows),2)
        self.assertTrue(self.parse(csv_bytes([row('account-1',reference='x'*4097)]))[1])
        self.assertTrue(self.parse(csv_bytes([row('account-1',occurred_at='2026-10-01T00:00:00Z')]))[1])
        self.assertTrue(self.parse(csv_bytes([row('wrong-account')]))[1])
    def test_empty_zero_statement_and_10000_rows(self):
        self.assertFalse(self.parse(csv_bytes([]),count=0,net=0)[1])
        rows=[row('account-1',line_id=f'line-{i}',source_id=f'cap-{i}',reference=f'cap-{i}',amount_minor='100') for i in range(10000)]
        parsed,errors=self.parse(csv_bytes(rows),count=10000,net=1000000)
        self.assertFalse(errors);self.assertEqual(len(parsed),10000)
        rows.append(row('account-1',line_id='excess',amount_minor='100'))
        self.assertTrue(self.parse(csv_bytes(rows),count=10001,net=1000100)[1])
    def test_bank_formula_text_remains_text_and_zero_bank_rejected(self):
        value={'schema_version':'1','line_id':'bank-1','bank_account_id':'bank_demo_test','booked_at':'2026-09-15T12:00:00Z','currency':'USD','amount_minor':'100','transfer_reference':'transfer-1','description':'=not-executed'}
        rows,errors=self.parse(csv_bytes([value],BANK_COLUMNS),kind='BANK',account='bank_demo_test',net=100)
        self.assertFalse(errors);self.assertEqual(rows[0]['description'],'=not-executed')
        value['amount_minor']='0';self.assertTrue(self.parse(csv_bytes([value],BANK_COLUMNS),kind='BANK',account='bank_demo_test',net=0)[1])

class EvidenceTests(TransactionTestCase):
    def setUp(self):
        self.actor=User.objects.create_user(username='recon',email='recon@example.test',password='test-only-password-123',platform_role='RECONCILIATION',verified=True,totp_confirmed=True)
        self.checker=User.objects.create_user(username='checker',email='checker@example.test',password='test-only-password-123',platform_role='FINANCE_APPROVER',verified=True,totp_confirmed=True)
        self.merchant=Merchant.objects.create(name='Fictional Statement Co',status='ACTIVE')
    def stage(self,rows=None,statement_id='statement-1',manifest_id='manifest-1',kind='PROCESSOR_ECONOMIC'):
        rows=[row(self.merchant.provider_account_id)] if rows is None else rows
        amount=sum(int(r['amount_minor']) for r in rows)
        return stage_import(self.actor,self.merchant,csv_bytes(rows,BANK_COLUMNS if kind=='BANK' else PROCESSOR_COLUMNS),kind=kind,account_id='bank_demo_'+self.merchant.provider_account_id.hex if kind=='BANK' else str(self.merchant.provider_account_id),statement_id=statement_id,manifest_id=manifest_id,period_start=START,period_end=END,opening_balance_minor=0,closing_balance_minor=amount,declared_row_count=len(rows),declared_net_minor=amount)
    def manifest(self,statement):
        return {'schema_version':1,'policy_version':'mprp_v1','statement_id':statement.statement_id,'manifest_id':statement.manifest_id,'provider_account_id':str(self.merchant.provider_account_id),'account_id':statement.account_id,'kind':statement.kind,'period_start':START.isoformat(),'period_end':END.isoformat(),'opening_balance_minor':0,'closing_balance_minor':statement.declared_net_minor,'row_count':statement.declared_row_count,'net_minor':statement.declared_net_minor,'source_high_water':1,'rows':statement.parsed_rows}
    def confirm(self,statement):
        with patch('reconciliation.domain.fetch_manifest',return_value=self.manifest(statement)):return confirm_import(self.actor,statement)
    def test_stage_has_no_lines_and_manifest_mismatch_quarantines_every_row(self):
        statement=self.stage()
        self.assertEqual(StatementLine.objects.count(),0);self.assertEqual(Journal.objects.count(),0)
        wrong=self.manifest(statement);wrong['net_minor']=9999
        with patch('reconciliation.domain.fetch_manifest',return_value=wrong):statement=confirm_import(self.actor,statement)
        self.assertEqual(statement.status,'QUARANTINED');self.assertEqual(StatementLine.objects.count(),0)
    def test_reimport_preserves_identity_and_conflicting_rows_quarantine(self):
        statement=self.confirm(self.stage())
        self.assertEqual(self.stage().pk,statement.pk)
        renamed=self.stage(statement_id='statement-2',manifest_id='manifest-2');self.confirm(renamed)
        self.assertEqual(StatementLine.objects.count(),1)
        changed=self.stage(rows=[row(self.merchant.provider_account_id,amount_minor='11000')],statement_id='statement-3',manifest_id='manifest-3')
        changed=self.confirm(changed);self.assertEqual(changed.status,'QUARANTINED')
        self.assertEqual(StatementLine.objects.get().amount_minor,10000)
    def test_import_matching_never_posts_money_and_wrong_reference_stays_exception(self):
        statement=self.confirm(self.stage())
        counts=run_matching(self.actor,self.merchant)
        self.assertEqual(counts['matched'],0);self.assertEqual(Journal.objects.count(),0)
        occurred=datetime(2026,9,15,12,tzinfo=dt_timezone.utc)
        post_economic_event(self.merchant,'capture-1','CAPTURE',10000,occurred,reference='capture-1')
        count=Journal.objects.count();self.assertEqual(run_matching(self.actor,self.merchant)['matched'],1)
        self.assertEqual(run_matching(self.actor,self.merchant)['matched'],0);self.assertEqual(Journal.objects.count(),count)
        self.assertEqual(MatchAllocation.objects.count(),1)
        self.assertFalse(ReconciliationException.objects.exclude(state='RESOLVED').exists())
    def test_capacity_and_reason_only_monetary_waiver_rejected(self):
        self.confirm(self.stage());line=StatementLine.objects.get()
        source=post_economic_event(self.merchant,'capture-1','CAPTURE',10000,datetime(2026,9,15,12,tzinfo=dt_timezone.utc),reference='capture-1').source
        allocate_match(self.actor,self.merchant,[{'line_id':str(line.pk),'source_id':str(source.pk),'amount_minor':10000}])
        with self.assertRaises(ValidationError):allocate_match(self.actor,self.merchant,[{'line_id':str(line.pk),'source_id':str(source.pk),'amount_minor':10000}])
        exception=ReconciliationException.objects.create(merchant=self.merchant,category='AMOUNT_DIFFERENCE',source_reference='difference',amount_minor=1)
        with self.assertRaises(ValidationError):update_exception(self.actor,exception,'resolve',reason='ignore',proof='arbitrary')
    def test_correction_requires_independent_approval_and_preserves_source(self):
        post_economic_event(self.merchant,'correction-1','CORRECTION_CREDIT',500,START+timedelta(days=1),reference='correction-1')
        source=EconomicSource.objects.get(source_id='correction-1');exception=ReconciliationException.objects.get(category='UNRECOGNIZED_CORRECTION')
        proposal=propose_adjustment(self.actor,exception,source,'Fictional goodwill correction')
        with self.assertRaises(PermissionDenied):decide_adjustment(self.actor,proposal,True)
        self.assertEqual(Journal.objects.count(),0)
        decide_adjustment(self.checker,proposal,True)
        decide_adjustment(self.checker,proposal,True)
        self.assertEqual(Journal.objects.count(),1);source.refresh_from_db();self.assertTrue(source.recognized)
    def test_zero_activity_close_requires_three_manifests_and_distinct_checker(self):
        period=AccountingPeriod.objects.create(merchant=self.merchant,month=START.date())
        with patch('reconciliation.domain.month_manifests',return_value=[]):
            with self.assertRaises(ValidationError):propose_close(self.actor,period)
        manifests=[]
        for kind in ['PROCESSOR_ECONOMIC','PROCESSOR_TRANSFER','BANK']:
            statement=self.stage(rows=[],statement_id='statement-'+kind,manifest_id='manifest-'+kind,kind=kind)
            if kind=='BANK':statement.account_id='bank_demo_'+self.merchant.provider_account_id.hex;statement.save(update_fields=['account_id'])
            self.confirm(statement);manifests.append(self.manifest(statement))
        with patch('reconciliation.domain.month_manifests',return_value=manifests):
            period=propose_close(self.actor,period)
            with self.assertRaises(PermissionDenied):approve_close(self.actor,period)
            period=approve_close(self.checker,period,period.version)
        self.assertEqual(period.state,'CLOSED');self.assertEqual(period.close_reports.count(),1)
        period=propose_reopen(self.actor,period,'Correct identified fictional evidence')
        period=approve_reopen(self.checker,period,period.version)
        self.assertEqual(period.state,'OPEN');self.assertIsNotNone(period.close_reports.get().invalidated_at)

class GroupAndRoleTests(TransactionTestCase):
    setUp=EvidenceTests.setUp
    stage=EvidenceTests.stage
    def test_explicit_same_batch_aggregate_and_split_transfer(self):
        occurred=datetime(2026,9,15,12,tzinfo=dt_timezone.utc)
        s1=post_economic_event(self.merchant,'part-1','CAPTURE',6000,occurred,reference='part-1',batch_id='batch-common').source
        s2=post_economic_event(self.merchant,'part-2','CAPTURE',4000,occurred,reference='part-2',batch_id='batch-common').source
        aggregate=StatementLine.objects.create(merchant=self.merchant,kind='PROCESSOR_ECONOMIC',account_id=str(self.merchant.provider_account_id),line_id='aggregate',source_id='batch-common',event_type='CAPTURE',reference='batch-common',batch_id='batch-common',occurred_at=occurred,amount_minor=10000,row_hash='a'*64,canonical_row={})
        group=allocate_match(self.actor,self.merchant,[{'line_id':str(aggregate.pk),'source_id':str(s.pk),'amount_minor':s.amount_minor} for s in [s1,s2]])
        self.assertEqual(group.allocations.count(),2)
        transfer=post_economic_event(self.merchant,'transfer','SETTLEMENT',10000,occurred,reference='transfer').source
        split=[]
        for i,amount in enumerate([2500,7500]):
            split.append(StatementLine.objects.create(merchant=self.merchant,kind='BANK',account_id='bank_demo_'+self.merchant.provider_account_id.hex,line_id=f'bank-part-{i}',source_id='transfer',event_type='BANK_TRANSFER',reference='transfer',occurred_at=occurred,amount_minor=amount,row_hash=str(i)*64,canonical_row={}))
        group=allocate_match(self.actor,self.merchant,[{'line_id':str(line.pk),'source_id':str(transfer.pk),'amount_minor':line.amount_minor} for line in split])
        self.assertEqual(group.allocations.count(),2)
    def test_revoked_analyst_rejected_from_admission_even_with_stale_user(self):
        stale=self.actor
        User.objects.filter(pk=self.actor.pk).update(platform_role='AUDITOR')
        with self.assertRaises(PermissionDenied):self.stage()
        self.assertEqual(StatementImport.objects.count(),0)

class ReconciliationViewTests(TransactionTestCase):
    setUp=EvidenceTests.setUp
    def request(self,path,user=None):
        from django.test import RequestFactory
        request=RequestFactory().get(path);request.user=user or self.actor;request.session={};return request
    def test_templates_render_for_authorized_staff_and_denied_mutation_roles(self):
        from . import views
        from .forms import StatementUploadForm
        self.assertEqual(views.imports_new(self.request('/operations/imports/new/')).status_code,200)
        self.assertEqual(views.workspace(self.request('/operations/reconciliation/')).status_code,200)
        self.assertEqual(views.periods(self.request('/operations/periods/')).status_code,200)
        self.checker.platform_role='AUDITOR';self.checker.save(update_fields=['platform_role'])
        with self.assertRaises(PermissionDenied):views.imports_new(self.request('/operations/imports/new/',self.checker))
    def test_statement_preview_template_handles_both_processor_and_bank_columns(self):
        from . import views
        statement=EvidenceTests.stage(self)
        self.assertEqual(views.import_detail(self.request('/operations/imports/'+str(statement.pk)+'/'),statement.pk).status_code,200)
        statement.kind='BANK';statement.parsed_rows=[{'schema_version':'1','line_id':'bank-row','bank_account_id':'bank_demo_example','booked_at':'2026-09-15T12:00:00Z','currency':'USD','amount_minor':'10000','transfer_reference':'transfer','description':'Simulated settlement'}];statement.save(update_fields=['kind','parsed_rows'])
        response=views.import_detail(self.request('/operations/imports/'+str(statement.pk)+'/'),statement.pk)
        self.assertContains(response,'Simulated settlement')

class MatchingQueryGrowthTests(TransactionTestCase):
    setUp=EvidenceTests.setUp
    def test_exact_matching_uses_batched_reads_and_guarded_bulk_allocations(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        occurred=START+timedelta(days=1)
        for i in range(40):
            source=post_economic_event(self.merchant,f'query-source-{i}','CAPTURE',100,occurred,reference=f'query-reference-{i}').source
            StatementLine.objects.create(merchant=self.merchant,kind='PROCESSOR_ECONOMIC',account_id=str(self.merchant.provider_account_id),line_id=f'query-line-{i}',source_id=source.source_id,event_type='CAPTURE',reference=source.reference,batch_id='batch-query',occurred_at=occurred,amount_minor=100,row_hash='0'*64,canonical_row={})
        with CaptureQueriesContext(connection) as observed:
            counts=run_matching(self.actor,self.merchant)
        self.assertEqual(counts,{'matched':40,'exceptions':0})
        # This bounds ORM round trips across40 sources, including lock/capacity
        # revalidation, bulk writes, mandatory audit, and real transaction commit.
        self.assertLess(len(observed),35)
        self.assertEqual(MatchAllocation.objects.count(),40)
