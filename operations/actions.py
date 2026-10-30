"""Atomic, scoped repetition protection for local financial approvals."""
import hashlib
import json
import re
import uuid
from datetime import timedelta
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.http import Http404
from accounts.models import User
from merchants.models import Merchant, Membership
from .audit import record
from .models import ApprovalReceipt


class ActionConflict(ValidationError):
    def __init__(self, message='This action key was already used for different input.'):
        super().__init__(message, code='key_conflict')


def _now():
    with connection.cursor() as cursor:
        cursor.execute('SELECT clock_timestamp()')
        return cursor.fetchone()[0]


def _fingerprint(payload):
    def validate(value, depth=0):
        if depth > 6:
            raise ValidationError('Approval input is too deeply nested.')
        if value is None or type(value) is bool:
            return
        if type(value) is int and -(2**63) < value < 2**63:
            return
        if type(value) is str and len(value) <= 2000:
            return
        if type(value) is list and len(value) <= 100:
            for item in value:validate(item, depth + 1)
            return
        if type(value) is dict and len(value) <= 50 and all(type(key) is str and len(key) <= 64 for key in value):
            for item in value.values():validate(item, depth + 1)
            return
        raise ValidationError('Approval input has an unsupported type or bound.')
    if type(payload) is not dict:
        raise ValidationError('Approval input must be an object.')
    validate(payload)
    raw=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
    if len(raw)>16384:raise ValidationError('Approval input exceeds its bound.')
    return hashlib.sha256(raw).hexdigest()


def _require_integrity():
    with connection.cursor() as cursor:
        cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid=to_regclass(%s) AND tgname=%s AND NOT tgisinternal AND tgenabled IN ('O','A'))", ['operations_approvalreceipt','approval_receipt_immutable'])
        if not cursor.fetchone()[0]:
            raise ValidationError('Local approval integrity is unavailable. Restore the required database guard before retrying.', code='capability_unavailable')


def _reference(resource, merchant):
    if getattr(resource,'merchant_id',None)!=merchant.pk or not hasattr(resource,'_meta'):
        raise ValidationError('Approval result is outside its merchant scope.')
    return uuid.UUID(str(resource.pk)),resource._meta.label_lower


def local_action(actor,merchant,kind,resource_id,action_key,payload,perform,*,participants=(),required_role='OWNER',load_result=None):
    """Return the same resource for equivalent repeats, before version checks.

    ``perform(current_actor, locked_merchant)`` runs only for a new action and
    must contain fresh step-up and domain admission checks. ``load_result`` is
    called as (result_ref, current_actor, locked_merchant) for protected replay.
    Invalid or failed work leaves neither an approval receipt nor local effects.
    """
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,63}',str(kind)) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',str(action_key)):
        raise ValidationError('Refresh the approval form to obtain a valid action key.')
    try:
        resource_id=uuid.UUID(str(resource_id))
        actor_id=uuid.UUID(str(actor.pk))
        participant_ids={uuid.UUID(str(getattr(person,'pk',person))) for person in participants if person is not None}
        participant_ids.add(actor_id)
    except (ValueError,TypeError,AttributeError):
        raise ValidationError('Approval actor or resource identity is invalid.') from None
    if len(participant_ids)>20 or load_result is None:
        raise ValidationError('A bounded participant set and scoped result loader are required.')
    fingerprint=_fingerprint(payload)
    roles={required_role} if isinstance(required_role,str) else set(required_role)
    with transaction.atomic():
        people={person.pk:person for person in User.objects.select_for_update().filter(pk__in=sorted(participant_ids)).order_by('pk')}
        current=people.get(actor_id)
        if len(people)!=len(participant_ids) or not current or not current.is_active or not current.verified or not current.totp_confirmed or current.auth_epoch!=actor.auth_epoch:
            raise PermissionDenied('Current verified account and authenticator authority is required.')
        locked=Merchant.objects.select_for_update().get(pk=getattr(merchant,'pk',merchant))
        memberships=list(Membership.objects.select_for_update().filter(merchant=locked,user_id__in=sorted(participant_ids)).order_by('pk'))
        member=next((item for item in memberships if item.user_id==actor_id and item.active),None)
        platform_roles=roles-{'OWNER','FINANCE','ANALYST'}
        if not (current.platform_role in platform_roles or member and member.role in roles):
            if not member and roles <= {'OWNER','FINANCE','ANALYST'}:raise Http404
            raise PermissionDenied('Your current role cannot perform this approval.')
        from .models import Control
        from . import run_identity
        control=Control.objects.select_for_update().filter(pk=1).first()
        if not control or not run_identity.is_current(control):
            raise ValidationError('This app process is not qualified for the current financial run. Refresh operational readiness before retrying.',code='capability_unavailable')
        existing=ApprovalReceipt.objects.filter(actor=current,merchant=locked,kind=kind,resource_id=resource_id,action_key=str(action_key)).first()
        if existing:
            if existing.schema_version!=1 or existing.policy_version!='mprp_v1':
                raise ActionConflict('This retained approval uses an unsupported schema or policy.')
            if existing.fingerprint!=fingerprint:raise ActionConflict()
            if _now()>=existing.created_at+timedelta(days=90):
                raise ActionConflict('The retained response expired. Inspect the resource; this action key remains reserved.')
            result=load_result(existing.result_ref,current,locked)
            result_ref,result_model=_reference(result,locked)
            if result_ref!=existing.result_ref or result_model!=existing.result_model:
                raise ActionConflict('The retained approval result identity is inconsistent.')
            result._local_action_replayed=True
            result._local_action_receipt=existing.pk
            return result
        _require_integrity()
        from .protection import rate_limit
        rate_limit('financial-user:'+str(current.pk),30,60)
        result=perform(current,locked)
        result_ref,result_model=_reference(result,locked)
        receipt=ApprovalReceipt.objects.create(actor=current,merchant=locked,kind=kind,resource_id=resource_id,action_key=str(action_key),fingerprint=fingerprint,result_ref=result_ref,result_model=result_model,created_at=_now())
        record(current,locked,'local_action.completed',resource_id,{'kind':kind,'receipt_id':str(receipt.pk),'result_ref':str(result_ref)})
        result._local_action_replayed=False
        result._local_action_receipt=receipt.pk
        return result
