"""Provider invariants exercised against its independent PostgreSQL schema."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from django.conf import settings
from django.db import close_old_connections, transaction, connection, DatabaseError
from django.test import TransactionTestCase
from . import domain
from .auth import signed_headers
from .models import (Control, ProviderAccount, ProviderPayment, ProviderOperation,
                     EconomicSource, ProviderDispute, BankTransfer, SettlementBatch)


class ProviderSemanticsTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.environment = str(settings.MPRP_ENVIRONMENT_ID)
        self.main_instance = str(uuid.uuid4())
        self.run_token = str(uuid.uuid4())
        self.epoch = domain.handshake({'environment_id': self.environment, 'main_instance_id': self.main_instance, 'run_token': self.run_token})['dispatch_epoch']
        self.account_id = uuid.uuid4()
        domain.mutate('accounts', self.envelope({'merchant_id': str(uuid.uuid4())}))

    def envelope(self, payload, key=None, epoch=None):
        return {'schema_version': 1, 'policy_version': 'mprp_v1', 'environment_id': self.environment,
                'main_instance_id': self.main_instance, 'dispatch_epoch': self.epoch if epoch is None else epoch,
                'operation_key': str(key or uuid.uuid4()), 'provider_account_id': str(self.account_id),
                'payload_hash': domain.digest(payload), 'payload': payload}

    def authorize(self, amount=10000, scenario='tok_demo_success'):
        payment_id = uuid.uuid4()
        result = domain.mutate('authorizations', self.envelope({'payment_id': str(payment_id), 'amount_minor': amount, 'currency': 'USD', 'scenario': scenario}))
        return payment_id, result

    def capture(self, payment_id, amount=10000):
        return domain.mutate('captures', self.envelope({'payment_id': str(payment_id), 'amount_minor': amount, 'fee_minor': (29*amount+500)//1000+30, 'currency': 'USD'}))

    def test_capture_refund_and_signed_settlement_conserve_cents(self):
        payment_id, _ = self.authorize()
        captured = self.capture(payment_id)['snapshot']
        self.assertEqual(captured['fee_minor'], 320)
        domain.mutate('refunds', self.envelope({'payment_id': str(payment_id), 'amount_minor': 2500, 'reason': 'Fictional return'}))
        batch = domain.mutate('settlements/flush', self.envelope({}))['snapshot']
        self.assertEqual(batch['net_minor'], 7180)
        self.assertEqual(ProviderAccount.objects.get(pk=self.account_id).bank_balance_minor, 7180)
        self.assertEqual(list(EconomicSource.objects.filter(payment_id=payment_id).values_list('amount_minor', flat=True)), [10000, -320, -2500])
        self.assertEqual(BankTransfer.objects.get(batch_id=batch['batch_id']).reference, batch['transfer_reference'])
        second = domain.mutate('settlements/flush', self.envelope({}))['snapshot']
        self.assertEqual(second['net_minor'], 0)
        self.assertEqual(BankTransfer.objects.count(), 1)

    def test_partial_capture_is_final_and_fee_is_not_refunded(self):
        payment_id, _ = self.authorize()
        self.assertEqual(self.capture(payment_id, 4000)['snapshot']['fee_minor'], 146)
        second = self.capture(payment_id, 1000)
        self.assertEqual(second['state'], 'FAILED')
        domain.mutate('refunds', self.envelope({'payment_id': str(payment_id), 'amount_minor': 4000, 'reason': 'Full principal refund'}))
        self.assertEqual(sum(EconomicSource.objects.filter(payment_id=payment_id).values_list('amount_minor', flat=True)), -146)

    def test_expiry_equality_rejects_capture(self):
        payment_id, _ = self.authorize()
        ProviderPayment.objects.filter(pk=payment_id).update(authorization_expires_at=domain.now())
        result = self.capture(payment_id)
        self.assertEqual(result['state'], 'FAILED')
        self.assertFalse(EconomicSource.objects.exists())
        self.assertEqual(ProviderPayment.objects.get(pk=payment_id).state, 'EXPIRED')

    def test_duplicate_key_and_conflicting_payload(self):
        payment_id, _ = self.authorize()
        envelope = self.envelope({'payment_id': str(payment_id), 'amount_minor': 10000})
        first = domain.mutate('captures', envelope)
        self.assertEqual(domain.mutate('captures', envelope), first)
        self.assertEqual(EconomicSource.objects.count(), 2)
        conflict = dict(envelope, payload={'payment_id': str(payment_id), 'amount_minor': 5000})
        conflict['payload_hash'] = domain.digest(conflict['payload'])
        with self.assertRaises(domain.ProviderError):
            domain.mutate('captures', conflict)
        self.assertEqual(EconomicSource.objects.count(), 2)

    def test_terminal_absence_prevents_late_dispatch(self):
        payment_id = uuid.uuid4()
        key = str(uuid.uuid4())
        resolved = domain.resolve_key(self.envelope({}, key))
        self.assertEqual(resolved['state'], 'NO_EFFECT')
        late = domain.mutate('authorizations', self.envelope({'payment_id': str(payment_id), 'amount_minor': 10000, 'currency': 'USD', 'scenario': 'tok_demo_success'}, key))
        self.assertEqual(late['state'], 'NO_EFFECT')
        self.assertFalse(ProviderPayment.objects.filter(pk=payment_id).exists())

    def test_epoch_replacement_permanently_fences_old_run(self):
        old_epoch = self.epoch
        replacement = domain.handshake({'environment_id': self.environment, 'main_instance_id': self.main_instance, 'run_token': str(uuid.uuid4())})
        self.assertGreater(replacement['dispatch_epoch'], old_epoch)
        with self.assertRaises(domain.ProviderError):
            domain.mutate('settlements/flush', self.envelope({}, epoch=old_epoch))
        with self.assertRaises(domain.ProviderError):
            domain.handshake({'environment_id': self.environment, 'main_instance_id': self.main_instance, 'run_token': self.run_token})

    def test_two_concurrent_capture_keys_have_one_final_effect(self):
        payment_id, _ = self.authorize()
        barrier = Barrier(2)
        def capture_in_connection():
            close_old_connections()
            try:
                data = self.envelope({'payment_id': str(payment_id), 'amount_minor': 10000})
                barrier.wait(timeout=5)
                return domain.mutate('captures', data)['state']
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: capture_in_connection(), range(2)))
        self.assertCountEqual(results, ['SUCCEEDED', 'FAILED'])
        self.assertEqual(EconomicSource.objects.count(), 2)
        self.assertEqual(ProviderPayment.objects.get(pk=payment_id).captured_minor, 10000)

    def test_refund_dispute_order_and_won_reversal(self):
        payment_id, _ = self.authorize()
        self.capture(payment_id)
        domain.mutate('refunds', self.envelope({'payment_id': str(payment_id), 'amount_minor': 4000, 'reason': 'Fictional refund'}))
        opened = domain.mutate('disputes/open', self.envelope({'payment_id': str(payment_id), 'outcome': 'demo_won'}))['snapshot']
        self.assertEqual(opened['principal_minor'], 6000)
        blocked = domain.mutate('refunds', self.envelope({'payment_id': str(payment_id), 'amount_minor': 1, 'reason': 'Blocked during dispute'}))
        self.assertEqual(blocked['state'], 'FAILED')
        domain.mutate('disputes/respond', self.envelope({'dispute_id': opened['dispute_id'], 'text': 'Synthetic receipt reviewed'}))
        ProviderDispute.objects.filter(pk=opened['dispute_id']).update(decision_at=domain.now()-timedelta(seconds=1))
        domain.progress_disputes()
        domain.progress_disputes()
        self.assertEqual(ProviderDispute.objects.get(pk=opened['dispute_id']).state, 'WON')
        self.assertEqual(EconomicSource.objects.filter(event_type='DISPUTE_WON').count(), 1)
        self.assertEqual(EconomicSource.objects.get(event_type='DISPUTE_WON').amount_minor, 6000)

    def test_payout_uses_settled_bank_and_same_account_token(self):
        payment_id, _ = self.authorize()
        self.capture(payment_id)
        account = ProviderAccount.objects.get(pk=self.account_id)
        payout = {'payout_id': str(uuid.uuid4()), 'amount_minor': 9680, 'beneficiary_token': account.beneficiary_token, 'currency': 'USD'}
        self.assertEqual(domain.mutate('payouts', self.envelope(payout))['state'], 'FAILED')
        domain.mutate('settlements/flush', self.envelope({}))
        paid = domain.mutate('payouts', self.envelope(payout))
        self.assertEqual(paid['state'], 'SUCCEEDED')
        self.assertEqual(ProviderAccount.objects.get(pk=self.account_id).bank_balance_minor, 0)
        self.assertEqual(BankTransfer.objects.get(kind='PAYOUT').reference, paid['snapshot']['sources'][0]['reference'])

    def test_immutable_manifest_matches_exact_csv_and_zero_activity(self):
        start = domain.now()-timedelta(hours=1)
        empty = domain.statement(self.account_id, 'PROCESSOR_ECONOMIC', domain.iso(start), domain.iso(domain.now()))
        self.assertEqual(empty.manifest['rows'], [])
        self.assertEqual(len(bytes(empty.csv_content).decode().splitlines()), 1)
        payment_id, _ = self.authorize()
        self.capture(payment_id)
        with self.assertRaises(domain.ProviderError):
            domain.statement(self.account_id, 'PROCESSOR_ECONOMIC', domain.iso(start), domain.iso(domain.now()))
        # Unbatched earlier activity also invalidates an opening accumulator,
        # even when this narrow later window would contain no detail rows.
        after_activity=domain.now()
        with self.assertRaises(domain.ProviderError):
            domain.statement(self.account_id,'PROCESSOR_ECONOMIC',domain.iso(after_activity),domain.iso(domain.now()))
        domain.mutate('settlements/flush', self.envelope({}))
        end = domain.now()
        manifest = domain.statement(self.account_id, 'PROCESSOR_ECONOMIC', domain.iso(start), domain.iso(end))
        self.assertEqual(manifest.manifest['net_minor'], 9680)
        self.assertTrue(all(isinstance(value, str) for row in manifest.manifest['rows'] for value in row.values()))
        again = domain.statement(self.account_id, 'PROCESSOR_ECONOMIC', domain.iso(start), domain.iso(end))
        self.assertEqual(again.id, manifest.id)
        self.assertEqual(bytes(again.csv_content), bytes(manifest.csv_content))

    def test_signed_transport_replay_and_query_tamper(self):
        path = '/v1/events/?after=0'
        headers = signed_headers('GET', path, b'', settings.SIMULATOR_API_KEY)
        first = self.client.get(path, headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.client.get(path, headers=headers).status_code, 401)
        fresh = signed_headers('GET', path, b'', settings.SIMULATOR_API_KEY)
        self.assertEqual(self.client.get('/v1/events/?after=1', headers=fresh).status_code, 401)

    def test_provider_terminal_records_reject_direct_sql_rewrite(self):
        payment_id, _ = self.authorize()
        self.capture(payment_id)
        source = EconomicSource.objects.first()
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute('UPDATE processor_sim_economicsource SET amount_minor=1 WHERE id=%s', [source.id])
        source.refresh_from_db()
        self.assertEqual(source.amount_minor, 10000)
        operation = ProviderOperation.objects.filter(kind='captures').first()
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("UPDATE processor_sim_provideroperation SET result='{}'::jsonb WHERE key=%s", [operation.key])

    def test_retirement_and_scoped_purge_leave_permanent_namespace_receipt(self):
        from .models import RetiredNamespace
        merchant_id=ProviderAccount.objects.get(pk=self.account_id).merchant_id
        retired=domain.mutate('accounts/retire',self.envelope({'merchant_closed_at':domain.iso(domain.now()-timedelta(days=366)), 'approval_id':str(uuid.uuid4()),'inventory_hash':'a'*64,'reason':'Expired fictional tenant retained-data purge'}))
        self.assertTrue(retired['snapshot']['retired'])
        self.assertTrue(ProviderAccount.objects.get(pk=self.account_id).retired)
        with self.assertRaises(domain.ProviderError):
            domain.mutate('authorizations',self.envelope({'payment_id':str(uuid.uuid4()),'amount_minor':100,'currency':'USD','scenario':'tok_demo_success'}))
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute('SELECT public.sim_purge_retired_namespace(%s)',[self.account_id])
        with connection.cursor() as cursor:
            cursor.execute("DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='mprp_purge') THEN CREATE ROLE mprp_purge NOLOGIN; END IF; END $$")
            cursor.execute('GRANT USAGE ON SCHEMA public TO mprp_purge')
            cursor.execute('GRANT EXECUTE ON FUNCTION public.sim_purge_retired_namespace(uuid) TO mprp_purge')
            cursor.execute('SET SESSION AUTHORIZATION mprp_purge')
        try:
            complete=False
            for _ in range(20):
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT public.sim_purge_retired_namespace(%s)',[self.account_id])
                        value=cursor.fetchone()[0]
                        value=json.loads(value) if isinstance(value,str) else value
                if value['purge_complete']:
                    complete=True;break
            self.assertTrue(complete)
        finally:
            with connection.cursor() as cursor:cursor.execute('RESET SESSION AUTHORIZATION')
        self.assertFalse(ProviderAccount.objects.filter(pk=self.account_id).exists())
        self.assertIsNotNone(RetiredNamespace.objects.get(account_id=self.account_id).completed_at)
        with self.assertRaises(domain.ProviderError):
            domain.mutate('accounts',self.envelope({'merchant_id':str(merchant_id)}))


    def test_restoration_marker_fences_and_requires_distinct_unchanged_inventory(self):
        from .models import ControlAudit
        marker=domain.mark_restored('fictional-local-operator','Explicit independent database restoration fixture')
        with self.assertRaises(domain.ProviderError):
            domain.mutate('settlements/flush',self.envelope({}))
        self.epoch=domain.handshake({'environment_id':self.environment,'main_instance_id':self.main_instance,'run_token':self.run_token})['dispatch_epoch']
        maker=str(uuid.uuid4());checker=str(uuid.uuid4())
        payload={'environment_id':self.environment,'main_instance_id':self.main_instance,'dispatch_epoch':self.epoch,'restore_marker_id':marker['restore_marker_id'],'inventory_hash':'b'*64,'event_high_water':0,'source_high_water':0,'maker_id':maker,'checker_id':checker,'approval_id':str(uuid.uuid4())}
        with self.assertRaises(domain.ProviderError):domain.release_restoration(dict(payload,checker_id=maker))
        with self.assertRaises(domain.ProviderError):domain.release_restoration(dict(payload,event_high_water=1))
        self.assertFalse(domain.release_restoration(payload)['restored'])
        self.assertFalse(domain.release_restoration(payload)['restored'])
        self.assertEqual(ControlAudit.objects.filter(action='restoration.released').count(),1)
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:cursor.execute('DELETE FROM processor_sim_controlaudit')

    def _backdated_correction(self,at,amount=123):
        with transaction.atomic():
            control=Control.objects.select_for_update().get(pk=1)
            account=ProviderAccount.objects.select_for_update().get(pk=self.account_id)
            source=domain.create_source(control,account,uuid.uuid4(),'CORRECTION_CREDIT',amount,at,reason='Privileged late-source fixture')
            control.save()
        domain.mutate('settlements/flush',self.envelope({}))
        return source

    def test_later_period_trading_reuses_unchanged_past_manifest(self):
        at=domain.now();start=at-timedelta(hours=1);end=at-timedelta(minutes=30)
        first=domain.statement(self.account_id,'PROCESSOR_ECONOMIC',domain.iso(start),domain.iso(end))
        original=bytes(first.csv_content);original_manifest=dict(first.manifest)
        payment_id,_=self.authorize();self.capture(payment_id)
        current=domain.statement(self.account_id,'PROCESSOR_ECONOMIC',domain.iso(start),domain.iso(end))
        self.assertEqual(current.pk,first.pk)
        first.refresh_from_db()
        self.assertEqual(bytes(first.csv_content),original)
        self.assertEqual(first.manifest,original_manifest)
        self.assertEqual(current.revision,1)

    def test_backdated_economic_source_creates_revision_without_rewriting_history(self):
        at=domain.now();start=at-timedelta(hours=1);end=at-timedelta(minutes=30)
        first=domain.statement(self.account_id,'PROCESSOR_ECONOMIC',domain.iso(start),domain.iso(end))
        original=bytes(first.csv_content);original_manifest=dict(first.manifest)
        source=self._backdated_correction(at-timedelta(minutes=45))
        revised=domain.statement(self.account_id,'PROCESSOR_ECONOMIC',domain.iso(start),domain.iso(end))
        self.assertNotEqual(revised.pk,first.pk)
        self.assertNotEqual(revised.statement_id,first.statement_id)
        self.assertEqual(revised.revision,2)
        self.assertEqual(revised.manifest['rows'][0]['source_id'],str(source.pk))
        self.assertEqual(revised.manifest['net_minor'],123)
        self.assertEqual(domain.statement(self.account_id,'PROCESSOR_ECONOMIC',domain.iso(start),domain.iso(end)).pk,revised.pk)
        first.refresh_from_db()
        self.assertEqual(bytes(first.csv_content),original)
        self.assertEqual(first.manifest,original_manifest)

    def test_changed_opening_control_alone_requires_new_immutable_revision(self):
        at=domain.now();start=at-timedelta(minutes=20);end=at-timedelta(minutes=10)
        first=domain.statement(self.account_id,'PROCESSOR_ECONOMIC',domain.iso(start),domain.iso(end))
        self._backdated_correction(at-timedelta(minutes=30))
        revised=domain.statement(self.account_id,'PROCESSOR_ECONOMIC',domain.iso(start),domain.iso(end))
        self.assertNotEqual(revised.pk,first.pk)
        self.assertEqual(bytes(revised.csv_content),bytes(first.csv_content))
        self.assertEqual(revised.manifest['opening_balance_minor'],123)
        self.assertEqual(revised.manifest['closing_balance_minor'],123)
        self.assertEqual(revised.manifest['row_count'],0)
        self.assertNotEqual(revised.content_hash,first.content_hash)

    def test_existing_v1_manifest_survives_revision_schema_upgrade(self):
        from django.db.migrations.executor import MigrationExecutor
        from .models import StatementManifest
        old_target=('processor_sim','0007_control_audit_guard')
        new_target=('processor_sim','0008_remove_statementmanifest_sim_unique_statement_window_and_more')
        executor=MigrationExecutor(connection)
        executor.migrate([old_target])
        try:
            old_model=executor.loader.project_state([old_target]).apps.get_model('processor_sim','StatementManifest')
            at=domain.now();start=at-timedelta(hours=1);end=at-timedelta(minutes=30)
            identity=uuid.uuid4();content=(','.join(domain.ECONOMIC_COLUMNS)+'\n').encode()
            manifest={'schema_version':1,'policy_version':'mprp_v1','manifest_id':str(identity),'statement_id':'legacy_'+identity.hex,'provider_account_id':str(self.account_id),'account_id':str(self.account_id),'kind':'PROCESSOR_ECONOMIC','period_start':domain.iso(start),'period_end':domain.iso(end),'opening_balance_minor':0,'closing_balance_minor':0,'row_count':0,'net_minor':0,'source_high_water':0,'rows':[],'content_hash':domain.digest([])}
            old_model.objects.create(id=identity,account_id=self.account_id,statement_id=manifest['statement_id'],kind='PROCESSOR_ECONOMIC',period_start=start,period_end=end,source_high_water=0,manifest=manifest,csv_content=content)
        finally:
            MigrationExecutor(connection).migrate([new_target])
        current=domain.statement(self.account_id,'PROCESSOR_ECONOMIC',domain.iso(start),domain.iso(end))
        self.assertEqual(current.pk,identity)
        self.assertEqual(current.revision,1)
        self.assertEqual(current.manifest,manifest)
        self.assertEqual(bytes(current.csv_content),content)
