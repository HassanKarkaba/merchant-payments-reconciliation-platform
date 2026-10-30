"""Transactional synthetic processor semantics and immutable provider observations."""
import csv
import hashlib
import io
import json
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Sum, Q
from django.utils.dateparse import parse_datetime
from .models import (Control, ProviderAccount, ProviderPayment, ProviderOperation,
                     EconomicSource, SettlementBatch, BankTransfer, ProviderDispute,
                     ProviderEvent, StatementManifest, RetiredNamespace, ControlAudit)

SCHEMA_VERSION = 1
POLICY_VERSION = 'mprp_v1'
SCENARIOS = {'tok_demo_success', 'tok_demo_decline', 'tok_demo_timeout_after_commit', 'tok_demo_requires_review'}
KINDS = {'authorizations', 'captures', 'voids', 'refunds', 'payouts', 'disputes/open', 'disputes/respond', 'corrections', 'settlements/flush', 'accounts', 'accounts/retire'}


class ProviderError(Exception):
    def __init__(self, message, code='invalid_input', status=400):
        super().__init__(message)
        self.code = code
        self.status = status


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def now():
    # The independent provider database decides effect and deadline time.
    with connection.cursor() as cursor:
        cursor.execute('SELECT clock_timestamp()')
        return cursor.fetchone()[0]


def iso(value):
    return value.astimezone(dt_timezone.utc).isoformat().replace('+00:00', 'Z') if value else None


def parse_instant(value):
    if not isinstance(value, str):
        raise ProviderError('A timezone-aware RFC3339 timestamp is required')
    parsed = parse_datetime(value)
    if parsed is None or parsed.tzinfo is None:
        raise ProviderError('A timezone-aware RFC3339 timestamp is required')
    return parsed.astimezone(dt_timezone.utc)


def valid_uuid(value, label='identifier'):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ProviderError(f'Invalid {label}')


def integer(value, low=1, high=100000000):
    if type(value) is not int or not low <= value <= high:
        raise ProviderError(f'Amount must be an integer from {low} through {high} USD cents')
    return value


def locked_control():
    Control.objects.get_or_create(id=1)
    return Control.objects.select_for_update().get(id=1)


def guard_envelope(control, data, allow_restore=False):
    expected = {'schema_version', 'policy_version', 'environment_id', 'main_instance_id', 'dispatch_epoch', 'operation_key', 'provider_account_id', 'payload_hash', 'payload'}
    if set(data) != expected:
        raise ProviderError('Unsupported or missing envelope fields')
    if data['schema_version'] != 1 or data['policy_version'] != POLICY_VERSION:
        raise ProviderError('Unsupported schema or policy', 'unsupported_version')
    if str(data['environment_id']) != control.environment_id or control.environment_id != str(settings.MPRP_ENVIRONMENT_ID):
        raise ProviderError('Environment binding mismatch', 'binding_mismatch', 409)
    if str(data['main_instance_id']) != control.main_instance_id or data['dispatch_epoch'] != control.dispatch_epoch or not control.run_token:
        raise ProviderError('Dispatch epoch has been fenced', 'stale_epoch', 409)
    if not isinstance(data['payload'], dict) or data['payload_hash'] != digest(data['payload']):
        raise ProviderError('Canonical payload hash mismatch', 'payload_conflict', 409)
    key = data['operation_key']
    if not isinstance(key, str) or not 16 <= len(key) <= 128 or not key.isascii():
        raise ProviderError('Invalid immutable operation key')
    if control.restored and not allow_restore:
        raise ProviderError('Simulator restoration marker is active', 'restore_hold', 503)
    return key


@transaction.atomic
def handshake(data):
    if set(data) != {'environment_id', 'main_instance_id', 'run_token'}:
        raise ProviderError('Invalid handshake fields')
    environment = str(valid_uuid(data['environment_id'], 'environment'))
    main_instance = str(valid_uuid(data['main_instance_id'], 'main instance'))
    token = str(valid_uuid(data['run_token'], 'run token'))
    if environment != str(settings.MPRP_ENVIRONMENT_ID):
        raise ProviderError('Environment binding mismatch', 'binding_mismatch', 409)
    control = locked_control()
    if control.environment_id and (control.environment_id != environment or control.main_instance_id != main_instance):
        raise ProviderError('Provider namespace is already bound to another main database', 'binding_mismatch', 409)
    if token in control.retired_tokens:
        raise ProviderError('A replaced run token cannot be reactivated', 'stale_epoch', 409)
    if token != control.run_token:
        old = control.run_token
        control.retired_tokens = control.retired_tokens + ([old] if old else [])
        control.run_token = token
        control.dispatch_epoch += 1
    control.environment_id = environment
    control.main_instance_id = main_instance
    control.save()
    return capabilities(control)


def capabilities(control=None):
    control = control or Control.objects.filter(id=1).first()
    with connection.cursor() as cursor:
        cursor.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgenabled <> 'D'")
        triggers = {row[0] for row in cursor.fetchall()}
        cursor.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,(rolname<>'mprp_runtime') FROM pg_roles WHERE rolname=current_user")
        role = cursor.fetchone()
        cursor.execute("SELECT current_setting('fsync'),current_setting('synchronous_commit'),current_setting('full_page_writes')")
        durable = cursor.fetchone() == ('on','on','on')
    required = {'sim_source_immutable','sim_operation_immutable','sim_event_immutable','sim_batch_immutable','sim_transfer_immutable','sim_manifest_immutable','sim_control_audit_immutable'}
    from django.db.migrations.executor import MigrationExecutor
    executor=MigrationExecutor(connection)
    migrated=not executor.migration_plan(executor.loader.graph.leaf_nodes())
    worker_fresh = bool(control and control.worker_heartbeat and (now()-control.worker_heartbeat).total_seconds() < 30)
    return {'schema_version': 1, 'policy_version': POLICY_VERSION, 'environment_id': str(settings.MPRP_ENVIRONMENT_ID),
            'migrations_applied':migrated,'role_safe': bool(role and not any(role)), 'required_triggers': required <= triggers, 'durable_settings': durable, 'worker': worker_fresh,
            'simulator_instance_id': str(control.instance_id) if control else None,
            'dispatch_epoch': control.dispatch_epoch if control else 0,
            'acknowledged_cursor': control.acknowledged_cursor if control else 0,
            'event_high_water': control.next_event_sequence - 1 if control else 0,
            'source_high_water': control.next_source_sequence - 1 if control else 0,
            'resolve_key': True, 'terminal_no_effect': True, 'immutable_manifests': True,
            'currency': 'USD', 'sandbox': True, 'restored': bool(control and control.restored), 'restore_marker_id': str(control.restore_marker_id) if control and control.restore_marker_id else None}


