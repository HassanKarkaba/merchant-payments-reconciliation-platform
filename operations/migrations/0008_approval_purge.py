from django.db import migrations

GUARD_SQL = r'''
CREATE TRIGGER approval_receipt_immutable BEFORE UPDATE OR DELETE ON operations_approvalreceipt
FOR EACH ROW EXECUTE FUNCTION mprp_immutable_financial();
DO $$ BEGIN IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='mprp_runtime') THEN REVOKE UPDATE,DELETE ON operations_approvalreceipt FROM mprp_runtime; END IF; END $$;
'''

BATCH_BRANCH = r'''
 IF p.stage=33 THEN
  DELETE FROM operations_approvalreceipt WHERE ctid IN (
   SELECT ctid FROM operations_approvalreceipt WHERE merchant_id=p.merchant_id_safe LIMIT batch_size
  );
  GET DIAGNOSTICS n=ROW_COUNT;
  IF n>0 THEN
   p.deleted_counts:=jsonb_set(p.deleted_counts,ARRAY['operations_approvalreceipt'],to_jsonb(COALESCE((p.deleted_counts->>'operations_approvalreceipt')::bigint,0)+n));
   UPDATE operations_purgereceipt SET status='PURGING',deleted_counts=p.deleted_counts,updated_at=clock_timestamp() WHERE id=p.id;
   RETURN jsonb_build_object('receipt_id',p.id,'status','PURGING','stage',p.stage,'deleted_counts',p.deleted_counts);
  END IF;
 END IF;
'''


def extend_scoped_purger(apps,schema_editor):
    # The retained 0005 migration owns the complete fixed-stage implementation.
    # Add the new owned table before its final Merchant deletion, preserving
    # previously admitted stages and existing function grants/role checks.
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_functiondef('public.mprp_purge_merchant_batch(uuid,integer)'::regprocedure)")
        definition=cursor.fetchone()[0]
    marker=' target_table:=CASE p.stage'
    if definition.count(marker)!=1:raise RuntimeError('Unexpected purge procedure baseline; scoped extension was not applied.')
    schema_editor.execute(definition.replace(marker,BATCH_BRANCH+marker,1),params=None)


class Migration(migrations.Migration):
    dependencies=[('operations','0007_approval_receipt')]
    operations=[migrations.RunSQL(GUARD_SQL,reverse_sql=migrations.RunSQL.noop),migrations.RunPython(extend_scoped_purger,reverse_code=migrations.RunPython.noop)]
