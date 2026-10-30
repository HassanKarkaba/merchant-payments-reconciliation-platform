"""Settled-funds availability and distinct-person payout controls."""
import hashlib
import json
import uuid
from datetime import timedelta
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from merchants.domain import locked_authority, require_role
from operations.audit import record
from payments.domain import FINANCE, money
from .models import Beneficiary, Payout, SettlementBatch, Transfer


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def expected_beneficiary_token(merchant):
    return 'acct_demo_' + merchant.provider_account_id.hex


def balances_and_availability(merchant, excluding_payout=None):
    from accounting.domain import balances
    from payments.models import Refund
    values = balances(merchant)
    payouts = Payout.objects.filter(merchant=merchant, reserved=True)
    if excluding_payout:
        payouts = payouts.exclude(pk=excluding_payout.pk)
    payout_reserved = payouts.aggregate(total=Sum('amount_minor'))['total'] or 0
    refund_reserved = Refund.objects.filter(merchant=merchant, reserved=True).aggregate(total=Sum('amount_minor'))['total'] or 0
    reserved = payout_reserved + refund_reserved
    return {**values, 'payout_reserved': payout_reserved, 'refund_reserved': refund_reserved, 'reserved': reserved, 'available_payout': max(0, min(values.get('merchant_payable', 0), values.get('bank_control', 0)) - reserved)}


def _blocked(merchant):
    from disputes.models import Dispute
    return Dispute.objects.filter(merchant=merchant, state__in=['OPEN', 'RESPONDED']).exists()


def propose_beneficiary(actor, merchant, token):
    if token != expected_beneficiary_token(merchant):
        raise ValidationError('Use the synthetic beneficiary token issued for this exact merchant.')
    with locked_authority(actor, merchant, FINANCE) as locked:
        version = (Beneficiary.objects.filter(merchant=locked).aggregate(last=Max('version'))['last'] or 0) + 1
        proposal = Beneficiary.objects.create(merchant=locked, token=token, version=version, maker=actor, payload_hash=fingerprint({'merchant_id': str(locked.pk), 'token': token, 'version': version}))
        record(actor, locked, 'beneficiary.proposed', str(proposal.pk), {'version': version})
        return proposal


def _lock_people(*ids):
    from accounts.models import User
    list(User.objects.select_for_update().filter(pk__in=sorted(set(ids))).order_by('pk'))


@transaction.atomic
def approve_beneficiary(actor, merchant, proposal_id, proposal_version, step_up_verified=False):
    if not step_up_verified:
        raise PermissionDenied('A fresh password and TOTP step-up is required.')
    maker_id = Beneficiary.objects.only('maker_id').get(pk=proposal_id, merchant=merchant).maker_id
    _lock_people(actor.pk, maker_id)
    with locked_authority(actor, merchant, ('OWNER',)) as locked:
        proposal = Beneficiary.objects.select_for_update().get(pk=proposal_id, merchant=locked)
        if proposal.maker_id == actor.pk:
            raise PermissionDenied('A different owner must approve this beneficiary.')
        require_role(proposal.maker, locked, FINANCE, active=True)
        if not proposal.maker.verified or not proposal.maker.totp_confirmed:
            raise PermissionDenied('The maker must retain verified account and authenticator authority.')
        if proposal.version != proposal_version or proposal.payload_hash != fingerprint({'merchant_id': str(locked.pk), 'token': proposal.token, 'version': proposal.version}):
            raise ValidationError('The beneficiary proposal changed. Reload and review it.', code='stale_version')
        if proposal.status == 'ACTIVE' and proposal.checker_id == actor.pk:
            return proposal
        if proposal.status != 'PROPOSED' or proposal.token != expected_beneficiary_token(locked):
            raise ValidationError('This proposal cannot be approved.')
        Beneficiary.objects.filter(merchant=locked, status='ACTIVE').update(status='SUPERSEDED')
        proposal.checker = actor
        proposal.status = 'ACTIVE'
        proposal.approved_at = timezone.now()
        proposal.save(update_fields=['checker', 'status', 'approved_at'])
        for payout in Payout.objects.select_for_update().filter(merchant=locked, status='APPROVED').exclude(beneficiary=proposal):
            payout.status = 'NEEDS_REAPPROVAL'
            payout.reserved = False
            payout.version += 1
            payout.save(update_fields=['status', 'reserved', 'version', 'updated_at'])
            if payout.operation_id:
                from operations.models import Operation
                Operation.objects.filter(pk=payout.operation_id, status='QUEUED').update(status='CANCELLED', completed_at=timezone.now())
        locked.funds_version += 1
        locked.save(update_fields=['funds_version'])
        record(actor, locked, 'beneficiary.approved', str(proposal.pk), {'maker_id': str(proposal.maker_id), 'version': proposal.version})
        return proposal


