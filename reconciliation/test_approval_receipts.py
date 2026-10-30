import secrets
import uuid
from django.conf import settings
from django.utils import timezone
import time
from unittest.mock import patch
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test import TransactionTestCase,RequestFactory
from accounting.models import AccountingPeriod,EconomicSource,Journal,CloseReport
from accounting.domain import post_economic_event
from operations.models import ApprovalReceipt,Control
from . import domain,views
from .models import ReconciliationException
from . import tests as evidence_fixtures
START=evidence_fixtures.START

class ApprovalReceiptBoundaryTests(TransactionTestCase):
    def setUp(self):
        evidence_fixtures.EvidenceTests.setUp(self)
        fresh_process=patch('operations.run_identity._bound_identity',None)
        fresh_process.start();self.addCleanup(fresh_process.stop)
        Control.objects.create(environment_id=settings.MPRP_ENVIRONMENT_ID,dispatch_epoch=1,simulator_instance_id=str(uuid.uuid4()),restore_hold=False,provider_ready=True,provider_checked_at=timezone.now(),worker_heartbeat=timezone.now())
    def request(self,data,step_up=True):
        request=RequestFactory().post('/operations/financial-approval/',data)
        request.user=self.checker;request.session={'auth_epoch':self.checker.auth_epoch}
        if step_up:request.session['step_up_at']=time.time()
        request._messages=FallbackStorage(request)
        return request
    def proposal(self):
        post_economic_event(self.merchant,'receipt-correction','CORRECTION_CREDIT',500,START,reference='receipt-correction')
        source=EconomicSource.objects.get(source_id='receipt-correction')
        exception=ReconciliationException.objects.get(category='UNRECOGNIZED_CORRECTION')
        return domain.propose_adjustment(self.actor,exception,source,'Fictional independently reviewed correction')
    def body(self,resource,key=None):
        return {'expected_version':resource.version,'proposal_hash':getattr(resource,'payload_hash',None) or resource.proposal_hash,'action_key':key or secrets.token_hex(24)}
    def test_adjustment_replay_survives_version_change_without_new_step_up_or_posting(self):
        proposal=self.proposal();body=self.body(proposal)
        self.assertEqual(views.adjustment_decide(self.request(body),proposal.pk,'approve').status_code,302)
        proposal.refresh_from_db();self.assertEqual(proposal.state,'APPROVED')
        with patch('reconciliation.views.require_step_up',side_effect=AssertionError('A retained result is not a new approval.')):
            self.assertEqual(views.adjustment_decide(self.request(body,step_up=False),proposal.pk,'approve').status_code,302)
        self.assertEqual(ApprovalReceipt.objects.count(),1);self.assertEqual(Journal.objects.count(),1)
        self.assertEqual(views.adjustment_decide(self.request(body),proposal.pk,'reject').status_code,409)
        self.assertEqual(ApprovalReceipt.objects.count(),1);self.assertEqual(Journal.objects.count(),1)
        # A stale request object cannot expose the receipt after current authority changes.
        from accounts.models import User
        User.objects.filter(pk=self.checker.pk).update(platform_role='AUDITOR')
        with self.assertRaises(PermissionDenied):views.adjustment_decide(self.request(body),proposal.pk,'approve')
    def test_unadmitted_hash_error_does_not_consume_key(self):
        proposal=self.proposal();body=self.body(proposal);invalid={**body,'proposal_hash':'0'*64}
        self.assertEqual(views.adjustment_decide(self.request(invalid),proposal.pk,'approve').status_code,409)
        self.assertEqual(ApprovalReceipt.objects.count(),0);self.assertEqual(Journal.objects.count(),0)
        self.assertEqual(views.adjustment_decide(self.request(body),proposal.pk,'approve').status_code,302)
        self.assertEqual(ApprovalReceipt.objects.count(),1);self.assertEqual(Journal.objects.count(),1)
    def test_close_and_reopen_replay_frozen_context_after_current_hash_is_cleared(self):
        period=AccountingPeriod.objects.create(merchant=self.merchant,month=START.date())
        manifests=[]
        for kind in ['PROCESSOR_ECONOMIC','PROCESSOR_TRANSFER','BANK']:
            statement=evidence_fixtures.EvidenceTests.stage(self,rows=[],statement_id='approval-'+kind,manifest_id='approval-manifest-'+kind,kind=kind)
            manifest=evidence_fixtures.EvidenceTests.manifest(self,statement)
            with patch('reconciliation.domain.fetch_manifest',return_value=manifest):domain.confirm_import(self.actor,statement)
            manifests.append(manifest)
        with patch('reconciliation.domain.month_manifests',return_value=manifests):period=domain.propose_close(self.actor,period)
        close_body=self.body(period)
        def outside_locks(*args,**kwargs):
            self.assertFalse(connection.in_atomic_block,'Provider HTTP must not occur under local approval locks.')
            return manifests
        with patch('reconciliation.domain.month_manifests',side_effect=outside_locks):
            self.assertEqual(views.period_action(self.request(close_body),period.pk,'approve-close').status_code,302)
        period.refresh_from_db();self.assertEqual(period.state,'CLOSED');self.assertEqual(CloseReport.objects.count(),1)
        period=domain.propose_reopen(self.actor,period,'Correct identified fictional evidence')
        reopen_body=self.body(period)
        self.assertEqual(views.period_action(self.request(reopen_body),period.pk,'approve-reopen').status_code,302)
        period.refresh_from_db();self.assertEqual(period.state,'OPEN');self.assertEqual(period.proposal_hash,'')
        with patch('reconciliation.views.require_step_up',side_effect=AssertionError('Replay must not consume new step-up.')),patch('reconciliation.domain.month_manifests',side_effect=AssertionError('A retained result needs no provider lookup.')):
            self.assertEqual(views.period_action(self.request(reopen_body,step_up=False),period.pk,'approve-reopen').status_code,302)
            self.assertEqual(views.period_action(self.request(close_body,step_up=False),period.pk,'approve-close').status_code,302)
        period.refresh_from_db();self.assertEqual(period.state,'OPEN');self.assertEqual(ApprovalReceipt.objects.count(),2);self.assertEqual(CloseReport.objects.count(),1)
        self.assertIsNotNone(CloseReport.objects.get().invalidated_at)

    def test_missing_integrity_guard_blocks_new_admission_but_allows_authorized_receipt_read(self):
        proposal=self.proposal();body=self.body(proposal)
        self.assertEqual(views.adjustment_decide(self.request(body),proposal.pk,'approve').status_code,302)
        with connection.cursor() as cursor:cursor.execute('ALTER TABLE operations_approvalreceipt DISABLE TRIGGER approval_receipt_immutable')
        try:
            self.assertEqual(views.adjustment_decide(self.request(body,step_up=False),proposal.pk,'approve').status_code,302)
            new={**body,'action_key':secrets.token_hex(24)}
            self.assertEqual(views.adjustment_decide(self.request(new),proposal.pk,'approve').status_code,503)
            self.assertEqual(ApprovalReceipt.objects.count(),1);self.assertEqual(Journal.objects.count(),1)
        finally:
            with connection.cursor() as cursor:cursor.execute('ALTER TABLE operations_approvalreceipt ENABLE TRIGGER approval_receipt_immutable')
