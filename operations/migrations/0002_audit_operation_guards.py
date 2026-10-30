from django.db import migrations
SQL = """
CREATE FUNCTION mprp_audit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'audit history is append only'; END $$;
CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE ON operations_auditevent FOR EACH ROW EXECUTE FUNCTION mprp_audit_immutable();
CREATE FUNCTION mprp_operation_identity() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
IF ROW(NEW.merchant_id,NEW.actor_id,NEW.kind,NEW.resource_id,NEW.action_key,NEW.fingerprint,NEW.provider_key,NEW.payload,NEW.schema_version,NEW.policy_version) IS DISTINCT FROM ROW(OLD.merchant_id,OLD.actor_id,OLD.kind,OLD.resource_id,OLD.action_key,OLD.fingerprint,OLD.provider_key,OLD.payload,OLD.schema_version,OLD.policy_version) THEN RAISE EXCEPTION 'admitted operation identity is immutable'; END IF; RETURN NEW; END $$;
CREATE TRIGGER operation_identity BEFORE UPDATE ON operations_operation FOR EACH ROW EXECUTE FUNCTION mprp_operation_identity();
DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mprp_runtime') THEN REVOKE UPDATE,DELETE ON operations_auditevent FROM mprp_runtime; END IF; END $$;
"""
class Migration(migrations.Migration):
    dependencies=[('operations','0001_initial')]
    operations=[migrations.RunSQL(SQL,reverse_sql=migrations.RunSQL.noop)]