def _payout_payload(payout):
    return {'merchant_id': str(payout.merchant_id), 'amount_minor': payout.amount_minor, 'currency': 'USD', 'beneficiary_id': str(payout.beneficiary_id), 'beneficiary_version': payout.beneficiary_version, 'beneficiary_token': payout.beneficiary.token, 'reason': payout.reason}


def propose_payout(actor, merchant, amount_minor, beneficiary_version, reason, action_key, expected_version=None):
    money(amount_minor)
    if not reason.strip() or len(reason) > 2000:
        raise ValidationError('Provide a payout reason of up to 2,000 characters.')
    with locked_authority(actor, merchant, FINANCE) as locked:
        existing = Payout.objects.filter(merchant=locked, maker=actor, action_key=action_key).first()
        if existing:
            if existing.amount_minor != amount_minor or existing.beneficiary_version != beneficiary_version or existing.reason != reason.strip():
                raise ValidationError('This action key was used for a different payout.', code='key_conflict')
            return existing
        if expected_version is not None and expected_version != locked.version:
            raise ValidationError('Merchant settings changed. Review the payout again.', code='stale_version')
        beneficiary = Beneficiary.objects.select_for_update().filter(merchant=locked, status='ACTIVE', version=beneficiary_version).first()
        if not beneficiary:
            raise ValidationError('Choose the current approved beneficiary version.')
        if amount_minor > balances_and_availability(locked)['available_payout']:
            raise ValidationError('The proposed amount exceeds currently available settled simulated funds.')
        payout = Payout(merchant=locked, maker=actor, beneficiary=beneficiary, beneficiary_version=beneficiary.version, amount_minor=amount_minor, reason=reason.strip(), action_key=action_key, funds_version=locked.funds_version)
        payout.payload_hash = fingerprint(_payout_payload(payout))
        payout.save()
        record(actor, locked, 'payout.proposed', str(payout.pk), {'amount_minor': amount_minor, 'beneficiary_version': beneficiary.version})
        return payout


@transaction.atomic
def approve_payout(actor, merchant, payout_id, proposal_version, step_up_verified=False):
    from operations.domain import admit_operation
    if not step_up_verified:
        raise PermissionDenied('A fresh password and TOTP step-up is required.')
    maker_id = Payout.objects.only('maker_id').get(pk=payout_id, merchant=merchant).maker_id
    _lock_people(actor.pk, maker_id)
    with locked_authority(actor, merchant, ('OWNER',)) as locked:
        payout = Payout.objects.select_for_update().select_related('beneficiary', 'maker').get(pk=payout_id, merchant=locked)
        if payout.maker_id == actor.pk:
            raise PermissionDenied('A different owner must approve this payout.')
        require_role(payout.maker, locked, FINANCE, active=True)
        if not payout.maker.verified or not payout.maker.totp_confirmed:
            raise PermissionDenied('The maker must retain verified account and authenticator authority.')
        if payout.version != proposal_version or payout.payload_hash != fingerprint(_payout_payload(payout)):
            raise ValidationError('The immutable payout proposal changed. Review its current version.', code='stale_version')
        if payout.status == 'APPROVED' and payout.checker_id == actor.pk:
            return payout
        if payout.status not in ('PROPOSED', 'NEEDS_REAPPROVAL') or payout.beneficiary.status != 'ACTIVE' or payout.beneficiary.version != payout.beneficiary_version:
            raise ValidationError('The payout or beneficiary is no longer eligible for approval.')
        if _blocked(locked):
            raise ValidationError('An open dispute blocks payout approval.')
        if payout.amount_minor > balances_and_availability(locked, excluding_payout=payout)['available_payout']:
            raise ValidationError('Insufficient settled simulated funds after active reservations.')
        payout.checker = actor
        payout.approved_at = timezone.now()
        payout.approval_expires_at = payout.approved_at + timedelta(hours=24)
        payout.status = 'APPROVED'
        payout.reserved = True
        payout.funds_version = locked.funds_version
        payout.version += 1
        payout.save()
        # A new immutable operation identity is required when a prior unsent
        # approval was invalidated. The proposal itself remains unchanged.
        action_key = str(uuid.uuid5(payout.pk, 'approval:' + str(payout.version)))
        payload = {'payout_id': str(payout.pk), 'amount_minor': payout.amount_minor, 'currency': 'USD', 'beneficiary_token': payout.beneficiary.token, 'beneficiary_version': payout.beneficiary_version, 'provider_account_id': str(locked.provider_account_id)}
        op = admit_operation(actor=payout.maker, merchant=locked, kind='PAYOUT', resource_id=payout.pk, action_key=action_key, payload=payload)
        payout.operation = op
        payout.save(update_fields=['operation', 'updated_at'])
        locked.funds_version += 1
        locked.save(update_fields=['funds_version'])
        record(actor, locked, 'payout.approved', str(payout.pk), {'maker_id': str(payout.maker_id), 'amount_minor': payout.amount_minor, 'operation_id': str(op.pk)})
        return payout


