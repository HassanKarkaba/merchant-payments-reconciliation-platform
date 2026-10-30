from django.db import migrations

SQL=r'''
CREATE OR REPLACE FUNCTION mprp_validate_allocation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE line_total numeric; source_total numeric; capacity bigint; source_capacity bigint; invalid integer;
BEGIN
 SELECT COUNT(*) INTO invalid FROM reconciliation_matchallocation a
 JOIN reconciliation_matchgroup g ON g.id=a.group_id
 JOIN reconciliation_statementline l ON l.id=a.line_id
 JOIN accounting_economicsource s ON s.id=a.source_id
 WHERE a.id=NEW.id AND (l.merchant_id<>g.merchant_id OR s.merchant_id<>g.merchant_id OR l.currency<>s.currency OR (a.amount_minor<>0 AND (sign(a.amount_minor)<>sign(l.amount_minor) OR sign(a.amount_minor)<>sign(s.amount_minor))) OR (a.amount_minor=0 AND (l.event_type<>'BATCH_ZERO' OR s.event_type<>'BATCH_ZERO' OR l.amount_minor<>0 OR s.amount_minor<>0)) OR (l.kind='BANK' AND (s.event_type NOT IN ('SETTLEMENT','PAYOUT') OR s.reference<>l.reference)) OR (l.kind<>'BANK' AND (s.provider_account_id<>l.account_id OR s.event_type<>l.event_type OR ((s.source_id<>l.source_id OR s.reference<>l.reference) AND NOT (s.batch_id<>'' AND s.batch_id=l.source_id AND s.batch_id=l.reference AND (SELECT COUNT(DISTINCT aa.source_id) FROM reconciliation_matchallocation aa WHERE aa.group_id=a.group_id)>1)))));
 IF invalid>0 THEN RAISE EXCEPTION 'incompatible reconciliation allocation' USING ERRCODE='23514'; END IF;
 SELECT COALESCE(SUM(a.amount_minor),0),l.amount_minor INTO line_total,capacity FROM reconciliation_statementline l LEFT JOIN reconciliation_matchallocation a ON a.line_id=l.id LEFT JOIN reconciliation_matchgroup g ON g.id=a.group_id WHERE l.id=NEW.line_id AND g.status='MATCHED' GROUP BY l.id;
 SELECT COALESCE(SUM(a.amount_minor),0),s.amount_minor INTO source_total,source_capacity FROM accounting_economicsource s LEFT JOIN reconciliation_matchallocation a ON a.source_id=s.id LEFT JOIN reconciliation_matchgroup g ON g.id=a.group_id JOIN reconciliation_statementline l ON l.id=a.line_id WHERE s.id=NEW.source_id AND g.status='MATCHED' AND l.kind=(SELECT kind FROM reconciliation_statementline WHERE id=NEW.line_id) GROUP BY s.id;
 IF abs(line_total)<>abs(capacity) OR abs(source_total)>abs(source_capacity) THEN RAISE EXCEPTION 'reconciliation evidence capacity exceeded' USING ERRCODE='23514'; END IF;
 IF capacity=0 AND (SELECT COUNT(*) FROM reconciliation_matchallocation a JOIN reconciliation_matchgroup g ON g.id=a.group_id WHERE a.line_id=NEW.line_id AND g.status='MATCHED')>1 THEN RAISE EXCEPTION 'zero batch evidence already matched' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
CREATE CONSTRAINT TRIGGER allocation_capacity_guard AFTER INSERT ON reconciliation_matchallocation DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION mprp_validate_allocation();
CREATE TRIGGER allocation_immutable BEFORE UPDATE OR DELETE ON reconciliation_matchallocation FOR EACH ROW EXECUTE FUNCTION mprp_immutable_financial();
'''
REVERSE='''DROP TRIGGER IF EXISTS allocation_immutable ON reconciliation_matchallocation; DROP TRIGGER IF EXISTS allocation_capacity_guard ON reconciliation_matchallocation; DROP FUNCTION IF EXISTS mprp_validate_allocation();'''
class Migration(migrations.Migration):
    dependencies=[('reconciliation','0001_initial'),('accounting','0003_ledger_guards')]
    operations=[migrations.RunSQL(SQL,REVERSE)]
