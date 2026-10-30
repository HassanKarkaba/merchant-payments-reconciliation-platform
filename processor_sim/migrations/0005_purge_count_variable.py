from django.db import migrations

SQL = r'''
CREATE OR REPLACE FUNCTION sim_purge_retired_namespace(target uuid) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE receipt public.processor_sim_retirednamespace%ROWTYPE; table_names text[]:=ARRAY['processor_sim_statementmanifest','processor_sim_providerevent','processor_sim_banktransfer','processor_sim_economicsource','processor_sim_settlementbatch','processor_sim_providerdispute','processor_sim_providerpayment','processor_sim_provideroperation','processor_sim_provideraccount']; table_name text; removed bigint; more boolean; purge_counts jsonb; key text;
BEGIN
  IF session_user<>'mprp_purge' THEN RAISE EXCEPTION 'Dedicated local purge identity is required'; END IF;
  SELECT * INTO receipt FROM public.processor_sim_retirednamespace WHERE account_id=target FOR UPDATE;
  IF NOT FOUND OR clock_timestamp()<receipt.merchant_closed_at+interval '365 days' THEN RAISE EXCEPTION 'Eligible permanently retired namespace is required'; END IF;
  IF receipt.completed_at IS NOT NULL THEN RETURN jsonb_build_object('purge_complete',true,'retirement_id',receipt.retirement_id,'phase',receipt.phase,'remaining_rows',0); END IF;
  IF EXISTS(SELECT 1 FROM public.processor_sim_provideraccount WHERE id=target AND (NOT retired OR bank_balance_minor<>0)) OR EXISTS(SELECT 1 FROM public.processor_sim_providerdispute WHERE account_id=target AND state IN ('OPEN','RESPONDED')) OR EXISTS(SELECT 1 FROM public.processor_sim_providerpayment WHERE account_id=target AND state='AUTHORIZED') THEN RAISE EXCEPTION 'Retired namespace still has an obligation'; END IF;
  PERFORM set_config('mprp.purge_account',target::text,true);
  table_name:=table_names[receipt.phase+1];
  IF table_name IS NULL THEN RAISE EXCEPTION 'Invalid purge phase'; END IF;
  IF table_name='processor_sim_provideraccount' THEN
    DELETE FROM public.processor_sim_provideraccount WHERE id=target;
    GET DIAGNOSTICS removed=ROW_COUNT;
    more:=false;
  ELSE
    EXECUTE format('DELETE FROM public.%I WHERE ctid IN (SELECT ctid FROM public.%I WHERE account_id=$1 LIMIT 500)',table_name,table_name) USING target;
    GET DIAGNOSTICS removed=ROW_COUNT;
    EXECUTE format('SELECT EXISTS(SELECT 1 FROM public.%I WHERE account_id=$1)',table_name) INTO more USING target;
  END IF;
  key:=replace(table_name,'processor_sim_','');
  purge_counts:=jsonb_set(receipt.purged_counts,ARRAY[key],to_jsonb(COALESCE((receipt.purged_counts->>key)::bigint,0)+removed),true);
  UPDATE public.processor_sim_retirednamespace SET purged_counts=purge_counts,phase=CASE WHEN more THEN receipt.phase ELSE receipt.phase+1 END,completed_at=CASE WHEN receipt.phase=8 AND NOT more THEN clock_timestamp() ELSE NULL END WHERE account_id=target;
  RETURN jsonb_build_object('retirement_id',receipt.retirement_id,'purge_complete',receipt.phase=8 AND NOT more,'phase',CASE WHEN more THEN receipt.phase ELSE receipt.phase+1 END,'removed_rows',removed,'counts',purge_counts);
END $$;
'''

class Migration(migrations.Migration):
    dependencies=[('processor_sim','0004_scoped_namespace_purge')]
    operations=[migrations.RunSQL(SQL,migrations.RunSQL.noop)]
