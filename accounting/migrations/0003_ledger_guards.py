from django.db import migrations

SQL = r'''
DO $$ BEGIN IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='mprp_runtime') THEN REVOKE UPDATE, DELETE ON accounting_journal,accounting_journalentry FROM mprp_runtime; END IF; END $$;
CREATE OR REPLACE FUNCTION mprp_immutable_financial() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'posted financial records are immutable' USING ERRCODE='23514'; END;
$$;
CREATE TRIGGER journal_immutable BEFORE UPDATE OR DELETE ON accounting_journal FOR EACH ROW EXECUTE FUNCTION mprp_immutable_financial();
CREATE TRIGGER entry_immutable BEFORE UPDATE OR DELETE ON accounting_journalentry FOR EACH ROW EXECUTE FUNCTION mprp_immutable_financial();
CREATE OR REPLACE FUNCTION mprp_validate_journal() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE j uuid; n bigint; deb numeric; cred numeric; bad bigint;
BEGIN
 IF TG_TABLE_NAME='accounting_journal' THEN j:=NEW.id; ELSE j:=NEW.journal_id; END IF;
 SELECT COUNT(*),COALESCE(SUM(CASE WHEN e.side='DEBIT' THEN e.amount_minor ELSE 0 END),0),COALESCE(SUM(CASE WHEN e.side='CREDIT' THEN e.amount_minor ELSE 0 END),0),COUNT(*) FILTER(WHERE a.merchant_id<>r.merchant_id OR a.currency<>r.currency OR p.merchant_id<>r.merchant_id OR s.merchant_id<>r.merchant_id OR p.state IN ('CLOSED','REOPEN_PROPOSED'))
 INTO n,deb,cred,bad FROM accounting_journal r
 JOIN accounting_accountingperiod p ON p.id=r.period_id JOIN accounting_economicsource s ON s.id=r.source_id
 LEFT JOIN accounting_journalentry e ON e.journal_id=r.id LEFT JOIN accounting_ledgeraccount a ON a.id=e.account_id WHERE r.id=j;
 IF n<2 OR deb<>cred OR deb=0 OR bad>0 THEN
  RAISE EXCEPTION 'journal must contain balanced positive scoped USD entries in an open period' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END; $$;
CREATE CONSTRAINT TRIGGER journal_balance_guard AFTER INSERT ON accounting_journal DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION mprp_validate_journal();
CREATE CONSTRAINT TRIGGER entry_balance_guard AFTER INSERT ON accounting_journalentry DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION mprp_validate_journal();
CREATE OR REPLACE FUNCTION mprp_source_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.batch_id<>'' AND OLD.batch_id IS DISTINCT FROM NEW.batch_id THEN RAISE EXCEPTION 'economic source batch membership is immutable' USING ERRCODE='23514'; END IF;
 IF ROW(OLD.merchant_id,OLD.provider_account_id,OLD.source_id,OLD.event_type,OLD.amount_minor,OLD.currency,OLD.occurred_at,OLD.reference) IS DISTINCT FROM ROW(NEW.merchant_id,NEW.provider_account_id,NEW.source_id,NEW.event_type,NEW.amount_minor,NEW.currency,NEW.occurred_at,NEW.reference) THEN RAISE EXCEPTION 'economic source is immutable' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER source_core_immutable BEFORE UPDATE ON accounting_economicsource FOR EACH ROW EXECUTE FUNCTION mprp_source_immutable();
CREATE TRIGGER source_delete_immutable BEFORE DELETE ON accounting_economicsource FOR EACH ROW EXECUTE FUNCTION mprp_immutable_financial();
'''
REVERSE = '''DROP TRIGGER IF EXISTS source_delete_immutable ON accounting_economicsource; DROP TRIGGER IF EXISTS source_core_immutable ON accounting_economicsource; DROP TRIGGER IF EXISTS journal_balance_guard ON accounting_journal; DROP TRIGGER IF EXISTS entry_balance_guard ON accounting_journalentry; DROP TRIGGER IF EXISTS journal_immutable ON accounting_journal; DROP TRIGGER IF EXISTS entry_immutable ON accounting_journalentry; DROP FUNCTION IF EXISTS mprp_validate_journal(); DROP FUNCTION IF EXISTS mprp_source_immutable(); DROP FUNCTION IF EXISTS mprp_immutable_financial();'''

class Migration(migrations.Migration):
    dependencies=[('accounting','0002_initial')]
    operations=[migrations.RunSQL(SQL,REVERSE)]
