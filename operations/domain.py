import hashlib
import re
import secrets
from datetime import timedelta
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from .models import Operation, Job, Control, Notification, Outbox
from .audit import canonical, record

class Conflict(ValidationError):
    def __init__(self,message,*args,**kwargs):
        kwargs.setdefault('code','key_conflict');super().__init__(message,*args,**kwargs)

def action_key(): return secrets.token_hex(24)

def amount_minor(value,minimum=1,maximum=100000000):
    value=str(value).strip()
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?',value): raise ValidationError('Use a USD amount with at most two decimal places.')
    whole,_,fraction=value.partition('.')
    amount=int(whole)*100+int((fraction+'00')[:2])
    if not minimum<=amount<=maximum: raise ValidationError(f'Amount must be between {minimum} and {maximum} cents.')
    return amount

def enqueue(kind,merchant=None,payload=None,due_at=None):
    return Job.objects.create(kind=kind,merchant=merchant,payload=payload or {},due_at=due_at or timezone.now())

@transaction.atomic
def admit_operation(actor,merchant,kind,resource_id,action_key,payload,**kwargs):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{32,128}',str(action_key)): raise ValidationError('Refresh the form to obtain an action key.')
    fingerprint=hashlib.sha256(canonical(payload).encode()).hexdigest()
    existing=Operation.objects.filter(merchant=merchant,actor=actor,kind=kind,resource_id=resource_id,action_key=action_key).first()
    if existing:
        if existing.fingerprint!=fingerprint: raise Conflict('This action key was already used for different input.')
        return existing
    control=Control.objects.select_for_update().filter(pk=1).first()
    if not control or control.restore_hold or not control.dispatch_epoch: raise ValidationError('Financial admission is on recovery hold. Inspect operational status.',code='capability_unavailable')
    from .health import local_admission_ready
    if not local_admission_ready(control): raise ValidationError('Financial prerequisites are unavailable. Inspect operational status and retry when readiness is restored.',code='capability_unavailable')
    if Operation.objects.filter(merchant=merchant,status__in=['QUEUED','DISPATCHING','UNKNOWN']).count()>=100: raise ValidationError('This merchant has reached the active financial work limit.')
    if actor:
        from .protection import rate_limit
        rate_limit('financial-user:'+str(actor.pk),30,60)
    op=Operation.objects.create(actor=actor,merchant=merchant,kind=kind,resource_id=resource_id,action_key=action_key,payload=payload,fingerprint=fingerprint)
    enqueue('financial_dispatch',merchant,{'operation_id':str(op.pk)})
    record(actor,merchant,'operation.admitted',op.pk,{'kind':kind,'state':'QUEUED'})
    return op

def notify(user,title,url='',event_key=None,merchant=None):
    if not user.is_active:return None
    if merchant and (not user.verified or not (user.platform_role in ['OPERATIONS','RECONCILIATION','FINANCE_APPROVER','AUDITOR'] or user.memberships.filter(merchant=merchant,active=True,role__in=['OWNER','FINANCE']).exists())):return None
    return Notification.objects.get_or_create(recipient=user,event_key=event_key or secrets.token_hex(16),defaults={'title':title,'url':url,'merchant':merchant})[0]

@transaction.atomic
def mail(email,subject,body,expires_at=None,merchant=None):
    from accounts.domain import encrypt
    out=Outbox.objects.create(recipient=email,subject=subject,encrypted_body=encrypt(body),expires_at=expires_at or timezone.now()+timedelta(hours=24),merchant=merchant)
    enqueue('notification_send',payload={'outbox_id':str(out.pk)})
    return out
