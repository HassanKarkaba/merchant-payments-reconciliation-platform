from django.db import migrations

SQL='''
CREATE FUNCTION sim_control_audit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Provider control audit is append only'; END $$;
CREATE TRIGGER sim_control_audit_immutable BEFORE UPDATE OR DELETE ON processor_sim_controlaudit FOR EACH ROW EXECUTE FUNCTION sim_control_audit_immutable();
DO $$ BEGIN IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='mprp_runtime') THEN REVOKE DELETE ON processor_sim_control,processor_sim_provideraccount FROM mprp_runtime; REVOKE UPDATE,DELETE ON processor_sim_controlaudit FROM mprp_runtime; END IF; END $$;
'''

class Migration(migrations.Migration):
    dependencies=[('processor_sim','0006_controlaudit_control_restore_marked_at_and_more')]
    operations=[migrations.RunSQL(SQL,migrations.RunSQL.noop)]
