from contextlib import contextmanager
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.db import transaction
from django.shortcuts import get_object_or_404
from accounts.models import User
from .models import Merchant, Membership
PLATFORM_READ={'OPERATIONS','RECONCILIATION','FINANCE_APPROVER','AUDITOR'}
def require_merchant(user,merchant_id,roles=None,active=False):
    if not user.is_authenticated or not user.is_active: raise PermissionDenied
    merchant=get_object_or_404(Merchant,pk=merchant_id)
    if user.platform_role in PLATFORM_READ and roles is None: return merchant
    membership=Membership.objects.filter(merchant=merchant,user=user,active=True).first()
    if not membership: raise Http404
    if roles is not None and membership.role not in roles: raise PermissionDenied
    if active and merchant.status!='ACTIVE': raise ValidationError('This merchant is not active for new financial work.')
    return merchant

def require_role(user,merchant,roles,active=False):
    return require_merchant(user,merchant.pk,roles,active)

@contextmanager
def locked_authority(user,merchant,roles,active=True):
    with transaction.atomic():
        current=User.objects.select_for_update().get(pk=user.pk)
        locked=Merchant.objects.select_for_update().get(pk=merchant.pk)
        membership=Membership.objects.select_for_update().filter(user=current,merchant=locked,active=True).first()
        if not current.is_active or not current.verified or not current.totp_confirmed: raise PermissionDenied
        if not membership or membership.role not in roles: raise PermissionDenied
        if active and locked.status!='ACTIVE': raise ValidationError('Merchant must be active.')
        yield locked