def cancel_payout(actor, merchant, payout_id, expected_version=None):
    with locked_authority(actor, merchant, FINANCE, active=False) as locked:
        payout = Payout.objects.select_for_update().get(pk=payout_id, merchant=locked)
        if expected_version is not None and payout.version != expected_version:
            raise ValidationError('The payout changed. Reload before cancelling.', code='stale_version')
        if payout.maker_id != actor.pk:
            require_role(actor, locked, ('OWNER',))
        if payout.status == 'CANCELLED':
            return payout
        if payout.status not in ('PROPOSED', 'APPROVED', 'NEEDS_REAPPROVAL'):
            raise ValidationError('A sent or uncertain payout cannot be recalled. Inspect its result.')
        if payout.operation_id:
            from operations.models import Operation, Control
            if Control.objects.filter(pk=1, restore_hold=True).exists():
                raise ValidationError('Recovery hold prevents proving this payout was unsent. Inspect and resolve its original operation before cancellation.', code='capability_unavailable')
            op = Operation.objects.select_for_update().get(pk=payout.operation_id)
            if op.status != 'QUEUED':
                raise ValidationError('Dispatch has begun. This payout remains tracked to its authoritative result.')
            op.status = 'CANCELLED'
            op.completed_at = timezone.now()
            op.save(update_fields=['status', 'completed_at'])
        payout.status = 'CANCELLED'
        payout.reserved = False
        payout.version += 1
        payout.save(update_fields=['status', 'reserved', 'version', 'updated_at'])
        record(actor, locked, 'payout.cancelled', str(payout.pk))
        return payout


def prepare_operation(op):
    from operations.models import Operation
    from payments.models import Refund
    with locked_authority(op.actor, op.merchant, FINANCE) as merchant:
        payout = Payout.objects.select_for_update(of=('self',)).select_related('beneficiary', 'checker').get(pk=op.resource_id, merchant=merchant)
        if not payout.checker_id:
            raise ValidationError('Payout has no checker.')
        require_role(payout.checker, merchant, ('OWNER',), active=True)
        if not payout.checker.verified or not payout.checker.totp_confirmed:
            raise PermissionDenied('The checker no longer has complete account authority.')
        valid = payout.status == 'APPROVED' and payout.operation_id == op.pk and payout.reserved and payout.approval_expires_at and timezone.now() < payout.approval_expires_at and payout.beneficiary.status == 'ACTIVE' and payout.beneficiary.version == payout.beneficiary_version and payout.payload_hash == fingerprint(_payout_payload(payout)) and payout.maker_id != payout.checker_id
        if not valid or _blocked(merchant) or payout.amount_minor > balances_and_availability(merchant, excluding_payout=payout)['available_payout']:
            payout.status = 'NEEDS_REAPPROVAL'
            payout.reserved = False
            payout.version += 1
            payout.save(update_fields=['status', 'reserved', 'version', 'updated_at'])
            raise ValidationError('Payout guards changed; review and obtain a fresh approval.')
        from reconciliation.models import ReconciliationException
        if ReconciliationException.objects.filter(merchant=merchant, category='UNRECOGNIZED_CORRECTION').exclude(state__in=['RESOLVED', 'ACCEPTED_NONFINANCIAL']).exists():
            raise ValidationError('An unrecognized processor correction must be reviewed before payout dispatch.', code='temporarily_blocked')
        earlier = Payout.objects.filter(merchant=merchant, status='APPROVED', reserved=True).filter(Q(approved_at__lt=payout.approved_at) | Q(approved_at=payout.approved_at, id__lt=payout.pk)).exists()
        if earlier or Refund.objects.filter(merchant=merchant, reserved=True).exists() or Operation.objects.filter(merchant=merchant, status__in=('DISPATCHING', 'UNKNOWN')).exclude(pk=op.pk).exists():
            raise ValidationError('Payout is waiting for earlier approved or unresolved monetary work.', code='temporarily_blocked')
        payout.status = 'DISPATCHING'
        payout.version += 1
        payout.save(update_fields=['status', 'version', 'updated_at'])
        return dict(op.payload)


