import hashlib
import json
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from accounting.domain import apply_economic_event
from .models import Dispute, DisputeResponse


def has_open_dispute(payment=None,merchant=None):
    query=Dispute.objects.filter(state__in=['OPEN','RESPONDED'])
    if payment is not None:query=query.filter(payment=payment)
    if merchant is not None:query=query.filter(merchant=merchant)
    return query.exists()


def lost_principal(payment):
    return Dispute.objects.filter(payment=payment,state='LOST').aggregate(n=Sum('principal_minor'))['n'] or 0


def _time(value):
    result=parse_datetime(value) if isinstance(value,str) else value
    if result is None or timezone.is_naive(result):raise ValidationError('Dispute snapshot has an invalid timestamp.')
    return result


@transaction.atomic
def apply_dispute_event(merchant,event):
    from merchants.models import Merchant
    from payments.models import Payment
    from operations.audit import record
    Merchant.objects.select_for_update().get(pk=merchant.pk)
    data=event.get('payload',event)
    if data.get('snapshot'):data=data['snapshot']
    provider_id=str(data.get('dispute_id') or data.get('object_id') or data.get('id') or '')
    payment_id=str(data.get('payment_id') or '')
    payment=Payment.objects.select_for_update().filter(merchant=merchant,provider_id=payment_id).first()
    if not payment:
        try:payment=Payment.objects.select_for_update().filter(merchant=merchant,pk=payment_id).first()
        except (ValueError,ValidationError):payment=None
    if not payment:raise ValidationError('Dispute payment history must be reconstructed before the case.')
    sources=data.get('sources',[])
    for source in sorted(sources,key=lambda s:s.get('sequence',0)):
        apply_economic_event(merchant,source)
    kind=data.get('event_type') or data.get('kind')
    if kind in ('DISPUTE_OPEN','DISPUTE_WON') and data.get('amount_minor') is not None:
        apply_economic_event(merchant,data)
    principal=data.get('principal_minor')
    if principal is None and kind=='DISPUTE_OPEN':principal=abs(data['amount_minor'])
    if isinstance(principal,bool) or not isinstance(principal,int) or principal<=0:raise ValidationError('Dispute principal must be positive integer cents.')
    state=data.get('state') or {'DISPUTE_OPEN':'OPEN','DISPUTE_WON':'WON','DISPUTE_LOST':'LOST'}.get(kind,'OPEN')
    if state not in ('OPEN','RESPONDED','WON','LOST'):raise ValidationError('Unsupported dispute state.')
    version=data.get('provider_version',data.get('version',1))
    opened=_time(data.get('opened_at') or data.get('occurred_at'))
    deadline=_time(data.get('deadline'))
    dispute,created=Dispute.objects.get_or_create(payment=payment,defaults={'merchant':merchant,'provider_id':provider_id,'principal_minor':principal,'state':state,'provider_version':version,'opened_at':opened,'deadline':deadline,'response_version':data.get('response_version',0)})
    if dispute.provider_id!=provider_id or dispute.principal_minor!=principal:raise ValidationError('Conflicting immutable dispute identity or principal.')
    if not created and version>dispute.provider_version:
        if dispute.state in ('WON','LOST') and state!=dispute.state:raise ValidationError('A terminal dispute outcome cannot change.')
        dispute.state=state;dispute.provider_version=version;dispute.response_version=data.get('response_version',dispute.response_version);dispute.version+=1
        dispute.save(update_fields=['state','provider_version','response_version','version','updated_at'])
    record(actor=None,merchant=merchant,action='dispute.observe',resource_id=str(dispute.pk),summary={'state':dispute.state,'provider_version':dispute.provider_version})
    return dispute


apply_dispute_snapshot=apply_dispute_event