def source_dict(source):
    result = {'id': str(source.id), 'source_id': str(source.id), 'sequence': source.sequence,
            'event_type': source.event_type, 'kind': source.event_type,
            'object_id': str(source.object_id), 'payment_id': str(source.payment_id) if source.payment_id else None,
            'provider_account_id': str(source.account_id), 'currency': 'USD', 'amount_minor': source.amount_minor,
            'occurred_at': iso(source.occurred_at), 'reference': source.reference,
            'dependencies': source.dependencies, 'batch_id': str(source.batch_id) if source.batch_id else None}
    if source.event_type.startswith('DISPUTE_'):
        dispute = ProviderDispute.objects.filter(id=source.object_id).first()
        if dispute:
            result.update({'dispute_id': str(dispute.id), 'principal_minor': dispute.principal_minor,
                'opened_at': iso(dispute.opened_at), 'deadline': iso(dispute.deadline),
                'state': dispute.state, 'provider_version': dispute.version, 'version': dispute.version,
                'response_version': dispute.response_version})
    return result


def create_source(control, account, object_id, event_type, amount_minor, at, payment_id=None, dependencies=None, clearing=True, batch=None, reason=''):
    source_id = uuid.uuid4()
    source = EconomicSource.objects.create(id=source_id, sequence=control.next_source_sequence,
        account=account, object_id=object_id, payment_id=payment_id, event_type=event_type,
        amount_minor=amount_minor, occurred_at=at, reference=f'{event_type.lower()}_{source_id.hex}',
        dependencies=dependencies or [], clearing_line=clearing, batch=batch, reason=reason)
    control.next_source_sequence += 1
    return source


def payment_snapshot(payment):
    dispute = ProviderDispute.objects.filter(payment=payment).first()
    return {'object_type': 'payment', 'object_id': str(payment.id), 'payment_id': str(payment.id),
            'provider_id': str(payment.id), 'provider_account_id': str(payment.account_id), 'currency': 'USD',
            'authorized_minor': payment.authorized_minor, 'captured_minor': payment.captured_minor,
            'refunded_minor': payment.refunded_minor, 'fee_minor': payment.fee_minor, 'state': payment.state,
            'version': payment.version, 'provider_version': payment.version,
            'authorization_expires_at': iso(payment.authorization_expires_at),
            'dispute_id': str(dispute.id) if dispute else None,
            'dispute_state': dispute.state if dispute else None,
            'sources': [source_dict(s) for s in EconomicSource.objects.filter(payment_id=payment.id)]}


def dispute_snapshot(dispute):
    return {'object_type': 'dispute', 'object_id': str(dispute.id), 'dispute_id': str(dispute.id),
            'payment_id': str(dispute.payment_id), 'provider_account_id': str(dispute.account_id), 'currency': 'USD',
            'principal_minor': dispute.principal_minor, 'amount_minor': dispute.principal_minor,
            'state': dispute.state, 'version': dispute.version, 'provider_version': dispute.version,
            'opened_at': iso(dispute.opened_at), 'deadline': iso(dispute.deadline),
            'response_version': dispute.response_version, 'response_stored': bool(dispute.response),
            'decision_at': iso(dispute.decision_at),
            'sources': [source_dict(s) for s in EconomicSource.objects.filter(payment_id=dispute.payment_id)]}


def payout_snapshot(transfer):
    return {'object_type': 'payout', 'object_id': str(transfer.id), 'payout_id': str(transfer.id),
            'provider_account_id': str(transfer.account_id), 'currency': 'USD', 'amount_minor': -transfer.amount_minor,
            'beneficiary_token': transfer.beneficiary_token, 'state': 'PAID', 'version': 1, 'provider_version': 1,
            'reference': transfer.reference, 'sources': [source_dict(transfer.source)]}


def batch_snapshot(batch):
    sources = list(batch.economic_sources.all())
    transfer = BankTransfer.objects.filter(batch=batch).first()
    if transfer:
        sources.append(transfer.source)
    return {'object_type': 'settlement', 'object_id': str(batch.id), 'batch_id': str(batch.id),
            'provider_account_id': str(batch.account_id), 'currency': 'USD', 'net_minor': batch.net_minor,
            'amount_minor': batch.net_minor, 'cutoff': iso(batch.cutoff), 'state': batch.state,
            'version': 1, 'provider_version': 1, 'occurred_at': iso(batch.created_at), 'created_at': iso(batch.created_at),
            'transfer_id': str(transfer.id) if transfer else None,
            'transfer_reference': transfer.reference if transfer else None,
            'sources': [source_dict(s) for s in sorted(sources, key=lambda s: s.sequence)[:500]],
            'source_count': len(sources), 'history_complete': len(sources) <= 500,
            'source_high_water': max((source.sequence for source in sources), default=0)}


def account_snapshot(account):
    return {'object_type': 'account', 'object_id': str(account.id), 'provider_account_id': str(account.id),
            'merchant_id': str(account.merchant_id), 'beneficiary_token': account.beneficiary_token,
            'bank_account_id': account.bank_account_id, 'bank_balance_minor': account.bank_balance_minor,
            'currency': 'USD', 'retired': account.retired}


def emit(control, account, event_type, snapshot, at):
    event_id = uuid.uuid4()
    body = {'event_id': str(event_id), 'event_type': event_type,
            'object_type': snapshot['object_type'], 'object_id': snapshot['object_id'],
            'provider_account_id': str(account.id), 'environment_id': control.environment_id,
            'provider_version': snapshot.get('version', 1), 'occurred_at': iso(at),
            'payload': snapshot, 'schema_version': 1}
    ProviderEvent.objects.create(sequence=control.next_event_sequence, event_id=event_id,
        account=account, body=body, due_at=at)
    control.next_event_sequence += 1