def complete_operation(op, snapshot):
    from accounting.domain import apply_economic_event
    payout = Payout.objects.select_for_update().get(pk=op.resource_id, merchant=op.merchant)
    snapshot = snapshot.get('snapshot', snapshot)
    if str(snapshot.get('provider_account_id')) != str(op.merchant.provider_account_id) or snapshot.get('currency') != 'USD' or str(snapshot.get('payout_id') or snapshot.get('object_id')) != str(payout.pk) or snapshot.get('amount_minor') != payout.amount_minor or snapshot.get('beneficiary_token') != payout.beneficiary.token:
        raise ValidationError('Processor payout result conflicts with the immutable approved payload.')
    sources = snapshot.get('sources', [])
    payout_sources = [source for source in sources if (source.get('event_type') or source.get('kind')) == 'PAYOUT']
    if len(payout_sources) != 1 or payout_sources[0].get('amount_minor') != -payout.amount_minor:
        raise ValidationError('The processor payout lacks one exact authoritative economic source.')
    source = payout_sources[0]
    apply_economic_event(op.merchant, source)
    occurred_at = parse_datetime(source['occurred_at']) if isinstance(source['occurred_at'], str) else source['occurred_at']
    Transfer.objects.get_or_create(reference=source.get('reference') or str(source.get('source_id') or source.get('id')), defaults={'merchant': op.merchant, 'payout': payout, 'kind': 'PAYOUT', 'amount_minor': -payout.amount_minor, 'state': 'SUCCEEDED', 'occurred_at': occurred_at})
    payout.status = 'PAID'
    payout.reserved = False
    payout.version += 1
    payout.save(update_fields=['status', 'reserved', 'version', 'updated_at'])
    record(op.actor, op.merchant, 'payout.paid', str(payout.pk), {'amount_minor': payout.amount_minor, 'operation_id': str(op.pk)})
    return payout


def fail_operation(op, reason='', cancelled=False):
    payout = Payout.objects.select_for_update().filter(pk=op.resource_id, merchant=op.merchant).first()
    if not payout:
        return
    if payout.status != 'NEEDS_REAPPROVAL':
        payout.status = 'CANCELLED' if cancelled else 'DECLINED'
    payout.reserved = False
    payout.version += 1
    payout.save(update_fields=['status', 'reserved', 'version', 'updated_at'])
    record(op.actor, op.merchant, 'payout.no_effect', str(payout.pk), {'reason': str(reason)[:500]})


