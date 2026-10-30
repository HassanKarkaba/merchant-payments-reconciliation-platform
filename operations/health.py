"""Capability probes never create a money key or an economic effect."""
from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
from .models import Control
from . import provider,run_identity

MAIN_TRIGGERS = {'journal_immutable', 'entry_immutable', 'journal_balance_guard', 'entry_balance_guard',
                 'source_core_immutable', 'source_delete_immutable', 'audit_immutable', 'operation_identity','approval_receipt_immutable'}


def provider_capabilities(control=None):
    result = provider.request('GET', '/v1/capabilities/', timeout=2)
    if result.get('schema_version') != 1 or result.get('policy_version') != 'mprp_v1' or result.get('currency') != 'USD' or result.get('sandbox') is not True:
        return None
    if str(result.get('environment_id')) != str(settings.MPRP_ENVIRONMENT_ID):
        return None
    if control and (str(result.get('simulator_instance_id')) != control.simulator_instance_id or result.get('dispatch_epoch') != control.dispatch_epoch):
        return None
    return result


def capabilities():
    result = {key: False for key in ['read','financial_admission','callback','worker','reconciliation','notification','recovery']}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,(rolname<>'mprp_runtime') FROM pg_roles WHERE rolname=current_user")
            role = cursor.fetchone()
            safe_role = bool(role and not any(role))
            cursor.execute("SELECT current_setting('fsync'),current_setting('synchronous_commit'),current_setting('full_page_writes')")
            durable = cursor.fetchone() == ('on','on','on')
            cursor.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgenabled <> 'D'")
            triggers = MAIN_TRIGGERS <= {row[0] for row in cursor.fetchall()}
        executor = MigrationExecutor(connection)
        migrated = not executor.migration_plan(executor.loader.graph.leaf_nodes())
        result['read'] = migrated
        result['callback'] = migrated and triggers
        result['notification'] = migrated
        result['reconciliation'] = migrated and triggers
        control = Control.objects.first()
        if control:
            age = (timezone.now()-control.worker_heartbeat).total_seconds() if control.worker_heartbeat else 999999
            result['worker'] = 0 <= age < 30
            remote = provider_capabilities(control)
            recovery = bool(remote and remote.get('resolve_key') is True and remote.get('terminal_no_effect') is True and remote.get('immutable_manifests') is True)
            result['recovery'] = recovery
            remote_money = bool(recovery and remote.get('migrations_applied') and remote.get('role_safe') and remote.get('required_triggers') and remote.get('durable_settings') and remote.get('worker') and not remote.get('restored'))
            result['financial_admission'] = bool(migrated and durable and triggers and safe_role and result['worker'] and remote_money and not control.restore_hold and run_identity.is_current(control))
    except Exception:
        # Readiness is deliberately fail closed; liveness remains independent.
        pass
    return result


def local_admission_ready(control=None):
    """Recheck authoritative local prerequisites inside financial admission; no HTTP."""
    control=control or Control.objects.first()
    if not control or control.restore_hold or not control.dispatch_epoch or not control.provider_ready:
        return False
    if not run_identity.is_current(control):return False
    at=timezone.now()
    if control.last_clock and (control.last_clock-at).total_seconds()>2:return False
    if not control.worker_heartbeat or not control.provider_checked_at:
        return False
    if not 0 <= (at-control.worker_heartbeat).total_seconds() < 30 or not 0 <= (at-control.provider_checked_at).total_seconds() < 30:
        return False
    with connection.cursor() as cursor:
        cursor.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,(rolname<>'mprp_runtime') FROM pg_roles WHERE rolname=current_user")
        role=cursor.fetchone()
        if not role or any(role):return False
        cursor.execute("SELECT current_setting('fsync'),current_setting('synchronous_commit'),current_setting('full_page_writes')")
        if cursor.fetchone()!=('on','on','on'):return False
        cursor.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgenabled <> 'D'")
        return MAIN_TRIGGERS <= {row[0] for row in cursor.fetchall()}


def refresh_provider_readiness(control=None):
    """Called outside all financial transactions by the named worker."""
    control=control or Control.objects.first()
    if not control or not run_identity.is_current(control):return False
    ready=False
    try:
        remote=provider_capabilities(control)
        ready=bool(remote and remote.get('resolve_key') and remote.get('terminal_no_effect') and remote.get('immutable_manifests') and remote.get('migrations_applied') and remote.get('role_safe') and remote.get('required_triggers') and remote.get('durable_settings') and remote.get('worker') and not remote.get('restored'))
    except provider.ProviderError:
        pass
    Control.objects.filter(**run_identity.control_scope(control)).update(provider_checked_at=timezone.now(),provider_ready=ready)
    return ready