def envelope_result(control, operation, snapshot=None, reason=''):
    return {'schema_version': 1, 'policy_version': POLICY_VERSION, 'environment_id': control.environment_id,
            'simulator_instance_id': str(control.instance_id), 'operation_key': operation.key,
            'state': operation.state, 'payload_hash': operation.payload_hash, 'reason': reason, 'snapshot': snapshot,
            'source_high_water': control.next_source_sequence - 1}


def operation_response(operation):
    # Older retained v1 rows remain immutable; the bound request hash is transport metadata.
    result=dict(operation.result)
    result.setdefault('payload_hash',operation.payload_hash)
    return result


def check_payload(payload, required, optional=()):
    if not set(required) <= set(payload) or set(payload) - set(required) - set(optional):
        raise ProviderError('Unsupported or missing payload fields')


def account_for(data):
    account_id = valid_uuid(data['provider_account_id'], 'provider account')
    try:
        account = ProviderAccount.objects.select_for_update().get(id=account_id, environment_id=str(settings.MPRP_ENVIRONMENT_ID), retired=False)
    except ProviderAccount.DoesNotExist:
        raise ProviderError('Provider account does not exist', 'not_found', 404)
    return account


def payment_for(account, payload):
    try:
        return ProviderPayment.objects.select_for_update().get(id=valid_uuid(payload['payment_id']), account=account)
    except ProviderPayment.DoesNotExist:
        raise ProviderError('Payment does not exist in this provider account', 'not_found', 404)


@transaction.atomic
def mutate(kind, data):
    if kind not in KINDS:
        raise ProviderError('Unknown operation', 'not_found', 404)
    control = locked_control()
    key = guard_envelope(control, data)
    existing = ProviderOperation.objects.filter(key=key).first()
    if existing:
        if str(existing.account_id) != str(data['provider_account_id']):
            raise ProviderError('Operation key belongs to another provider account', 'key_conflict', 409)
        if existing.state == 'NO_EFFECT':
            return operation_response(existing)
        if existing.payload_hash != data['payload_hash'] or existing.kind != kind or str(existing.account_id) != str(data['provider_account_id']):
            raise ProviderError('Operation key has another immutable payload or scope', 'key_conflict', 409)
        return operation_response(existing)
    payload = data['payload']
    if 'provider_account_id' in payload and str(payload['provider_account_id']) != str(data['provider_account_id']):
        raise ProviderError('Payload account conflicts with envelope', 'binding_mismatch', 409)
    if 'fee_policy' in payload and payload['fee_policy'] != POLICY_VERSION:
        raise ProviderError('Unsupported fee policy')
    at = now()
    if kind == 'accounts':
        check_payload(payload, {'merchant_id'}, {'provider_account_id'})
        account_id = valid_uuid(data['provider_account_id'])
        merchant_id = valid_uuid(payload['merchant_id'])
        if RetiredNamespace.objects.filter(Q(account_id=account_id)|Q(merchant_id=merchant_id)).exists():
            raise ProviderError('This namespace is permanently retired', 'retired_namespace', 409)
        existing_account = ProviderAccount.objects.filter(Q(id=account_id) | Q(merchant_id=merchant_id)).first()
        if existing_account and (existing_account.id != account_id or existing_account.merchant_id != merchant_id or existing_account.retired):
            raise ProviderError('Provider account identity conflict', 'account_conflict', 409)
        account = existing_account or ProviderAccount.objects.create(id=account_id, merchant_id=merchant_id, environment_id=control.environment_id)
        snapshot = account_snapshot(account)
        operation = ProviderOperation.objects.create(key=key, payload_hash=data['payload_hash'], kind=kind, account=account, object_id=account.id, state='SUCCEEDED', payload=payload)
    else:
        account = account_for(data)
        operation = ProviderOperation(key=key, payload_hash=data['payload_hash'], kind=kind, account=account, state='SUCCEEDED', payload=payload)
        try:
            snapshot = _perform(kind, control, account, payload, at, key)
        except ProviderError as error:
            # Valid economic requests can definitively fail without effect; malformed transport is rejected before key consumption.
            if error.status not in {409, 422}:
                raise
            operation.state = 'FAILED'
            operation.object_id = valid_uuid(payload.get('payment_id') or payload.get('payout_id')) if payload.get('payment_id') or payload.get('payout_id') else None
            operation.result = envelope_result(control, operation, reason=str(error))
            operation.save()
            control.save()
            return operation_response(operation)
        operation.object_id = valid_uuid(snapshot['object_id'])
        operation.save()
    operation.result = envelope_result(control, operation, snapshot)
    operation.save(update_fields=['result'])
    control.save()
    return operation_response(operation)


