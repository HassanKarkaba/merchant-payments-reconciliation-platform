from importlib import import_module

from django.db import migrations

REFERENCE_GUARD="((s.source_id<>l.source_id OR s.reference<>l.reference) AND NOT (s.batch_id<>'' AND s.batch_id=l.source_id AND s.batch_id=l.reference AND (SELECT COUNT(DISTINCT aa.source_id) FROM reconciliation_matchallocation aa WHERE aa.group_id=a.group_id)>1))"
ZERO_GUARD="((s.event_type='BATCH_ZERO' AND (l.kind<>'PROCESSOR_TRANSFER' OR s.source_id<>l.source_id OR s.source_id<>l.batch_id OR s.batch_id<>l.batch_id OR s.amount_minor<>0 OR l.amount_minor<>0 OR a.amount_minor<>0)) OR (s.event_type<>'BATCH_ZERO' AND "+REFERENCE_GUARD+'))'
SOURCE_ZERO_UNIQUENESS="""
 IF source_capacity=0 AND (SELECT COUNT(*) FROM reconciliation_matchallocation a JOIN reconciliation_matchgroup g ON g.id=a.group_id JOIN reconciliation_statementline l ON l.id=a.line_id WHERE a.source_id=NEW.source_id AND l.kind=(SELECT kind FROM reconciliation_statementline WHERE id=NEW.line_id) AND g.status='MATCHED')>1 THEN RAISE EXCEPTION 'zero batch source evidence already matched' USING ERRCODE='23514'; END IF;
"""
LINE_ZERO_UNIQUENESS=" IF capacity=0 AND (SELECT COUNT(*) FROM reconciliation_matchallocation a JOIN reconciliation_matchgroup g ON g.id=a.group_id WHERE a.line_id=NEW.line_id AND g.status='MATCHED')>1 THEN RAISE EXCEPTION 'zero batch evidence already matched' USING ERRCODE='23514'; END IF;\n"

# The first local installation predates the manual grouping and zero-capacity
# hardening in 0002. Recognize its entire function body, rather than accepting a
# partial text match that could silently retain missing or altered checks.
BASE_SQL=import_module('reconciliation.migrations.0002_allocation_guards').SQL
BASE_FUNCTION=BASE_SQL.split('CREATE CONSTRAINT TRIGGER',1)[0].strip()
BASE_BODY=BASE_FUNCTION.split('AS $$',1)[1].split('$$;',1)[0].strip()
LEGACY_BODY=(BASE_BODY.replace(REFERENCE_GUARD,'s.source_id<>l.source_id OR s.reference<>l.reference',1)
             .replace('abs(line_total)<>abs(capacity)','abs(line_total)>abs(capacity)',1)
             .replace(LINE_ZERO_UNIQUENESS,''))


def upgraded_function(body):
    if body.strip() not in (BASE_BODY,LEGACY_BODY):
        raise RuntimeError('Unexpected allocation guard baseline; zero-batch identity change was not applied.')
    end=' RETURN NEW;\nEND;'
    if BASE_FUNCTION.count(REFERENCE_GUARD)!=1 or BASE_FUNCTION.count(end)!=1:
        raise RuntimeError('Unexpected canonical allocation guard definition.')
    # Always install the full canonical checks, including exact line capacity,
    # manual-group reference validation and both zero-evidence uniqueness rules.
    return BASE_FUNCTION.replace(REFERENCE_GUARD,ZERO_GUARD,1).replace(end,SOURCE_ZERO_UNIQUENESS+end,1)


def allow_exact_zero_batch_identity(apps,schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("""SELECT p.prosrc,l.lanname,p.prorettype='trigger'::regtype,
                          p.prosecdef,p.proisstrict,p.proleakproof,p.provolatile,p.proparallel,p.proconfig
                          FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang
                          WHERE p.oid='public.mprp_validate_allocation()'::regprocedure""")
        body,language,is_trigger,definer,strict,leakproof,volatility,parallel,config=cursor.fetchone()
    if (language,is_trigger,definer,strict,leakproof,volatility,parallel,config)!=('plpgsql',True,False,False,False,'v','u',None):
        raise RuntimeError('Unexpected allocation guard properties; zero-batch identity change was not applied.')
    schema_editor.execute(upgraded_function(body),params=None)


class Migration(migrations.Migration):
    dependencies=[('reconciliation','0003_statementimport_policy_version_and_more')]
    operations=[migrations.RunPython(allow_exact_zero_batch_identity,reverse_code=migrations.RunPython.noop)]
