from django.db import migrations

SQL=r'''
CREATE OR REPLACE FUNCTION mprp_purge_allowed(tenant uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT session_user='mprp_purge' AND tenant::text=current_setting('mprp.purge_merchant',true)
$$;
CREATE OR REPLACE FUNCTION mprp_immutable_financial() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE tenant uuid;
BEGIN
 tenant:=NULLIF(to_jsonb(OLD)->>'merchant_id','')::uuid;
 IF TG_TABLE_NAME='accounting_journalentry' THEN SELECT merchant_id INTO tenant FROM accounting_journal WHERE id=OLD.journal_id;
 ELSIF TG_TABLE_NAME='reconciliation_matchallocation' THEN SELECT merchant_id INTO tenant FROM reconciliation_matchgroup WHERE id=OLD.group_id;
 END IF;
 IF TG_OP='DELETE' AND mprp_purge_allowed(tenant) THEN RETURN OLD; END IF;
 RAISE EXCEPTION 'posted financial records are immutable' USING ERRCODE='23514';
END; $$;
CREATE OR REPLACE FUNCTION mprp_audit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' AND mprp_purge_allowed(OLD.merchant_id) THEN RETURN OLD; END IF;
 RAISE EXCEPTION 'audit history is append only' USING ERRCODE='23514';
END; $$;
CREATE OR REPLACE FUNCTION mprp_tenant_delete_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE tenant uuid;
BEGIN
 tenant:=NULLIF(to_jsonb(OLD)->>'merchant_id','')::uuid;
 IF TG_TABLE_NAME='disputes_disputeresponse' THEN SELECT merchant_id INTO tenant FROM disputes_dispute WHERE id=OLD.dispute_id;
 ELSIF TG_TABLE_NAME='reconciliation_importrow' THEN SELECT merchant_id INTO tenant FROM reconciliation_statementimport WHERE id=OLD.statement_id;
 ELSIF TG_TABLE_NAME='accounting_closereport' THEN SELECT merchant_id INTO tenant FROM accounting_accountingperiod WHERE id=OLD.period_id;
 ELSIF TG_TABLE_NAME='payments_checkoutsession' THEN SELECT merchant_id INTO tenant FROM payments_checkoutlink WHERE id=OLD.link_id;
 ELSIF TG_TABLE_NAME='merchants_merchant' THEN tenant:=OLD.id;
 ELSIF TG_TABLE_NAME='operations_inbox' THEN SELECT id INTO tenant FROM merchants_merchant WHERE provider_account_id::text=OLD.payload->>'provider_account_id';
 ELSIF TG_TABLE_NAME='operations_job' AND tenant IS NULL THEN SELECT m.id INTO tenant FROM merchants_merchant m JOIN operations_inbox i ON i.payload->>'provider_account_id'=m.provider_account_id::text WHERE i.event_id=OLD.payload->>'event_id';
 ELSIF TG_TABLE_NAME='operations_audithead' THEN BEGIN tenant:=OLD.stream::uuid; EXCEPTION WHEN invalid_text_representation THEN tenant:=NULL; END;
 END IF;
 IF mprp_purge_allowed(tenant) THEN RETURN OLD; END IF;
 RAISE EXCEPTION 'retained tenant data requires the separately privileged complete purge' USING ERRCODE='23514';
END; $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['disputes_disputeresponse','reconciliation_adjustmentproposal','reconciliation_importrow','reconciliation_reconciliationexception','reconciliation_statementline','reconciliation_statementimport','reconciliation_matchgroup','accounting_closereport','accounting_accountingperiod','accounting_ledgeraccount','settlements_transfer','settlements_settlementbatch','settlements_payout','settlements_beneficiary','payments_refund','payments_checkoutsession','disputes_dispute','payments_payment','payments_checkoutlink','operations_operation','operations_job','operations_notification','operations_outbox','operations_export','merchants_membership','operations_audithead','operations_inbox','merchants_merchant'] LOOP
  EXECUTE format('CREATE TRIGGER retained_tenant_delete BEFORE DELETE ON %I FOR EACH ROW EXECUTE FUNCTION mprp_tenant_delete_guard()',t);
 END LOOP;
END $$;
CREATE OR REPLACE FUNCTION mprp_retired_merchant_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM operations_purgereceipt WHERE status IN ('RETIRED','PURGING','COMPLETE') AND (merchant_id_safe=NEW.id OR provider_account_id=NEW.provider_account_id)) THEN RAISE EXCEPTION 'retired merchant and provider namespaces cannot be reused' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER retired_merchant_namespace BEFORE INSERT ON merchants_merchant FOR EACH ROW EXECUTE FUNCTION mprp_retired_merchant_guard();
CREATE OR REPLACE FUNCTION mprp_recovery_inventory_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'recovery inventory is retained platform history' USING ERRCODE='23514'; END IF;
 IF ROW(OLD.maker_id,OLD.environment_id,OLD.simulator_instance_id,OLD.dispatch_epoch,OLD.event_high_water,OLD.source_high_water,OLD.received_cursor,OLD.summary,OLD.findings,OLD.fingerprint,OLD.reason,OLD.created_at) IS DISTINCT FROM ROW(NEW.maker_id,NEW.environment_id,NEW.simulator_instance_id,NEW.dispatch_epoch,NEW.event_high_water,NEW.source_high_water,NEW.received_cursor,NEW.summary,NEW.findings,NEW.fingerprint,NEW.reason,NEW.created_at) THEN RAISE EXCEPTION 'recovery proposal payload is immutable' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER recovery_inventory_immutable BEFORE UPDATE OR DELETE ON operations_recoveryinventory FOR EACH ROW EXECUTE FUNCTION mprp_recovery_inventory_immutable();
CREATE OR REPLACE FUNCTION mprp_purge_receipt_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'namespace retirement receipt is permanent' USING ERRCODE='23514'; END IF;
 IF ROW(OLD.merchant_id_safe,OLD.provider_account_id,OLD.environment_id,OLD.closed_at,OLD.eligible_at) IS DISTINCT FROM ROW(NEW.merchant_id_safe,NEW.provider_account_id,NEW.environment_id,NEW.closed_at,NEW.eligible_at) THEN RAISE EXCEPTION 'retirement scope is immutable' USING ERRCODE='23514'; END IF;
 IF ((NEW.status IS DISTINCT FROM OLD.status AND NEW.status IN ('PURGING','COMPLETE')) OR NEW.stage<>OLD.stage OR NEW.deleted_counts<>OLD.deleted_counts) AND session_user<>'mprp_purge' THEN RAISE EXCEPTION 'only the separate purge procedure advances deletion' USING ERRCODE='23514'; END IF;
 IF OLD.status IN ('RETIRED','PURGING','COMPLETE') AND ROW(OLD.maker_id_safe,OLD.checker_id_safe,OLD.inventory_hash,OLD.dataset_counts,OLD.final_audit_heads) IS DISTINCT FROM ROW(NEW.maker_id_safe,NEW.checker_id_safe,NEW.inventory_hash,NEW.dataset_counts,NEW.final_audit_heads) THEN RAISE EXCEPTION 'retired purge approval is immutable' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER purge_receipt_immutable BEFORE UPDATE OR DELETE ON operations_purgereceipt FOR EACH ROW EXECUTE FUNCTION mprp_purge_receipt_guard();
CREATE OR REPLACE FUNCTION mprp_purge_merchant_batch(receipt_id uuid, batch_size integer DEFAULT 500) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE p operations_purgereceipt%ROWTYPE; m merchants_merchant%ROWTYPE; target_table text; predicate text; n bigint; maxstage integer:=33; env uuid; makerrole text; checkerrole text;
BEGIN
 IF session_user<>'mprp_purge' THEN RAISE EXCEPTION 'separate purge role required' USING ERRCODE='42501'; END IF;
 IF batch_size<1 OR batch_size>1000 THEN RAISE EXCEPTION 'purge batch bounds exceeded'; END IF;
 SELECT * INTO p FROM operations_purgereceipt WHERE id=receipt_id;
 IF NOT FOUND THEN RAISE EXCEPTION 'unknown purge receipt'; END IF;
 IF p.status='COMPLETE' THEN RETURN jsonb_build_object('receipt_id',p.id,'status',p.status,'stage',p.stage,'deleted_counts',p.deleted_counts); END IF;
 SELECT * INTO m FROM merchants_merchant WHERE id=p.merchant_id_safe FOR UPDATE;
 IF NOT FOUND AND p.stage<33 THEN RAISE EXCEPTION 'purge scope merchant is missing'; END IF;
 SELECT * INTO p FROM operations_purgereceipt WHERE id=receipt_id FOR UPDATE;
 SELECT environment_id INTO env FROM operations_control WHERE id=1;
 IF env IS DISTINCT FROM p.environment_id OR p.closed_at IS NULL OR clock_timestamp()<p.closed_at+interval '365 days' OR p.maker_id_safe IS NULL OR p.checker_id_safe IS NULL OR p.maker_id_safe=p.checker_id_safe OR p.approved_at IS NULL OR p.status NOT IN ('APPROVED','RETIRED','PURGING') THEN RAISE EXCEPTION 'purge approval, environment, or retention prerequisite failed'; END IF;
 SELECT platform_role INTO makerrole FROM accounts_user WHERE id=p.maker_id_safe AND is_active AND verified AND totp_confirmed;
 SELECT platform_role INTO checkerrole FROM accounts_user WHERE id=p.checker_id_safe AND is_active AND verified AND totp_confirmed;
 IF p.status<>'PURGING' AND (makerrole IS DISTINCT FROM 'OPERATIONS' OR checkerrole IS DISTINCT FROM 'FINANCE_APPROVER') THEN RAISE EXCEPTION 'purge actors no longer have current authority'; END IF;
 IF p.stage<33 AND (m.status<>'CLOSED' OR m.closed_at IS DISTINCT FROM p.closed_at) THEN RAISE EXCEPTION 'merchant is not the approved closed dataset'; END IF;
 IF EXISTS(SELECT 1 FROM operations_control WHERE restore_hold) OR EXISTS(SELECT 1 FROM operations_operation WHERE merchant_id=p.merchant_id_safe AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED')) OR EXISTS(SELECT 1 FROM disputes_dispute WHERE merchant_id=p.merchant_id_safe AND state IN ('OPEN','RESPONDED')) OR EXISTS(SELECT 1 FROM payments_refund WHERE merchant_id=p.merchant_id_safe AND reserved) OR EXISTS(SELECT 1 FROM settlements_payout WHERE merchant_id=p.merchant_id_safe AND reserved) THEN RAISE EXCEPTION 'active financial work, holds or disputes prohibit purge'; END IF;
 IF COALESCE(p.simulator_receipt->>'retired','false')<>'true' OR COALESCE(p.simulator_receipt->>'purge_complete','false')<>'true' OR COALESCE(p.simulator_receipt->>'provider_account_id','')<>p.provider_account_id::text OR COALESCE(p.simulator_receipt->>'remaining_rows','-1')<>'0' OR COALESCE(p.backup_inventory->>'empty','false')<>'true' THEN RAISE EXCEPTION 'independent simulator purge and complete backup inventory are required'; END IF;
 IF p.stage=0 AND ((p.backup_inventory->>'checked_at')::timestamptz < clock_timestamp()-interval '15 minutes') THEN RAISE EXCEPTION 'backup inventory must be freshly verified at purge admission'; END IF;
 IF p.stage=0 THEN
  IF EXISTS(SELECT 1 FROM accounting_accountingperiod WHERE merchant_id=p.merchant_id_safe AND state<>'CLOSED') OR EXISTS(SELECT 1 FROM reconciliation_reconciliationexception WHERE merchant_id=p.merchant_id_safe AND state NOT IN ('RESOLVED','ACCEPTED_NONFINANCIAL')) THEN RAISE EXCEPTION 'open period or exception prohibits purge'; END IF;
  IF EXISTS(SELECT a.id FROM accounting_ledgeraccount a JOIN accounting_journalentry e ON e.account_id=a.id WHERE a.merchant_id=p.merchant_id_safe GROUP BY a.id HAVING SUM(CASE WHEN e.side=a.normal_side THEN e.amount_minor ELSE -e.amount_minor END)<>0) THEN RAISE EXCEPTION 'nonzero balance or deficit prohibits purge'; END IF;
 END IF;
 PERFORM set_config('mprp.purge_merchant',p.merchant_id_safe::text,true);
 target_table:=CASE p.stage
 WHEN 0 THEN 'disputes_disputeresponse' WHEN 1 THEN 'reconciliation_matchallocation' WHEN 2 THEN 'reconciliation_adjustmentproposal' WHEN 3 THEN 'reconciliation_importrow' WHEN 4 THEN 'reconciliation_reconciliationexception' WHEN 5 THEN 'reconciliation_statementline' WHEN 6 THEN 'reconciliation_statementimport' WHEN 7 THEN 'accounting_journalentry' WHEN 8 THEN 'accounting_journal' WHEN 9 THEN 'accounting_economicsource' WHEN 10 THEN 'accounting_closereport' WHEN 11 THEN 'accounting_accountingperiod' WHEN 12 THEN 'accounting_ledgeraccount' WHEN 13 THEN 'settlements_transfer' WHEN 14 THEN 'settlements_settlementbatch' WHEN 15 THEN 'settlements_payout' WHEN 16 THEN 'settlements_beneficiary' WHEN 17 THEN 'payments_refund' WHEN 18 THEN 'payments_checkoutsession' WHEN 19 THEN 'disputes_dispute' WHEN 20 THEN 'payments_payment' WHEN 21 THEN 'payments_checkoutlink' WHEN 22 THEN 'operations_job' WHEN 23 THEN 'operations_operation' WHEN 24 THEN 'operations_notification' WHEN 25 THEN 'operations_outbox' WHEN 26 THEN 'operations_export' WHEN 27 THEN 'accounts_authtoken' WHEN 28 THEN 'merchants_membership' WHEN 29 THEN 'operations_auditevent' WHEN 30 THEN 'operations_audithead' WHEN 31 THEN 'operations_inbox' WHEN 32 THEN 'reconciliation_matchgroup' WHEN 33 THEN 'merchants_merchant' END;
 predicate:=CASE p.stage
 WHEN 0 THEN format('dispute_id IN (SELECT id FROM disputes_dispute WHERE merchant_id=%L::uuid)',p.merchant_id_safe)
 WHEN 1 THEN format('group_id IN (SELECT id FROM reconciliation_matchgroup WHERE merchant_id=%L::uuid)',p.merchant_id_safe)
 WHEN 3 THEN format('statement_id IN (SELECT id FROM reconciliation_statementimport WHERE merchant_id=%L::uuid)',p.merchant_id_safe)
 WHEN 7 THEN format('journal_id IN (SELECT id FROM accounting_journal WHERE merchant_id=%L::uuid)',p.merchant_id_safe)
 WHEN 10 THEN format('period_id IN (SELECT id FROM accounting_accountingperiod WHERE merchant_id=%L::uuid)',p.merchant_id_safe)
 WHEN 18 THEN format('link_id IN (SELECT id FROM payments_checkoutlink WHERE merchant_id=%L::uuid)',p.merchant_id_safe)
 WHEN 22 THEN format('(merchant_id=%L::uuid OR (kind=''callback_apply'' AND payload->>''event_id'' IN (SELECT event_id FROM operations_inbox WHERE payload->>''provider_account_id''=%L)))',p.merchant_id_safe,p.provider_account_id::text)
 WHEN 30 THEN format('stream=%L',p.merchant_id_safe::text)
 WHEN 31 THEN format('payload->>''provider_account_id''=%L',p.provider_account_id::text)
 WHEN 33 THEN format('id=%L::uuid',p.merchant_id_safe)
 ELSE format('merchant_id=%L::uuid',p.merchant_id_safe) END;
 EXECUTE format('DELETE FROM %I WHERE ctid IN (SELECT ctid FROM %I WHERE %s %s LIMIT %s)',target_table,target_table,predicate,CASE WHEN target_table='accounting_journal' THEN 'ORDER BY sequence DESC' ELSE '' END,batch_size);
 GET DIAGNOSTICS n=ROW_COUNT;
 p.deleted_counts:=jsonb_set(p.deleted_counts,ARRAY[target_table],to_jsonb(COALESCE((p.deleted_counts->>target_table)::bigint,0)+n));
 IF n=0 THEN p.stage:=p.stage+1; END IF;
 p.status:=CASE WHEN p.stage>maxstage THEN 'COMPLETE' ELSE 'PURGING' END;
 UPDATE operations_purgereceipt SET stage=p.stage,status=p.status,deleted_counts=p.deleted_counts,completed_at=CASE WHEN p.status='COMPLETE' THEN clock_timestamp() ELSE NULL END,updated_at=clock_timestamp() WHERE id=p.id;
 RETURN jsonb_build_object('receipt_id',p.id,'status',p.status,'stage',p.stage,'deleted_counts',p.deleted_counts);
END; $$;
REVOKE ALL ON FUNCTION mprp_purge_merchant_batch(uuid,integer) FROM PUBLIC;
DO $$ BEGIN IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='mprp_purge') THEN GRANT EXECUTE ON FUNCTION mprp_purge_merchant_batch(uuid,integer) TO mprp_purge; END IF; END $$;
'''
class Migration(migrations.Migration):
    dependencies=[('operations','0004_outbox_merchant_purgereceipt'),('accounting','0003_ledger_guards'),('reconciliation','0002_allocation_guards'),('disputes','0001_initial'),('settlements','0001_initial')]
    operations=[migrations.RunSQL(SQL,reverse_sql=migrations.RunSQL.noop)]