def _perform(kind, control, account, payload, at, key):
    if kind == 'accounts/retire':
        check_payload(payload, {'merchant_closed_at', 'approval_id', 'inventory_hash', 'reason'})
        closed_at = parse_instant(payload['merchant_closed_at'])
        if at < closed_at + timedelta(days=365):
            raise ProviderError('Financial retention has not expired', 'retention_not_expired', 409)
        approval_id = valid_uuid(payload['approval_id'])
        inventory_hash = payload['inventory_hash']
        if not isinstance(inventory_hash, str) or len(inventory_hash) != 64 or any(ch not in '0123456789abcdef' for ch in inventory_hash):
            raise ProviderError('A canonical purge inventory hash is required')
        if not isinstance(payload['reason'], str) or not 1 <= len(payload['reason'].strip()) <= 2000:
            raise ProviderError('Retirement reason is required')
        if account.bank_balance_minor != 0 or ProviderDispute.objects.filter(account=account, state__in=['OPEN','RESPONDED']).exists() or ProviderPayment.objects.filter(account=account, state='AUTHORIZED').exists() or EconomicSource.objects.filter(account=account, clearing_line=True, batch__isnull=True).exists():
            raise ProviderError('Provider namespace retains an unresolved obligation', 'namespace_not_clear', 409)
        counts = {model._meta.model_name: model.objects.filter(account=account).count() for model in [ProviderPayment,ProviderOperation,EconomicSource,SettlementBatch,BankTransfer,ProviderDispute,ProviderEvent,StatementManifest]}
        counts['provideroperation'] += 1  # This retirement's own immutable operation is part of the namespace.
        receipt, _ = RetiredNamespace.objects.get_or_create(account_id=account.id, defaults={'merchant_id':account.merchant_id,'environment_id':control.environment_id,'merchant_closed_at':closed_at,'approval_id':approval_id,'inventory_hash':inventory_hash,'counts':counts})
        account.retired = True
        account.save(update_fields=['retired'])
        return {'object_type':'account','object_id':str(account.id),'provider_account_id':str(account.id),'state':'RETIRED','retired':True,'retirement_id':str(receipt.retirement_id),'counts':counts,'remaining_rows':sum(counts.values())+1,'purge_complete':False,'version':1}
    if kind == 'authorizations':
        check_payload(payload, {'payment_id', 'amount_minor', 'currency', 'scenario'}, {'capture_mode', 'provider_account_id', 'customer_name', 'checkout_session_id'})
        if payload['currency'] != 'USD' or payload['scenario'] not in SCENARIOS:
            raise ProviderError('Only selected synthetic USD scenarios are supported')
        amount = integer(payload['amount_minor'], 100)
        payment_id = valid_uuid(payload['payment_id'])
        if ProviderPayment.objects.filter(id=payment_id).exists():
            raise ProviderError('Payment already has an authorization attempt', 'state_conflict', 409)
        if payload['scenario'] in {'tok_demo_decline', 'tok_demo_requires_review'}:
            raise ProviderError('Synthetic decline' if payload['scenario'] == 'tok_demo_decline' else 'Synthetic nonfinancial review rejection; inspect this attempt', 'declined', 422)
        payment = ProviderPayment.objects.create(id=payment_id, account=account, authorized_minor=amount, authorization_expires_at=at+timedelta(days=7))
        snapshot = payment_snapshot(payment)
        emit(control, account, 'AUTHORIZATION_SUCCEEDED', snapshot, at)
        return snapshot
    if kind in {'captures', 'voids', 'refunds'}:
        required = {'payment_id'} | ({'amount_minor'} if kind != 'voids' else set())
        check_payload(payload, required, {'currency', 'fee_minor', 'fee_policy', 'reason', 'refund_id', 'scenario', 'provider_account_id'})
        if payload.get('currency', 'USD') != 'USD':
            raise ProviderError('Currency must be USD')
        payment = payment_for(account, payload)
        if kind in {'captures', 'voids'} and payment.state == 'AUTHORIZED' and at >= payment.authorization_expires_at:
            payment.state = 'EXPIRED'
            payment.version += 1
            payment.save()
        if kind == 'voids':
            if payment.state != 'AUTHORIZED':
                raise ProviderError('Only an unexpired, uncaptured authorization can be voided', 'state_conflict', 409)
            payment.state = 'VOIDED'
            payment.version += 1
            payment.save()
            snapshot = payment_snapshot(payment)
            emit(control, account, 'AUTHORIZATION_VOIDED', snapshot, at)
            return snapshot
        if kind == 'captures':
            amount = integer(payload['amount_minor'], 100)
            if payment.state != 'AUTHORIZED' or payment.captured_minor or amount > payment.authorized_minor:
                raise ProviderError('Authorization is not eligible for a final capture', 'state_conflict', 409)
            fee = (29 * amount + 500) // 1000 + 30
            if 'fee_minor' in payload and payload['fee_minor'] != fee:
                raise ProviderError('Captured fee conflicts with mprp_v1', 'fee_conflict', 409)
            payment.captured_minor = amount
            payment.fee_minor = fee
            payment.state = 'CAPTURED'
            payment.version += 1
            principal = create_source(control, account, payment.id, 'CAPTURE', amount, at, payment_id=payment.id)
            create_source(control, account, payment.id, 'FEE', -fee, at, payment_id=payment.id, dependencies=[str(principal.id)])
            payment.save()
            snapshot = payment_snapshot(payment)
            emit(control, account, 'CAPTURE_SUCCEEDED', snapshot, at)
            return snapshot
        amount = integer(payload['amount_minor'])
        if not isinstance(payload.get('reason', ''), str) or len(payload.get('reason', '')) > 2000:
            raise ProviderError('Refund reason is invalid')
        dispute = ProviderDispute.objects.filter(payment=payment).first()
        excluded = dispute.principal_minor if dispute and dispute.state in {'OPEN', 'RESPONDED', 'LOST'} else 0
        if payment.state != 'CAPTURED' or amount > payment.captured_minor-payment.refunded_minor-excluded:
            raise ProviderError('Captured principal is unavailable for this refund', 'principal_unavailable', 409)
        if EconomicSource.objects.filter(payment_id=payment.id, event_type='REFUND').count() >= 100:
            raise ProviderError('Maximum successful refund count reached', 'refund_limit', 409)
        refund_id = valid_uuid(payload['refund_id']) if payload.get('refund_id') else uuid.uuid4()
        capture = EconomicSource.objects.get(payment_id=payment.id, event_type='CAPTURE')
        create_source(control, account, refund_id, 'REFUND', -amount, at, payment_id=payment.id, dependencies=[str(capture.id)])
        payment.refunded_minor += amount
        payment.version += 1
        payment.save()
        snapshot = payment_snapshot(payment)
        snapshot['refund_id'] = str(refund_id)
        emit(control, account, 'REFUND_SUCCEEDED', snapshot, at)
        return snapshot
    if kind == 'payouts':
        check_payload(payload, {'payout_id', 'amount_minor', 'beneficiary_token'}, {'currency', 'reason', 'provider_account_id', 'beneficiary_version'})
        amount = integer(payload['amount_minor'])
        payout_id = valid_uuid(payload['payout_id'])
        if payload.get('currency', 'USD') != 'USD' or payload['beneficiary_token'] != account.beneficiary_token:
            raise ProviderError('Synthetic beneficiary does not belong to this account', 'beneficiary_mismatch', 409)
        if account.bank_balance_minor < amount:
            raise ProviderError('Insufficient settled synthetic bank funds', 'insufficient_funds', 409)
        if BankTransfer.objects.filter(id=payout_id).exists():
            raise ProviderError('Payout identity already exists', 'state_conflict', 409)
        source = create_source(control, account, payout_id, 'PAYOUT', -amount, at, clearing=False)
        transfer = BankTransfer.objects.create(id=payout_id, account=account, kind='PAYOUT', amount_minor=-amount, reference=source.reference, occurred_at=at, beneficiary_token=account.beneficiary_token, source=source)
        account.bank_balance_minor -= amount
        account.save(update_fields=['bank_balance_minor'])
        snapshot = payout_snapshot(transfer)
        emit(control, account, 'PAYOUT_SUCCEEDED', snapshot, at)
        return snapshot
    if kind == 'disputes/open':
        check_payload(payload, {'payment_id', 'outcome'}, {'dispute_id'})
        payment = payment_for(account, payload)
        if payload['outcome'] not in {'demo_won', 'demo_lost'}:
            raise ProviderError('Unsupported synthetic dispute outcome')
        if payment.state != 'CAPTURED' or ProviderDispute.objects.filter(payment=payment).exists() or payment.captured_minor <= payment.refunded_minor:
            raise ProviderError('Payment is not eligible for one dispute', 'state_conflict', 409)
        dispute = ProviderDispute.objects.create(id=valid_uuid(payload['dispute_id']) if payload.get('dispute_id') else uuid.uuid4(), payment=payment, account=account, principal_minor=payment.captured_minor-payment.refunded_minor, selected_outcome=payload['outcome'], opened_at=at, deadline=at+timedelta(days=7))
        prior = [str(s.id) for s in EconomicSource.objects.filter(payment_id=payment.id)]
        create_source(control, account, dispute.id, 'DISPUTE_OPEN', -dispute.principal_minor, at, payment_id=payment.id, dependencies=prior)
        payment.version += 1
        payment.save(update_fields=['version'])
        snapshot = dispute_snapshot(dispute)
        emit(control, account, 'DISPUTE_OPENED', snapshot, at)
        return snapshot
    if kind == 'disputes/respond':
        check_payload(payload, {'dispute_id', 'text'}, {'expected_version', 'response_version'})
        text = payload['text']
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
            raise ProviderError('Response must contain from 1 through 2000 plain-text characters')
        try:
            dispute = ProviderDispute.objects.select_for_update().get(id=valid_uuid(payload['dispute_id']), account=account)
        except ProviderDispute.DoesNotExist:
            raise ProviderError('Dispute not found', 'not_found', 404)
        if at >= dispute.deadline or dispute.state not in {'OPEN', 'RESPONDED'}:
            raise ProviderError('Dispute response deadline has passed', 'deadline_passed', 409)
        if 'expected_version' in payload and payload['expected_version'] != dispute.version:
            raise ProviderError('Dispute response version is stale', 'stale_version', 409)
        dispute.response = text.strip()
        dispute.response_version += 1
        dispute.version += 1
        dispute.state = 'RESPONDED'
        if not dispute.first_response_at:
            dispute.first_response_at = at
            dispute.decision_at = at + timedelta(seconds=60)
        dispute.save()
        snapshot = dispute_snapshot(dispute)
        emit(control, account, 'DISPUTE_RESPONSE_STORED', snapshot, at)
        return snapshot
    if kind == 'corrections':
        check_payload(payload, {'amount_minor', 'reason'}, {'original_source_id', 'correction_id'})
        amount = payload['amount_minor']
        if type(amount) is not int or not 1 <= abs(amount) <= 100000000:
            raise ProviderError('Correction must be nonzero bounded signed USD cents')
        reason = payload['reason']
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise ProviderError('Correction reason is required')
        dependencies = []
        if payload.get('original_source_id'):
            original = EconomicSource.objects.filter(id=valid_uuid(payload['original_source_id']), account=account).first()
            if not original:
                raise ProviderError('Original correction source does not belong to account')
            if original.event_type in {'CORRECTION_CREDIT', 'CORRECTION_DEBIT'} and amount != -original.amount_minor:
                raise ProviderError('A correction reversal must be the exact opposite amount')
            dependencies = [str(original.id)]
        correction_id = valid_uuid(payload['correction_id']) if payload.get('correction_id') else uuid.uuid4()
        source = create_source(control, account, correction_id, 'CORRECTION_CREDIT' if amount > 0 else 'CORRECTION_DEBIT', amount, at, dependencies=dependencies, reason=reason.strip())
        snapshot = {'object_type': 'correction', 'object_id': str(correction_id), 'provider_account_id': str(account.id), 'currency': 'USD', 'amount_minor': amount, 'state': 'CONFIRMED', 'version': 1, 'sources': [source_dict(source)]}
        emit(control, account, 'CORRECTION_CREATED', snapshot, at)
        return snapshot
    if kind == 'settlements/flush':
        check_payload(payload, set(), {'cutoff', 'cutoff_identity'})
        cutoff = parse_instant(payload['cutoff']) if payload.get('cutoff') else at
        if cutoff > at:
            raise ProviderError('Settlement cutoff cannot be in the future')
        identity = payload.get('cutoff_identity') or f'flush_{key}'
        if not isinstance(identity, str) or not 1 <= len(identity) <= 100:
            raise ProviderError('Invalid cutoff identity')
        return flush_account(control, account, cutoff, identity, at)
    raise ProviderError('Unknown operation', 'not_found', 404)