@transaction.atomic
def submit_response(actor,dispute,text,action_key,expected_version=None):
    from merchants.domain import require_merchant
    from merchants.models import Merchant
    from operations.domain import admit_operation
    from operations.audit import record
    from accounts.models import User
    actor=User.objects.select_for_update().get(pk=actor.pk)
    require_merchant(actor,dispute.merchant_id,roles=['OWNER','FINANCE'],active=True)
    Merchant.objects.select_for_update().get(pk=dispute.merchant_id)
    dispute=Dispute.objects.select_for_update().get(pk=dispute.pk)
    if not 1<=len(text.strip())<=2000:raise ValidationError('Response must be 1 through 2000 plain-text characters.')
    import uuid
    from operations.models import Operation
    response_id=uuid.uuid5(uuid.NAMESPACE_URL,f'mprp-dispute-response:{dispute.pk}:{actor.pk}:{action_key}')
    existing=DisputeResponse.objects.filter(pk=response_id).first()
    if existing:
        if existing.text!=text:raise ValidationError('This action key was used for different response text.',code='key_conflict')
        return Operation.objects.get(merchant=dispute.merchant,actor=actor,kind='DISPUTE_RESPONSE',resource_id=response_id,action_key=action_key)
    if expected_version is not None and expected_version!=dispute.version:raise ValidationError('This dispute changed. Refresh before responding.',code='stale_version')
    if dispute.state not in ('OPEN','RESPONDED') or timezone.now()>=dispute.deadline:raise ValidationError('This dispute is no longer accepting a response.')
    payload={'dispute_id':dispute.provider_id,'text':text,'expected_version':dispute.provider_version}
    hashed=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    # A deterministic local row identity makes concurrent replays converge before creating jobs.
    response,created=DisputeResponse.objects.get_or_create(pk=response_id,defaults={'dispute':dispute,'actor':actor,'text':text,'payload_hash':hashed})
    if not created and response.payload_hash!=hashed:raise ValidationError('This action key was already used for a different response.')
    operation=admit_operation(actor,dispute.merchant,'DISPUTE_RESPONSE',response.pk,action_key,payload)
    if created:record(actor=actor,merchant=dispute.merchant,action='dispute.response_admit',resource_id=str(response.pk),summary={'dispute_id':str(dispute.pk)})
    return operation


def dispatch_response(operation):
    from merchants.domain import require_merchant
    from operations.provider import request
    response=DisputeResponse.objects.select_related('dispute','actor').get(pk=operation.resource_id)
    require_merchant(response.actor,response.dispute.merchant_id,roles=['OWNER','FINANCE'],active=True)
    # Provider repeats the exact seven-day boundary check atomically.
    payload={'dispute_id':response.dispute.provider_id,'text':response.text,'expected_version':response.dispute.provider_version}
    return request('POST','/v1/disputes/respond/',payload)


@transaction.atomic
def apply_response_result(operation,snapshot):
    response=DisputeResponse.objects.select_for_update().select_related('dispute').get(pk=operation.resource_id)
    if snapshot.get('status') in ('FAILED','REJECTED','NO_EFFECT'):
        response.status='FAILED';response.save(update_fields=['status']);return response
    data=snapshot.get('result',snapshot)
    dispute=apply_dispute_event(response.dispute.merchant,data)
    response.status='ACCEPTED';response.accepted_at=timezone.now();response.provider_version=dispute.provider_version;response.save(update_fields=['status','accepted_at','provider_version'])
    dispute.response_text=response.text
    if not dispute.first_responded_at:dispute.first_responded_at=response.accepted_at
    dispute.save(update_fields=['response_text','first_responded_at','updated_at'])
    return response


def prepare_operation(operation):
    from merchants.domain import locked_authority
    response=DisputeResponse.objects.select_related('dispute','actor').get(pk=operation.resource_id)
    with locked_authority(response.actor,response.dispute.merchant,['OWNER','FINANCE'],active=True):
        dispute=Dispute.objects.select_for_update().get(pk=response.dispute_id)
        if dispute.state not in ('OPEN','RESPONDED') or timezone.now()>=dispute.deadline:
            raise ValidationError('The dispute response window has ended.')
        return dict(operation.payload)


def complete_operation(operation,snapshot):
    return apply_response_result(operation,snapshot)


def fail_operation(operation,reason='',cancelled=False):
    response=DisputeResponse.objects.select_for_update().get(pk=operation.resource_id)
    response.status='CANCELLED' if cancelled else 'FAILED'
    response.save(update_fields=['status'])
    return response
