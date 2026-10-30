import hashlib
import json
from django.db import transaction
from django.utils import timezone
from .models import AuditHead, AuditEvent

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,default=str)

@transaction.atomic
def record(actor=None,merchant=None,action='',resource_id='',summary=None,reason='',**kwargs):
    stream=str(merchant.pk) if merchant else 'platform'
    if merchant:
        # A deferred merchant FK check at commit also takes a row lock. Acquire
        # the control boundary before the audit head on every tenant path.
        from merchants.models import Merchant
        Merchant.objects.select_for_update().only('pk').get(pk=merchant.pk)
    AuditHead.objects.get_or_create(stream=stream)
    head=AuditHead.objects.select_for_update().get(pk=stream)
    values={'stream':stream,'sequence':head.sequence+1,'previous_hash':head.digest,'actor_id_safe':str(getattr(actor,'pk',actor) or 'service'),'merchant_id':str(merchant.pk) if merchant else None,'action':action,'resource_id':str(resource_id),'summary':summary or {},'reason':reason,'policy_version':'mprp_v1','created_at':timezone.now()}
    digest=hashlib.sha256(canonical(values).encode()).hexdigest()
    event=AuditEvent.objects.create(**values,event_hash=digest)
    head.sequence+=1;head.digest=digest;head.save(update_fields=['sequence','digest'])
    from .notifications import announce
    announce(action,resource_id,merchant,summary)
    return event

def inspect(stream):
    previous='0'*64
    for expected,event in enumerate(AuditEvent.objects.filter(stream=stream).order_by('sequence'),1):
        values={k:getattr(event,k) for k in ['stream','sequence','previous_hash','actor_id_safe','merchant_id','action','resource_id','summary','reason','policy_version','created_at']}
        if values['merchant_id']: values['merchant_id']=str(values['merchant_id'])
        if event.sequence!=expected or event.previous_hash!=previous or hashlib.sha256(canonical(values).encode()).hexdigest()!=event.event_hash:return False
        previous=event.event_hash
    head=AuditHead.objects.filter(pk=stream).first()
    return not head or (head.digest==previous)
