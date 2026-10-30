import hashlib
import hmac
import json
import secrets
import time
from datetime import timedelta
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction,IntegrityError
from django.http import JsonResponse,HttpResponse,Http404
from django.shortcuts import render,redirect,get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST,require_GET
from merchants.domain import require_merchant
from merchants.authz import require_platform
from merchants.models import Merchant,Membership
from .models import *
from .audit import record
from .domain import enqueue
from .health import capabilities

@login_required
def home(request):
    memberships=request.user.memberships.filter(active=True).select_related('merchant')
    if memberships.count()==1 and not request.user.platform_role:return redirect('merchants:overview',merchant_id=memberships.first().merchant_id)
    return render(request,'operations/home.html',{'memberships':memberships})

@login_required
def merchant_queue(request):
    require_platform(request,['OPERATIONS','AUDITOR'])
    query=Merchant.objects.order_by('-created_at')
    if request.GET.get('status'):query=query.filter(status=request.GET['status'])
    return render(request,'operations/merchants.html',{'merchants':query[:100]})

@login_required
@require_POST
def merchant_action(request,merchant_id,action):
    require_platform(request,['OPERATIONS']);reason=request.POST.get('reason','').strip()
    if not reason or len(reason)>2000:raise PermissionDenied('A bounded reason is required.')
    merchant=get_object_or_404(Merchant,pk=merchant_id)
    if action in ['activate','resume']:
        from . import provider
        # Account provisioning is independently idempotent; no main lock spans HTTP.
        provider.request('POST','/v1/accounts/',provider.envelope(merchant,'account-'+merchant.pk.hex,{'merchant_id':str(merchant.pk)}))
    with transaction.atomic():
        from accounts.models import User
        current_actor=User.objects.select_for_update().get(pk=request.user.pk)
        if not current_actor.is_active or not current_actor.verified or not current_actor.totp_confirmed or current_actor.platform_role!='OPERATIONS' or current_actor.auth_epoch!=request.session.get('auth_epoch'):
            raise PermissionDenied('Current platform operations authority is required.')
        merchant=Merchant.objects.select_for_update().get(pk=merchant_id)
        if action=='activate' and merchant.status=='PENDING':merchant.status='ACTIVE'
        elif action=='suspend' and merchant.status=='ACTIVE':merchant.status='SUSPENDED'
        elif action=='resume' and merchant.status=='SUSPENDED':merchant.status='ACTIVE'
        elif action=='close' and merchant.status=='CLOSING':
            from .lifecycle import closure_blockers
            blockers=closure_blockers(merchant)
            if blockers:
                raise PermissionDenied('Closure is blocked: '+' '.join(blockers))
            merchant.status='CLOSED';merchant.closed_at=timezone.now()
            from accounts.models import AuthToken
            AuthToken.objects.filter(merchant=merchant,kind='invite',used_at=None).update(used_at=timezone.now())
            merchant.memberships.exclude(role='OWNER').update(active=False)
            from payments.models import CheckoutLink
            CheckoutLink.objects.filter(merchant=merchant,status='ACTIVE').update(status='REVOKED')
        else:raise PermissionDenied('This transition is not available.')
        merchant.version+=1;merchant.status_reason=reason;merchant.save();record(request.user,merchant,'merchant.'+action,merchant.pk,{'status':merchant.status},reason=reason)
    return redirect('operations:merchants')

@login_required
def status(request):
    require_platform(request,['OPERATIONS','RECONCILIATION','FINANCE_APPROVER','AUDITOR'])
    return render(request,'operations/status.html',{'capabilities':capabilities(),'control':Control.objects.first(),'jobs':Job.objects.exclude(status='DONE').order_by('due_at')[:50],'unknown':Operation.objects.filter(status='UNKNOWN').count(),'sandbox_merchants':sandbox_controls(request),'sandbox_payments':sandbox_payments(request)})