def flush_account(control, account, cutoff, identity, at):
    existing = SettlementBatch.objects.filter(account=account, cutoff_identity=identity).first()
    if existing:
        return batch_snapshot(existing)
    lines = list(EconomicSource.objects.select_for_update().filter(account=account, clearing_line=True, batch__isnull=True, occurred_at__lt=cutoff))
    net = sum(s.amount_minor for s in lines)
    if not -(2**63) < net < 2**63 or not -(2**63) < account.bank_balance_minor + net < 2**63:
        raise ProviderError('Settlement arithmetic exceeds signed 64-bit bounds')
    batch = SettlementBatch.objects.create(account=account, cutoff_identity=identity, cutoff=cutoff, net_minor=net)
    if lines:
        EconomicSource.objects.filter(id__in=[s.id for s in lines]).update(batch=batch)
    if net:
        source = create_source(control, account, batch.id, 'SETTLEMENT', net, at, dependencies=[str(s.id) for s in lines], clearing=False, batch=None)
        transfer_id = uuid.uuid4()
        BankTransfer.objects.create(id=transfer_id, account=account, batch=batch, kind='SETTLEMENT', amount_minor=net, reference=source.reference, occurred_at=at, source=source)
        account.bank_balance_minor += net
        account.save(update_fields=['bank_balance_minor'])
    snapshot = batch_snapshot(batch)
    emit(control, account, 'SETTLEMENT_COMPLETED', snapshot, at)
    return snapshot


