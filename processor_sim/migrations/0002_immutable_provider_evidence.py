from django.db import migrations

SQL = r'''
CREATE FUNCTION sim_immutable_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Provider financial evidence is append only';
  END IF;
  IF TG_TABLE_NAME = 'processor_sim_economicsource' THEN
    IF (to_jsonb(NEW) - 'batch_id') IS DISTINCT FROM (to_jsonb(OLD) - 'batch_id')
      OR OLD.batch_id IS NOT NULL OR NEW.batch_id IS NULL THEN
      RAISE EXCEPTION 'Economic source is immutable except its first batch assignment';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM processor_sim_settlementbatch b WHERE b.id=NEW.batch_id AND b.account_id=NEW.account_id) THEN
      RAISE EXCEPTION 'Economic source and settlement batch account differ';
    END IF;
  ELSIF TG_TABLE_NAME = 'processor_sim_provideroperation' THEN
    IF OLD.result <> '{}'::jsonb OR (to_jsonb(NEW)-'result') IS DISTINCT FROM (to_jsonb(OLD)-'result') THEN
      RAISE EXCEPTION 'Provider operation and terminal result are immutable';
    END IF;
  ELSIF TG_TABLE_NAME = 'processor_sim_providerevent' THEN
    IF (to_jsonb(NEW)-ARRAY['delivered','attempts','due_at','status']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['delivered','attempts','due_at','status']) THEN
      RAISE EXCEPTION 'Provider event body is immutable';
    END IF;
  ELSE
    RAISE EXCEPTION 'Provider financial evidence is immutable';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER sim_source_immutable BEFORE UPDATE OR DELETE ON processor_sim_economicsource FOR EACH ROW EXECUTE FUNCTION sim_immutable_evidence();
CREATE TRIGGER sim_operation_immutable BEFORE UPDATE OR DELETE ON processor_sim_provideroperation FOR EACH ROW EXECUTE FUNCTION sim_immutable_evidence();
CREATE TRIGGER sim_event_immutable BEFORE UPDATE OR DELETE ON processor_sim_providerevent FOR EACH ROW EXECUTE FUNCTION sim_immutable_evidence();
CREATE TRIGGER sim_batch_immutable BEFORE UPDATE OR DELETE ON processor_sim_settlementbatch FOR EACH ROW EXECUTE FUNCTION sim_immutable_evidence();
CREATE TRIGGER sim_transfer_immutable BEFORE UPDATE OR DELETE ON processor_sim_banktransfer FOR EACH ROW EXECUTE FUNCTION sim_immutable_evidence();
CREATE TRIGGER sim_manifest_immutable BEFORE UPDATE OR DELETE ON processor_sim_statementmanifest FOR EACH ROW EXECUTE FUNCTION sim_immutable_evidence();
'''

REVERSE = r'''
DROP TRIGGER IF EXISTS sim_source_immutable ON processor_sim_economicsource;
DROP TRIGGER IF EXISTS sim_operation_immutable ON processor_sim_provideroperation;
DROP TRIGGER IF EXISTS sim_event_immutable ON processor_sim_providerevent;
DROP TRIGGER IF EXISTS sim_batch_immutable ON processor_sim_settlementbatch;
DROP TRIGGER IF EXISTS sim_transfer_immutable ON processor_sim_banktransfer;
DROP TRIGGER IF EXISTS sim_manifest_immutable ON processor_sim_statementmanifest;
DROP FUNCTION IF EXISTS sim_immutable_evidence();
'''


class Migration(migrations.Migration):
    dependencies = [('processor_sim', '0001_initial')]
    operations = [migrations.RunSQL(SQL, REVERSE)]