@login_required
def audit(request):
    require_platform(request,['OPERATIONS','RECONCILIATION','FINANCE_APPROVER','AUDITOR'])
    query=AuditEvent.objects.select_related('merchant')
    if request.GET.get('merchant'):query=query.filter(merchant_id=request.GET['merchant'])
    if request.GET.get('action'):query=query.filter(action__icontains=request.GET['action'][:100])
    from django.core.paginator import Paginator
    return render(request,'operations/audit.html',{'page':Paginator(query,50).get_page(request.GET.get('page'))})

@login_required
def notifications(request):
    from .notifications import visible_notifications
    return render(request,'operations/notifications.html',{'notifications':visible_notifications(request.user).order_by('-created_at')[:100]})
@login_required
@require_POST
def notification_read(request,notification_id):
    from .notifications import visible_notifications
    notice=get_object_or_404(visible_notifications(request.user),pk=notification_id)
    notice.read_at=timezone.now();notice.save(update_fields=['read_at']);return redirect('operations:notifications')

@login_required
def reports(request,merchant_id):
    merchant=require_merchant(request.user,merchant_id)
    from accounting.domain import balances
    from accounting.models import Journal,EconomicSource,AccountingPeriod
    from payments.models import Payment,Refund
    from settlements.models import Payout
    from django.db.models import Sum,Max
    totals=EconomicSource.objects.filter(merchant=merchant).values('event_type').annotate(total=Sum('amount_minor')).order_by('event_type')
    return render(request,'operations/reports.html',{'merchant':merchant,'balances':balances(merchant),'totals':totals,'cutoff':merchant.ledger_sequence,'generated_at':timezone.now(),'exports':Export.objects.filter(merchant=merchant,requester=request.user).order_by('-created_at')[:20],'periods':AccountingPeriod.objects.filter(merchant=merchant)[:12]})

@login_required
@require_POST
def export_create(request,merchant_id=None):
    from accounts.models import User
    merchant=require_merchant(request.user,merchant_id) if merchant_id else None
    with transaction.atomic():
        actor=User.objects.select_for_update().get(pk=request.user.pk)
        if not actor.is_active:raise PermissionDenied
        if merchant:
            merchant=Merchant.objects.select_for_update().get(pk=merchant.pk)
            require_merchant(actor,merchant.pk)
            from reconciliation.models import StatementImport
            active=Export.objects.filter(merchant=merchant,status__in=['QUEUED','RETRYING']).count()+StatementImport.objects.filter(merchant=merchant,status__in=['STAGED','VALIDATED']).count()
        else:active=Export.objects.filter(requester=actor,merchant=None,status__in=['QUEUED','RETRYING']).count()
        if active>=10:raise PermissionDenied('The active import and export limit has been reached. Inspect existing work before creating another request.')
        export=Export.objects.create(requester=actor,merchant=merchant,kind='ledger' if merchant else 'account')
        enqueue('export_build',merchant,{'export_id':str(export.pk)});record(actor,merchant,'export.admitted',export.pk)
    return redirect('operations:export_detail',export_id=export.pk)

@login_required
def export_detail(request,export_id,download=False):
    export=get_object_or_404(Export,pk=export_id,requester=request.user)
    if export.merchant:require_merchant(request.user,export.merchant_id)
    if download:
        if export.status!='READY' or not export.expires_at or timezone.now()>=export.expires_at:raise Http404
        record(request.user,export.merchant,'export.retrieved',export.pk)
        response=HttpResponse(bytes(export.content),content_type='text/csv; charset=utf-8');response['Content-Disposition']='attachment; filename="sandbox-report.csv"';return response
    return render(request,'operations/export.html',{'export':export,'merchant':export.merchant})

@require_GET
def live(request):return JsonResponse({'live':True})
@require_GET
def ready(request):
    result=capabilities();return JsonResponse(result,status=200 if all(result.values()) else 503)

