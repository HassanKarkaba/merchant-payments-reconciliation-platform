from importlib import import_module

from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from accounting.domain import balances, post_economic_event
from accounting.models import EconomicSource, Journal, JournalEntry
from merchants.models import Merchant


upgrade=import_module('reconciliation.migrations.0004_zero_batch_identity')


class AllocationGuardUpgradeTests(TransactionTestCase):
    def setUp(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_get_functiondef('public.mprp_validate_allocation()'::regprocedure)")
            original=cursor.fetchone()[0]
        self.addCleanup(self.restore_function,original)
        self.merchant=Merchant.objects.create(name='Fictional migration sentinel',status='ACTIVE')
        post_economic_event(self.merchant,'upgrade-sentinel','CAPTURE',10000,timezone.now())

    @staticmethod
    def restore_function(definition):
        with connection.cursor() as cursor:
            cursor.execute(definition)

    def financial_snapshot(self):
        return (list(Journal.objects.values().order_by('id')),
                list(JournalEntry.objects.values().order_by('id')),
                list(EconomicSource.objects.values().order_by('id')),
                balances(self.merchant))

    def installed_body(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT prosrc FROM pg_proc WHERE oid='public.mprp_validate_allocation()'::regprocedure")
            return cursor.fetchone()[0].strip()

    def check_upgrade(self,body):
        before=self.financial_snapshot()
        self.restore_function(upgrade.BASE_FUNCTION.replace(upgrade.BASE_BODY,body,1))
        with connection.schema_editor() as editor:
            upgrade.allow_exact_zero_batch_identity(None,editor)
        expected=upgrade.upgraded_function(upgrade.BASE_BODY).split('AS $$',1)[1].split('$$;',1)[0].strip()
        self.assertEqual(self.installed_body(),expected)
        self.assertIn('abs(line_total)<>abs(capacity)',expected)
        self.assertIn('zero batch evidence already matched',expected)
        self.assertIn('zero batch source evidence already matched',expected)
        self.assertEqual(self.financial_snapshot(),before)

    def test_fresh_baseline_upgrade_preserves_posted_ledger(self):
        self.check_upgrade(upgrade.BASE_BODY)

    def test_observed_legacy_baseline_upgrades_all_checks_and_preserves_posted_ledger(self):
        self.assertIn('abs(line_total)>abs(capacity)',upgrade.LEGACY_BODY)
        self.assertNotIn('zero batch evidence already matched',upgrade.LEGACY_BODY)
        self.check_upgrade(upgrade.LEGACY_BODY)

    def test_unknown_modified_baseline_is_rejected_without_replacing_function_or_data(self):
        before=self.financial_snapshot()
        unknown=upgrade.BASE_BODY.replace('l.currency<>s.currency','FALSE',1)
        self.restore_function(upgrade.BASE_FUNCTION.replace(upgrade.BASE_BODY,unknown,1))
        with connection.schema_editor() as editor:
            with self.assertRaisesRegex(RuntimeError,'Unexpected allocation guard baseline'):
                upgrade.allow_exact_zero_batch_identity(None,editor)
        self.assertEqual(self.installed_body(),unknown)
        self.assertEqual(self.financial_snapshot(),before)
