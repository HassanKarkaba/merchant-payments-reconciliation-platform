"""Durable, authorized synthetic controls use the same effect boundary as merchant work."""
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction
from accounts.models import User
from merchants.models import Merchant
from .audit import record
KINDS={'DISPUTE_OPEN','SETTLEMENT_FLUSH','CORRECTION'}

def prepare_operation(op):
    current=User.objects.select_for_update().get(pk=op.actor_id)
    merchant=Merchant.objects.select_for_update().get(pk=op.merchant_id)
    if not current.is_active or not current.verified or not current.totp_confirmed or current.platform_role!='OPERATIONS':raise PermissionDenied('Current operations authority is required.')
    if merchant.status not in ['ACTIVE','SUSPENDED','CLOSING']:raise ValidationError('This namespace is not available for synthetic effects.')
    return op.payload

def complete_operation(op,snapshot):
    if op.kind=='SETTLEMENT_FLUSH':
        from settlements.domain import apply_batch
        apply_batch(op.merchant,snapshot,op.actor)
    elif op.kind=='DISPUTE_OPEN':
        from disputes.domain import apply_dispute_snapshot
        apply_dispute_snapshot(op.merchant,snapshot)
    elif op.kind=='CORRECTION':
        from accounting.domain import apply_economic_event
        for source in snapshot.get('sources',[]):apply_economic_event(op.merchant,source)
    else:raise ValidationError('Unknown synthetic operation.')
    record(op.actor,op.merchant,'sandbox.result',op.pk,{'kind':op.kind})

def fail_operation(op,reason='',cancelled=False):
    record(op.actor,op.merchant,'sandbox.no_effect',op.pk,{'kind':op.kind,'cancelled':cancelled})