@csrf_exempt
@require_POST
def callback(request):
    raw=request.body
    if len(raw)>256*1024:return JsonResponse({'code':'body_too_large'},status=413)
    stamp=request.headers.get('X-MPRP-Timestamp','');nonce=request.headers.get('X-MPRP-Nonce','');signature=request.headers.get('X-MPRP-Signature','')
    try:valid=abs(time.time()-float(stamp))<=300
    except ValueError:valid=False
    content='\n'.join(['POST',request.get_full_path(),stamp,nonce,hashlib.sha256(raw).hexdigest()])
    from config.security import verification_secret
    transport_secret=verification_secret(settings.CALLBACK_VERIFICATION_KEYS,request.headers.get('X-MPRP-Key-Id',''))
    expected=hmac.new((transport_secret or '').encode(),content.encode(),hashlib.sha256).hexdigest()
    if not valid or not transport_secret or not 32<=len(nonce)<=128 or not secrets.compare_digest(signature,expected):return JsonResponse({'code':'invalid_auth'},status=401)
    try:
        event=json.loads(raw)
        required={'event_id','event_type','object_type','object_id','provider_account_id','environment_id','provider_version','occurred_at','payload','schema_version'}
        if not isinstance(event,dict) or set(event)!=required or type(event['schema_version']) is not int or event['schema_version']!=1 or str(event['environment_id'])!=settings.MPRP_ENVIRONMENT_ID:return JsonResponse({'code':'invalid_input'},status=400)
        import uuid
        from django.utils.dateparse import parse_datetime
        for field in ['event_id','object_id','provider_account_id']:uuid.UUID(str(event[field]))
        instant=parse_datetime(event['occurred_at'])
        if not isinstance(event['payload'],dict) or type(event['provider_version']) is not int or event['provider_version']<1 or instant is None or timezone.is_naive(instant):return JsonResponse({'code':'invalid_input'},status=400)
        with transaction.atomic():
            # Receipt audit shares the financial control order. Taking the audit
            # stream first would deadlock with a result holding this merchant.
            merchant=Merchant.objects.select_for_update().filter(provider_account_id=event['provider_account_id']).first()
            if not merchant:raise ValidationError('Unknown callback account')
            Nonce.objects.filter(expires_at__lte=timezone.now()).delete()
            Nonce.objects.create(value=nonce,expires_at=timezone.now()+timedelta(minutes=10))
            from .worker import admit_event
            admit_event(event)
            record(None,merchant,'callback.received',event['event_id'])
    except IntegrityError:return JsonResponse({'code':'replayed_transport'},status=401)
    except (ValueError,TypeError,KeyError):return JsonResponse({'code':'invalid_input'},status=400)
    except ValidationError:
        record(None,None,'callback.rejected',str(event.get('event_id',''))[:100],{'reason':'Conflicting identity or invalid source scope'})
        return JsonResponse({'code':'event_conflict'},status=409)
    return JsonResponse({'accepted':True},status=202)

