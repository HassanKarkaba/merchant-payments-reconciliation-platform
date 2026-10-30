"""Admit synthetic fixture effects through the real main worker and retained keys."""
import os
import secrets
import uuid
from django.db import transaction
from accounts.models import User
from merchants.models import Merchant
from operations.domain import admit_operation

def flush_settlement(actor,merchant):
    if not os.environ.get('MPRP_VERIFY_PROJECT','').startswith('mprp_verify_'):
        raise RuntimeError('Fixture controls require an isolated verification project')
    with transaction.atomic():
        actor=User.objects.select_for_update().get(pk=actor.pk)
        merchant=Merchant.objects.select_for_update().get(pk=merchant.pk)
        if not actor.is_active or actor.platform_role!='OPERATIONS':raise RuntimeError('Fixture operations authority is missing')
        return admit_operation(actor,merchant,'SETTLEMENT_FLUSH',uuid.uuid4(),secrets.token_hex(32),{})