@transaction.atomic
def resolve_key(data):
    control = locked_control()
    # The resolver uses the same authenticated/fenced envelope, but its payload is empty.
    key = guard_envelope(control, data, allow_restore=True)
    operation = ProviderOperation.objects.filter(key=key).first()
    if operation:
        if operation.account_id and str(operation.account_id) != str(data['provider_account_id']):
            raise ProviderError('Operation does not belong to provider account', 'not_found', 404)
        return operation_response(operation)
    account = account_for(data)
    operation = ProviderOperation.objects.create(key=key, kind='resolve-key', account=account, state='NO_EFFECT')
    operation.result = envelope_result(control, operation, reason='Terminal no-effect tombstone installed; this key can never commit an economic effect')
    operation.save(update_fields=['result'])
    return operation_response(operation)


def operation_lookup(key, account_id=None):
    operation = ProviderOperation.objects.filter(key=key).first()
    if not operation or (account_id and str(operation.account_id) != str(account_id)):
        raise ProviderError('No recorded operation; absence is not terminal no-effect evidence', 'not_found', 404)
    return operation_response(operation)


def object_snapshot(object_id):
    obj_id = valid_uuid(object_id)
    payment = ProviderPayment.objects.filter(id=obj_id).first()
    if payment:
        return payment_snapshot(payment)
    dispute = ProviderDispute.objects.filter(id=obj_id).first()
    if dispute:
        return dispute_snapshot(dispute)
    batch = SettlementBatch.objects.filter(id=obj_id).first()
    if batch:
        return batch_snapshot(batch)
    transfer = BankTransfer.objects.filter(id=obj_id, kind='PAYOUT').first()
    if transfer:
        return payout_snapshot(transfer)
    sources = EconomicSource.objects.filter(object_id=obj_id)
    if sources.exists():
        first = sources.first()
        return {'object_type': 'correction', 'object_id': str(obj_id), 'provider_account_id': str(first.account_id), 'currency': 'USD', 'state': 'CONFIRMED', 'version': 1, 'sources': [source_dict(s) for s in sources]}
    raise ProviderError('Provider object not found', 'not_found', 404)


def history(object_id, after=0, snapshot=None):
    obj_id = valid_uuid(object_id)
    control = Control.objects.get(id=1)
    high_water = control.next_source_sequence - 1 if snapshot is None else integer(snapshot, 0, 2**63-1)
    if high_water > control.next_source_sequence - 1:
        raise ProviderError('History snapshot is beyond retained source high-water', 'snapshot_mismatch', 409)
    dispute = ProviderDispute.objects.filter(id=obj_id).first()
    payment_id = dispute.payment_id if dispute else obj_id
    query = EconomicSource.objects.filter(Q(object_id=obj_id) | Q(payment_id=payment_id) | Q(batch_id=obj_id), sequence__gt=after, sequence__lte=high_water).order_by('sequence')
    items = list(query[:501])
    page = items[:500]
    return {'schema_version': 1, 'snapshot': high_water, 'source_high_water': high_water,
            'sources': [source_dict(s) for s in page], 'next_after': page[-1].sequence if len(items) > 500 else None, 'complete': len(items) <= 500}


@transaction.atomic
def acknowledge(data):
    if set(data) != {'environment_id', 'main_instance_id', 'received_cursor'}:
        raise ProviderError('Invalid event acknowledgment')
    control = locked_control()
    if str(data['environment_id']) != control.environment_id or str(data['main_instance_id']) != control.main_instance_id:
        raise ProviderError('Event acknowledgment binding mismatch', 'binding_mismatch', 409)
    cursor = integer(data['received_cursor'], 0, control.next_event_sequence - 1)
    control.acknowledged_cursor = max(control.acknowledged_cursor, cursor)
    control.save(update_fields=['acknowledged_cursor'])
    return {'acknowledged_cursor': control.acknowledged_cursor}


def event_feed(after=0):
    control = Control.objects.filter(id=1).first()
    high_water = control.next_event_sequence - 1 if control else 0
    events = list(ProviderEvent.objects.filter(sequence__gt=after).order_by('sequence')[:500])
    return {'schema_version': 1, 'events': [{'sequence': event.sequence, **event.body} for event in events],
            'high_water': high_water, 'event_high_water': high_water, 'acknowledged_cursor': control.acknowledged_cursor if control else 0,
            'next_cursor': events[-1].sequence if events else after}

ECONOMIC_COLUMNS = ['schema_version', 'line_id', 'provider_account_id', 'event_type', 'source_id', 'batch_id', 'occurred_at', 'currency', 'amount_minor', 'reference']
BANK_COLUMNS = ['schema_version', 'line_id', 'bank_account_id', 'booked_at', 'currency', 'amount_minor', 'transfer_reference', 'description']
STATEMENT_KINDS = {'PROCESSOR_ECONOMIC', 'PROCESSOR_TRANSFER', 'BANK'}