@login_required
@require_POST
def sandbox_action(request,action,payment_id=None):
    import uuid
    from accounts.models import User
    from .domain import admit_operation
    require_platform(request,['OPERATIONS'])
    merchant=get_object_or_404(Merchant,pk=request.POST.get('merchant_id'))
    key=request.POST.get('action_key','')
    try:
        if action=='open-dispute':
            from payments.models import Payment
            payment=get_object_or_404(Payment,pk=payment_id,merchant=merchant)
            payload={'payment_id':str(payment.pk),'outcome':request.POST.get('outcome','demo_won')};kind='DISPUTE_OPEN';resource=payment.pk
            if payload['outcome'] not in ['demo_won','demo_lost']:raise ValidationError('Select a synthetic outcome.')
        elif action=='flush':payload={};kind='SETTLEMENT_FLUSH';resource=uuid.uuid5(merchant.pk,'flush:'+key)
        elif action=='correction':
            try:amount=int(request.POST.get('amount_minor','0'))
            except ValueError:raise ValidationError('Enter signed integer USD cents.')
            reason=request.POST.get('reason','').strip()
            if not 1<=abs(amount)<=100000000 or not 1<=len(reason)<=2000:raise ValidationError('Use nonzero bounded cents and a correction reason.')
            resource=uuid.uuid5(merchant.pk,'correction:'+key);payload={'amount_minor':amount,'reason':reason,'correction_id':str(resource)};kind='CORRECTION'
            if request.POST.get('original_source_id'):payload['original_source_id']=str(uuid.UUID(request.POST['original_source_id']))
        else:raise Http404
        with transaction.atomic():
            actor=User.objects.select_for_update().get(pk=request.user.pk)
            merchant=Merchant.objects.select_for_update().get(pk=merchant.pk)
            if not actor.is_active or not actor.verified or not actor.totp_confirmed or actor.platform_role!='OPERATIONS':raise PermissionDenied
            if str(merchant.version)!=request.POST.get('expected_version'):raise ValidationError('Merchant state changed; reload the controls.')
            if merchant.status not in ['ACTIVE','SUSPENDED','CLOSING']:raise ValidationError('This namespace is not available for synthetic effects.')
            op=admit_operation(actor,merchant,kind,resource,key,payload)
        messages.success(request,'Synthetic processor work was durably accepted. Inspect its saved outcome.')
        return redirect('operations:operation_detail',merchant_id=merchant.pk,operation_id=op.pk)
    except (ValidationError,ValueError) as error:
        messages.error(request,' '.join(error.messages) if isinstance(error,ValidationError) else 'Invalid synthetic identifier.')
        return redirect('operations:status')

def operation_version(operation):
    return 1+operation.attempts*10+{'QUEUED':0,'DISPATCHING':1,'UNKNOWN':2,'SUCCEEDED':3,'FAILED':3,'CANCELLED':3}.get(operation.status,0)


@login_required
def operation_detail(request,merchant_id,operation_id,json_status=False):
    merchant=require_merchant(request.user,merchant_id)
    op=get_object_or_404(Operation,pk=operation_id,merchant=merchant)
    messages_by_state={'QUEUED':'Accepted and waiting for processing.','DISPATCHING':'Processing has started. The outcome is not yet recorded.','UNKNOWN':'The processor may have completed this action. Its existing key is being resolved; reservations remain protected.','SUCCEEDED':'The processor confirmed success and the local financial result was recorded.','FAILED':'The processor confirmed that this action had no successful effect.','CANCELLED':'This action was cancelled before dispatch.'}
    business_state=''
    if op.kind in ['AUTHORIZE','CAPTURE','VOID','REFUND']:
        from payments.models import Payment
        payment=Payment.objects.filter(pk=op.payload.get('payment_id',op.resource_id),merchant=merchant).first()
        if payment:business_state=payment.state
    elif op.kind=='PAYOUT':
        from settlements.models import Payout
        payout=Payout.objects.filter(pk=op.resource_id,merchant=merchant).first()
        if payout:business_state=payout.status
    from payments.forms import ActionForm
    from .domain import action_key
    message=messages_by_state.get(op.status,'Inspect the retained operation for its current result.')
    cancel_form=ActionForm(auto_id='operation_cancel_%s',initial={'action_key':action_key(),'expected_version':operation_version(op)})
    if json_status:return JsonResponse({'schema_version':1,'operation_id':str(op.pk),'state':op.status,'business_state':business_state,'outcome_known':op.status in ['SUCCEEDED','FAILED','CANCELLED'],'safe_message':message,'updated_at':op.updated_at.isoformat(),'version':operation_version(op),'inspection_url':f'/m/{merchant.pk}/operations/{op.pk}/','retry_after_seconds':3})
    return render(request,'operations/operation.html',{'merchant':merchant,'operation':op,'business_state':business_state,'safe_message':message,'cancel_form':cancel_form,'can_cancel':op.kind in ['AUTHORIZE','CAPTURE','VOID','REFUND','PAYOUT','DISPUTE_RESPONSE'] and merchant.memberships.filter(user=request.user,active=True,role__in=['OWNER','FINANCE']).exists()})