def apply_batch(merchant, snapshot, actor=None):
    from accounting.domain import apply_economic_event
    snapshot = snapshot.get('snapshot', snapshot)
    if str(snapshot.get('provider_account_id')) != str(merchant.provider_account_id) or snapshot.get('currency') != 'USD':
        raise ValidationError('Settlement account does not match this merchant.')
    batch_id = snapshot.get('batch_id') or snapshot.get('object_id')
    sources = snapshot.get('sources', [])
    net_minor = snapshot.get('net_minor')
    if type(net_minor) is not int or not -(2**63) < net_minor < 2**63:
        raise ValidationError('Invalid signed settlement total.')
    occurred_at = snapshot.get('occurred_at') or snapshot.get('created_at') or timezone.now()
    occurred_at = parse_datetime(occurred_at) if isinstance(occurred_at, str) else occurred_at
    with transaction.atomic():
        batch, created = SettlementBatch.objects.get_or_create(provider_id=batch_id, defaults={'merchant': merchant, 'net_minor': net_minor, 'cutoff': str(snapshot.get('cutoff', '')), 'transfer_ref': str(snapshot.get('transfer_reference', snapshot.get('transfer_ref', ''))), 'status': snapshot.get('state', 'COMPLETED'), 'sources': sources, 'occurred_at': occurred_at})
        if batch.merchant_id != merchant.pk or batch.net_minor != net_minor:
            raise ValidationError('Settlement batch identity conflicts with prior evidence.')
        if not isinstance(sources, list) or any(not isinstance(source, dict) or source.get('currency') != 'USD' or str(source.get('provider_account_id')) != str(merchant.provider_account_id) or type(source.get('amount_minor')) is not int for source in sources):
            raise ValidationError('Settlement source representation or account is invalid.')
        economic = [source for source in sources if (source.get('event_type') or source.get('kind')) not in ('SETTLEMENT', 'SETTLEMENT_TRANSFER')]
        if any(str(source.get('batch_id')) != str(batch_id) for source in economic):
            raise ValidationError('Economic sources do not belong to this exact settlement batch.')
        transfers = [source for source in sources if (source.get('event_type') or source.get('kind')) in ('SETTLEMENT', 'SETTLEMENT_TRANSFER')]
        if (net_minor and len(transfers) != 1) or (not net_minor and transfers):
            raise ValidationError('The settlement transfer evidence is missing or inconsistent with a zero batch.')
        if sum(source.get('amount_minor', 0) for source in economic) != net_minor:
            raise ValidationError('Settlement batch detail does not equal its immutable net.')
        # A batch may arrive before payment callbacks. Authenticate the frozen
        # capture policy here too, before treating its clearing lines as money.
        from accounting.domain import fee_for
        from operations.models import Operation
        groups = {}
        for source in economic:
            kind = source.get('event_type') or source.get('kind')
            if kind in ('CAPTURE', 'FEE'):
                payment_id = str(source.get('payment_id') or source.get('object_id'))
                groups.setdefault(payment_id, {'CAPTURE': [], 'FEE': []})[kind].append(source)
        for payment_id, group in groups.items():
            if len(group['CAPTURE']) != 1 or len(group['FEE']) != 1:
                raise ValidationError('Settlement detail lacks one complete capture and fee pair.', code='fee_mismatch')
            gross = group['CAPTURE'][0]['amount_minor']
            fee = -group['FEE'][0]['amount_minor']
            if fee != fee_for(gross):
                raise ValidationError('Settlement fee conflicts with the selected capture policy.', code='fee_mismatch')
            if not Operation.objects.filter(merchant=merchant, kind='CAPTURE', status__in=['QUEUED', 'DISPATCHING', 'UNKNOWN', 'SUCCEEDED'], resource_id=payment_id, payload__amount_minor=gross, payload__fee_minor=fee, policy_version='mprp_v1').exists():
                raise ValidationError('Settlement capture has no matching immutable local capture admission.', code='missing_admission')
        for source in sorted(economic, key=lambda item: item.get('sequence', 0)):
            kind = source.get('event_type') or source.get('kind')
            if kind in ('DISPUTE_OPEN', 'DISPUTE_WON', 'DISPUTE_LOST'):
                from disputes.domain import apply_dispute_event
                apply_dispute_event(merchant, source)
            else:
                apply_economic_event(merchant, source)
        if net_minor == 0:
            from accounting.models import EconomicSource
            prior_zero = EconomicSource.objects.filter(merchant=merchant, provider_account_id=str(merchant.provider_account_id), source_id=str(batch_id), event_type='BATCH_ZERO', amount_minor=0).first()
            # The exact batch identity is authority for zero evidence. Retain
            # an earlier human-readable alias when replaying immutable history.
            zero_reference = prior_zero.reference if prior_zero else snapshot.get('reference') or 'batch_' + uuid.UUID(str(batch_id)).hex
            apply_economic_event(merchant, {'source_id': str(batch_id), 'event_type': 'BATCH_ZERO', 'amount_minor': 0, 'occurred_at': occurred_at, 'reference': zero_reference, 'provider_account_id': str(merchant.provider_account_id), 'batch_id': str(batch_id)})
        for source in sources:
            if (source.get('event_type') or source.get('kind')) in ('SETTLEMENT', 'SETTLEMENT_TRANSFER'):
                if source.get('amount_minor') != net_minor:
                    raise ValidationError('Settlement transfer differs from the batch net.')
                apply_economic_event(merchant, source)
                at = source.get('occurred_at', occurred_at)
                at = parse_datetime(at) if isinstance(at, str) else at
                ref = source.get('reference') or str(source.get('source_id') or source.get('id'))
                Transfer.objects.get_or_create(reference=ref, defaults={'merchant': merchant, 'batch': batch, 'kind': 'SETTLEMENT', 'amount_minor': net_minor, 'state': 'SUCCEEDED', 'occurred_at': at})
        if created:
            record(actor, merchant, 'settlement.observed', str(batch.pk), {'net_minor': net_minor})
        return batch


def reject_pre_dispatch(op, reason=''):
    payout = Payout.objects.select_for_update().filter(pk=op.resource_id, merchant=op.merchant).first()
    if payout and payout.status not in ('PAID', 'CANCELLED'):
        payout.status = 'NEEDS_REAPPROVAL'
        payout.reserved = False
        payout.version += 1
        payout.save(update_fields=['status', 'reserved', 'version', 'updated_at'])
        record(op.actor, op.merchant, 'payout.needs_reapproval', str(payout.pk), {'reason': str(reason)[:500]})


apply_settlement_snapshot = apply_batch