@transaction.atomic
def statement(account_id, kind, period_start, period_end):
    if kind not in STATEMENT_KINDS:
        raise ProviderError('Unknown statement kind')
    start, end = parse_instant(period_start), parse_instant(period_end)
    if start >= end or end-start > timedelta(days=31) or end > now():
        raise ProviderError('Statement requires a completed half-open window of at most 31 days')
    control = locked_control()
    try:
        account = ProviderAccount.objects.select_for_update().get(id=valid_uuid(account_id), retired=False)
    except ProviderAccount.DoesNotExist:
        raise ProviderError('Provider account not found', 'not_found', 404)
    existing = list(StatementManifest.objects.filter(account=account, kind=kind, period_start=start, period_end=end).order_by('-revision'))
    rows = []
    if kind == 'PROCESSOR_ECONOMIC':
        eligible = EconomicSource.objects.filter(account=account, clearing_line=True, occurred_at__gte=start, occurred_at__lt=end)
        if EconomicSource.objects.filter(account=account,clearing_line=True,occurred_at__lt=end,batch__isnull=True).exists():
            raise ProviderError('Economic statement waits for all cutoff activity to be finalized in settlement batches', 'unbatched_sources', 409)
        opening = EconomicSource.objects.filter(account=account, clearing_line=True, occurred_at__lt=start).aggregate(total=Sum('amount_minor'))['total'] or 0
        for source in eligible.order_by('sequence'):
            rows.append({'schema_version': '1', 'line_id': str(source.id), 'provider_account_id': str(account.id),
                         'event_type': source.event_type, 'source_id': str(source.id), 'batch_id': str(source.batch_id),
                         'occurred_at': iso(source.occurred_at), 'currency': 'USD', 'amount_minor': str(source.amount_minor), 'reference': source.reference})
        columns = ECONOMIC_COLUMNS
    else:
        transfers = BankTransfer.objects.filter(account=account, occurred_at__gte=start, occurred_at__lt=end).order_by('occurred_at', 'id')
        opening = BankTransfer.objects.filter(account=account, occurred_at__lt=start).aggregate(total=Sum('amount_minor'))['total'] or 0
        for transfer in transfers:
            if kind == 'BANK':
                rows.append({'schema_version': '1', 'line_id': f'bank_{transfer.id.hex}', 'bank_account_id': account.bank_account_id,
                             'booked_at': iso(transfer.occurred_at), 'currency': 'USD', 'amount_minor': str(transfer.amount_minor),
                             'transfer_reference': transfer.reference, 'description': f'Simulated USD {transfer.kind.lower()}'})
            else:
                rows.append({'schema_version': '1', 'line_id': str(transfer.id), 'provider_account_id': str(account.id),
                             'event_type': transfer.kind, 'source_id': str(transfer.source_id), 'batch_id': str(transfer.batch_id) if transfer.batch_id else '',
                             'occurred_at': iso(transfer.occurred_at), 'currency': 'USD', 'amount_minor': str(transfer.amount_minor), 'reference': transfer.reference})
        if kind == 'PROCESSOR_TRANSFER':
            for batch in SettlementBatch.objects.filter(account=account, net_minor=0, created_at__gte=start, created_at__lt=end).order_by('created_at', 'id'):
                rows.append({'schema_version': '1', 'line_id': str(batch.id), 'provider_account_id': str(account.id), 'event_type': 'BATCH_ZERO',
                             'source_id': str(batch.id), 'batch_id': str(batch.id), 'occurred_at': iso(batch.created_at), 'currency': 'USD', 'amount_minor': '0', 'reference': f'batch_{batch.id.hex}'})
            rows.sort(key=lambda row: (row['occurred_at'], row['line_id']))
        columns = BANK_COLUMNS if kind == 'BANK' else ECONOMIC_COLUMNS
    if len(rows) > 100000:
        raise ProviderError('Statement exceeds supported row bound', 'statement_too_large', 413)
    manifest_id = uuid.uuid4()
    net = sum(int(row['amount_minor']) for row in rows)
    content = io.StringIO(newline='')
    writer = csv.DictWriter(content, fieldnames=columns, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    csv_bytes = content.getvalue().encode('utf-8')
    for prior in existing:
        if bytes(prior.csv_content)==csv_bytes and prior.manifest.get('opening_balance_minor')==opening and prior.manifest.get('closing_balance_minor')==opening+net:
            # Global later-period trading does not change this window's truth.
            return prior
    revision=max((prior.revision for prior in existing),default=0)+1
    window_hash=digest({'kind':kind,'provider_account_id':str(account.id),'period_start':iso(start),'period_end':iso(end),'opening_balance_minor':opening,'closing_balance_minor':opening+net,'csv_sha256':hashlib.sha256(csv_bytes).hexdigest()})
    manifest = {'schema_version': 1, 'policy_version': POLICY_VERSION, 'manifest_id': str(manifest_id),
                'statement_id': f'stmt_{manifest_id.hex}', 'provider_account_id': str(account.id),
                'account_id': account.bank_account_id if kind == 'BANK' else str(account.id),
                'bank_account_id': account.bank_account_id, 'kind': kind,
                'period_start': iso(start), 'period_end': iso(end), 'opening_balance_minor': opening,
                'closing_balance_minor': opening + net, 'row_count': len(rows), 'count': len(rows),
                'declared_row_count': len(rows), 'net_minor': net, 'declared_net_minor': net,
                'source_high_water': control.next_source_sequence - 1, 'rows': rows,
                'row_hashes': [{'line_id': row['line_id'], 'sha256': digest(row)} for row in rows],
                'content_hash': digest(rows), 'window_content_hash':window_hash,'revision':revision, 'csv_sha256': hashlib.sha256(csv_bytes).hexdigest()}
    return StatementManifest.objects.create(id=manifest_id, account=account, statement_id=manifest['statement_id'], kind=kind,
        period_start=start, period_end=end, source_high_water=manifest['source_high_water'], revision=revision, content_hash=window_hash, manifest=manifest, csv_content=csv_bytes)


@transaction.atomic
def progress_disputes():
    control = locked_control()
    at = now()
    ids = list(ProviderDispute.objects.filter(Q(state='OPEN', deadline__lte=at) | Q(state='RESPONDED', decision_at__lte=at)).order_by('deadline', 'id').values_list('id', flat=True)[:50])
    count = 0
    for dispute_id in ids:
        initial = ProviderDispute.objects.get(id=dispute_id)
        account = ProviderAccount.objects.select_for_update().get(id=initial.account_id)
        payment = ProviderPayment.objects.select_for_update().get(id=initial.payment_id)
        dispute = ProviderDispute.objects.select_for_update().get(id=dispute_id)
        if dispute.state not in {'OPEN', 'RESPONDED'}:
            continue
        won = dispute.state == 'RESPONDED' and dispute.selected_outcome == 'demo_won'
        dispute.state = 'WON' if won else 'LOST'
        dispute.version += 1
        if won:
            opening = EconomicSource.objects.get(object_id=dispute.id, event_type='DISPUTE_OPEN')
            create_source(control, account, dispute.id, 'DISPUTE_WON', dispute.principal_minor, at, payment_id=payment.id, dependencies=[str(opening.id)])
        dispute.save(update_fields=['state', 'version'])
        payment.version += 1
        payment.save(update_fields=['version'])
        emit(control, account, f'DISPUTE_{dispute.state}', dispute_snapshot(dispute), at)
        count += 1
    control.worker_heartbeat = at
    control.save()
    return count


@transaction.atomic
def daily_settlements():
    control = locked_control()
    at = now()
    cutoff = at.replace(hour=0, minute=0, second=0, microsecond=0)
    if at < cutoff + timedelta(minutes=5):
        return 0
    accounts = list(ProviderAccount.objects.filter(retired=False).order_by('id').values_list('id', flat=True))
    count = 0
    for account_id in accounts:
        account = ProviderAccount.objects.select_for_update().get(id=account_id)
        identity = f'daily_{cutoff.date().isoformat()}'
        if not SettlementBatch.objects.filter(account=account, cutoff_identity=identity).exists() and EconomicSource.objects.filter(account=account, clearing_line=True, batch__isnull=True, occurred_at__lt=cutoff).exists():
            flush_account(control, account, cutoff, identity, at)
            count += 1
    control.worker_heartbeat = at
    control.save()
    return count


def operation_inventory(after=0, snapshot=None):
    cutoff = parse_instant(snapshot) if snapshot else now()
    if cutoff > now():
        raise ProviderError('Inventory snapshot is in the future')
    records = list(ProviderOperation.objects.filter(created_at__lte=cutoff).order_by('created_at', 'key')[after:after+501])
    items = [{'operation_key': op.key, 'kind': op.kind, 'provider_account_id': str(op.account_id) if op.account_id else None,
              'object_id': str(op.object_id) if op.object_id else None, 'state': op.state,
              'payload_hash': op.payload_hash, 'payload': op.payload, 'created_at': iso(op.created_at)} for op in records[:500]]
    control = Control.objects.filter(id=1).first()
    return {'schema_version': 1, 'snapshot': iso(cutoff), 'operations': items,
            'next_after': after+500 if len(records)>500 else None,
            'event_high_water': control.next_event_sequence-1 if control else 0,
            'source_high_water': control.next_source_sequence-1 if control else 0,
            'simulator_instance_id': str(control.instance_id) if control else None,
            'environment_id': str(settings.MPRP_ENVIRONMENT_ID)}


def source_inventory(after=0, snapshot=None):
    control = Control.objects.get(id=1)
    high = control.next_source_sequence-1 if snapshot is None else integer(snapshot, 0, control.next_source_sequence-1)
    records = list(EconomicSource.objects.filter(sequence__gt=after, sequence__lte=high).order_by('sequence')[:501])
    return {'schema_version': 1, 'snapshot': high, 'sources': [source_dict(source) for source in records[:500]],
            'next_after': records[499].sequence if len(records)>500 else None,
            'source_high_water': high}


@transaction.atomic
def expire_authorizations():
    control = locked_control()
    at = now()
    payments = list(ProviderPayment.objects.select_for_update().filter(state='AUTHORIZED', authorization_expires_at__lte=at).order_by('authorization_expires_at')[:50])
    for payment in payments:
        payment.state = 'EXPIRED'
        payment.version += 1
        payment.save(update_fields=['state', 'version'])
        emit(control, payment.account, 'AUTHORIZATION_EXPIRED', payment_snapshot(payment), at)
    control.worker_heartbeat = at
    control.save()
    return len(payments)


@transaction.atomic
def mark_restored(operator,reason):
    if not isinstance(operator,str) or not 1<=len(operator)<=128 or not isinstance(reason,str) or not 1<=len(reason)<=2000:
        raise ProviderError('Bounded operator identity and restoration reason are required')
    control=locked_control()
    control.restored=True
    control.restore_marker_id=uuid.uuid4()
    control.restore_operator=operator
    control.restore_reason=reason
    control.restore_marked_at=now()
    control.dispatch_epoch+=1
    control.save()
    ControlAudit.objects.create(action='restoration.marked',operator_id=operator,reason=reason,summary={'marker_id':str(control.restore_marker_id),'dispatch_epoch':control.dispatch_epoch,'instance_id':str(control.instance_id)})
    return {'restore_marker_id':str(control.restore_marker_id),'dispatch_epoch':control.dispatch_epoch,'restored':True}


@transaction.atomic
def release_restoration(data):
    fields={'environment_id','main_instance_id','dispatch_epoch','restore_marker_id','inventory_hash','event_high_water','source_high_water','maker_id','checker_id','approval_id'}
    if set(data)!=fields:raise ProviderError('Invalid restoration release fields')
    control=locked_control()
    approval_id=valid_uuid(data['approval_id'])
    prior=ControlAudit.objects.filter(approval_id=approval_id,action='restoration.released').first()
    fingerprint=digest(data)
    if prior:
        if prior.summary.get('fingerprint')!=fingerprint:raise ProviderError('Release approval identity conflicts','key_conflict',409)
        return {'restored':control.restored,'approval_id':str(approval_id),'restore_marker_id':str(control.restore_marker_id)}
    if not control.restored or str(data['environment_id'])!=control.environment_id or str(data['main_instance_id'])!=control.main_instance_id or data['dispatch_epoch']!=control.dispatch_epoch or str(data['restore_marker_id'])!=str(control.restore_marker_id):
        raise ProviderError('Restoration marker or prepared environment differs','restore_conflict',409)
    maker=valid_uuid(data['maker_id']);checker=valid_uuid(data['checker_id'])
    if maker==checker:raise ProviderError('Recovery maker and finance checker must differ','self_approval',409)
    inventory_hash=data['inventory_hash']
    if not isinstance(inventory_hash,str) or len(inventory_hash)!=64 or any(ch not in '0123456789abcdef' for ch in inventory_hash):raise ProviderError('Inventory fingerprint is invalid')
    if data['event_high_water']!=control.next_event_sequence-1 or data['source_high_water']!=control.next_source_sequence-1 or control.acknowledged_cursor<data['event_high_water']:
        raise ProviderError('Inventory high-water or received acknowledgment changed','stale_inventory',409)
    ControlAudit.objects.create(approval_id=approval_id,action='restoration.released',operator_id=str(checker),reason='Distinct main finance approval of authenticated complete inventory',summary={'maker_id':str(maker),'checker_id':str(checker),'marker_id':str(control.restore_marker_id),'inventory_hash':inventory_hash,'fingerprint':fingerprint,'event_high_water':data['event_high_water'],'source_high_water':data['source_high_water']})
    control.restored=False
    control.save(update_fields=['restored'])
    return {'restored':False,'approval_id':str(approval_id),'restore_marker_id':str(control.restore_marker_id)}