@login_required
@require_POST
def operation_cancel(request,merchant_id,operation_id):
    from payments.forms import ActionForm
    from .actions import local_action,ActionConflict
    merchant=require_merchant(request.user,merchant_id,('OWNER','FINANCE'))
    hint=get_object_or_404(Operation,pk=operation_id,merchant=merchant)
    participants=[hint.actor_id] if hint.actor_id else []
    if hint.kind=='PAYOUT':
        from settlements.models import Payout
        payout=get_object_or_404(Payout,pk=hint.resource_id,merchant=merchant)
        if payout.checker_id:participants.append(payout.checker_id)
    form=ActionForm(request.POST)
    if not form.is_valid():return HttpResponse('Invalid cancellation form. Refresh the saved operation.',status=400)
    def perform(actor,locked):
        op=get_object_or_404(Operation.objects.select_for_update(),pk=operation_id,merchant=locked)
        if op.kind not in ['AUTHORIZE','CAPTURE','VOID','REFUND','PAYOUT','DISPUTE_RESPONSE']:raise PermissionDenied('Platform processor work is controlled by platform operations.')
        control=Control.objects.first()
        if not control or control.restore_hold and op.status=='QUEUED':raise ValidationError('Recovery inventory must establish whether this restored action was sent before cancellation can release funds.',code='capability_unavailable')
        if operation_version(op)!=form.cleaned_data['expected_version']:raise ActionConflict('The operation changed. Inspect its saved outcome before trying again.')
        if op.kind=='PAYOUT' and actor.pk!=op.actor_id and not locked.memberships.filter(user=actor,active=True,role='OWNER').exists():raise PermissionDenied('Only the payout maker or an owner may cancel it.')
        if op.status=='QUEUED':
            from .worker import feature
            feature(op).fail_operation(op,reason='Cancelled before dispatch',cancelled=True)
            op.status='CANCELLED';op.completed_at=timezone.now();op.save();record(actor,locked,'operation.cancelled',op.pk)
        elif op.status in ['DISPATCHING','UNKNOWN']:
            op.cancellation_requested=True;op.save(update_fields=['cancellation_requested','updated_at'])
            if not Job.objects.filter(kind='financial_resolve',payload__operation_id=str(op.pk),status__in=['READY','CLAIMED']).exists():enqueue('financial_resolve',locked,{'operation_id':str(op.pk)})
            record(actor,locked,'operation.cancellation_requested',op.pk)
        return op
    try:
        op=local_action(request.user,merchant,'OPERATION_CANCEL',operation_id,form.cleaned_data['action_key'],{'expected_version':form.cleaned_data['expected_version']},perform,participants=participants,required_role=('OWNER','FINANCE'),load_result=lambda ref,actor,locked:get_object_or_404(Operation,pk=ref,merchant=locked))
    except ValidationError as error:return HttpResponse(' '.join(error.messages),status=503 if getattr(error,'code',None)=='capability_unavailable' else 409 if getattr(error,'code',None)=='key_conflict' else 400)
    if op.status in ['DISPATCHING','UNKNOWN']:messages.info(request,'The action may already have completed. Its reservation remains until authoritative resolution.')
    return redirect('operations:operation_detail',merchant_id=merchant.pk,operation_id=op.pk)


def sandbox_controls(request):
    from .domain import action_key
    if request.user.platform_role!='OPERATIONS':return []
    return [{'merchant':merchant,'key':action_key()} for merchant in Merchant.objects.filter(status__in=['ACTIVE','SUSPENDED','CLOSING']).order_by('name')[:20]]

def sandbox_payments(request):
    from payments.models import Payment
    from .domain import action_key
    if request.user.platform_role!='OPERATIONS':return []
    return [{'payment':payment,'key':action_key()} for payment in Payment.objects.filter(state='CAPTURED').select_related('merchant').order_by('-created_at')[:20]]
