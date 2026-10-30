"""Restoration fencing, complete provider-key inventory and separate hold release."""
import hashlib
import time
import uuid
from urllib.parse import urlencode
from django.conf import settings
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from django.db.models import Max, BigIntegerField
from django.db.models.functions import Cast
from django.db.models.fields.json import KeyTextTransform
from django.utils import timezone
from .models import Control, Operation, Inbox, RecoveryInventory, AuditEvent, Job
from . import provider
from .audit import canonical, record


def _event_body(event):
    return {key:value for key,value in event.items() if key not in {'sequence','position','cursor'}}


def _verify_retained_prefix(start, end, deadline=None):
    cursor = start
    while cursor < end:
        budget=min(5,deadline-time.monotonic()) if deadline else 5
        if budget<=0:raise provider.ProviderUncertain('Startup event-prefix inspection exceeded its budget')
        page = provider.request('GET', f'/v1/events/?after={cursor}',timeout=budget)
        progressed = False
        for event in page.get('events', []):
            position = event.get('sequence')
            if type(position) is not int or position != cursor+1:
                return False
            if position > end:
                return True
            retained = Inbox.objects.filter(event_id=str(event.get('event_id'))).first()
            expected = hashlib.sha256(canonical(_event_body(event)).encode()).hexdigest()
            if not retained or retained.payload_hash != expected or retained.cursor not in (None, position):
                return False
            cursor = position
            progressed = True
            if cursor == end:
                return True
        if not progressed:
            return False
    return True


STARTUP_HOLD='Startup inventory pending; financial admission is temporarily held.'
STARTUP_PROOF='environment.startup_latched'
PROVIDER_KINDS={'authorizations':'AUTHORIZE','captures':'CAPTURE','voids':'VOID','refunds':'REFUND','payouts':'PAYOUT','disputes/open':'DISPUTE_OPEN','disputes/respond':'DISPUTE_RESPONSE','settlements/flush':'SETTLEMENT_FLUSH','corrections':'CORRECTION'}


def _run_hash(run_token):
    return hashlib.sha256(str(run_token).encode()).hexdigest()


def _startup_keys(items,deadline):
    """Compare all retained economic keys in bounded indexed main-store reads."""
    failures=[];owned_pending=[];checked=0
    for offset in range(0,len(items),500):
        if time.monotonic()>=deadline:raise provider.ProviderUncertain('Startup provider-key inspection exceeded its budget')
        chunk=[item for item in items[offset:offset+500] if item.get('kind') not in {'accounts','accounts/retire'}]
        keys=[]
        for item in chunk:
            try:keys.append(uuid.UUID(str(item['operation_key'])))
            except (ValueError,KeyError,TypeError):
                if item.get('state')!='NO_EFFECT':failures.append({'code':'invalid_provider_key','provider_key':str(item.get('operation_key',''))[:128]})
        local={str(op.provider_key):op for op in Operation.objects.filter(provider_key__in=keys).select_related('merchant')}
        for item in chunk:
            key=str(item.get('operation_key',''));operation=local.get(key)
            if item.get('state')=='NO_EFFECT' and operation is None:continue
            checked+=1
            expected=PROVIDER_KINDS.get(item.get('kind'))
            if operation is None:
                failures.append({'code':'missing_local_admission','provider_key':key[:128]});continue
            absent=item.get('state')=='NO_EFFECT'
            valid=(str(operation.merchant.provider_account_id)==str(item.get('provider_account_id')) and operation.schema_version==1 and operation.policy_version=='mprp_v1')
            if not absent:
                valid=valid and expected==operation.kind and item.get('payload_hash')==operation.fingerprint and isinstance(item.get('payload'),dict) and hashlib.sha256(canonical(item['payload']).encode()).hexdigest()==operation.fingerprint
            if not valid:
                failures.append({'code':'admission_identity_conflict','provider_key':key[:128]});continue
            if (operation.status in {'FAILED','CANCELLED'} and item.get('state')=='SUCCEEDED') or (operation.status=='SUCCEEDED' and item.get('state') in {'FAILED','NO_EFFECT'}):
                failures.append({'code':'terminal_outcome_conflict','provider_key':key[:128]});continue
            if operation.status=='QUEUED':owned_pending.append(operation.pk)
    # A restored QUEUED projection cannot cancel an independently sent request
    # as if it were unsent. Continue service resolution under the original key.
    for operation_id in owned_pending:
        if time.monotonic()>=deadline:raise provider.ProviderUncertain('Startup pending-result admission exceeded its budget')
        with transaction.atomic():
            from .worker import lock_scope
            preliminary=Operation.objects.get(pk=operation_id)
            lock_scope(preliminary)
            operation=Operation.objects.select_for_update().get(pk=operation_id)
            if operation.status!='QUEUED':continue
            operation.status='UNKNOWN'
            # Preserve a retained origin. If dispatch metadata was rolled back,
            # admission is a conservative bound; never grant a fresh24h window.
            operation.result={**operation.result,'uncertain_since':operation.result.get('uncertain_since') or operation.result.get('dispatched_at') or operation.created_at.isoformat(),'startup_provider_truth':True}
            operation.save(update_fields=['status','result','updated_at'])
            if not Job.objects.filter(kind='financial_resolve',payload__operation_id=str(operation.pk),status__in=['READY','CLAIMED','NEEDS_REVIEW']).exists():
                from .domain import enqueue
                enqueue('financial_resolve',operation.merchant,{'operation_id':str(operation.pk)})
            record(None,operation.merchant,'recovery.pending_provider_result',operation.pk,{'provider_key':str(operation.provider_key),'status':'UNKNOWN'})
    return {'checked_keys':checked,'owned_pending':len(owned_pending),'mismatch_count':len(failures),'mismatches':failures[:50]}


def prepare_run(new_run=False,deadline=None):
    deadline=time.monotonic()+115 if deadline is None else deadline
    if time.monotonic()>=deadline:raise provider.ProviderUncertain('Startup inspection exceeded its budget')
    invalid_environment=False
    with transaction.atomic():
        control,_ = Control.objects.get_or_create(pk=1,defaults={'environment_id':settings.MPRP_ENVIRONMENT_ID})
        control=Control.objects.select_for_update().get(pk=1)
        if str(control.environment_id)!=str(settings.MPRP_ENVIRONMENT_ID):
            control.restore_hold=True;control.hold_reason='Main database environment differs from configured environment.';control.provider_ready=False;control.provider_checked_at=None;control.save()
            record(None,None,'recovery.hold',uuid.uuid4(),{'main_instance_id':str(control.main_instance_id)},reason=control.hold_reason)
            invalid_environment=True
        else:
            proof=AuditEvent.objects.filter(stream='platform',action=STARTUP_PROOF,resource_id=_run_hash(control.run_token)).order_by('-sequence').first() if control.restore_hold and control.hold_reason==STARTUP_HOLD else None
            if proof and not new_run:
                preserve_hold=bool(proof.summary.get('preserve_hold'))
                previous_reason=str(proof.summary.get('previous_reason',''))
            else:
                if proof:
                    preserve_hold=bool(proof.summary.get('preserve_hold'));previous_reason=str(proof.summary.get('previous_reason',''))
                else:
                    preserve_hold=bool(control.restore_hold and control.hold_reason!='Initial pairing required')
                    previous_reason=control.hold_reason if preserve_hold else ''
                control.run_token=uuid.uuid4()
                control.restore_hold=True;control.hold_reason=STARTUP_HOLD;control.provider_ready=False;control.provider_checked_at=None;control.save()
                record(None,None,STARTUP_PROOF,_run_hash(control.run_token),{'preserve_hold':preserve_hold,'previous_reason':previous_reason})
            # Retries remain held even when a previous handshake committed but
            # its response was lost. Individual process starts never call this.
            control.restore_hold=True;control.provider_ready=False;control.provider_checked_at=None;control.save(update_fields=['restore_hold','provider_ready','provider_checked_at'])
            values={'environment_id':str(control.environment_id),'main_instance_id':str(control.main_instance_id),'run_token':str(control.run_token)}
            previous_cursor=control.received_cursor
    if invalid_environment:raise ValidationError('Environment mismatch; financial admission remains held')
    from accounting.models import EconomicSource
    # Compare the main prefix observed BEFORE handshake with the later provider
    # prefix; ordinary service ingestion during inspection cannot create a false
    # rollback signal from a stale remote high-water.
    local_source_high=EconomicSource.objects.aggregate(high=Max(Cast(KeyTextTransform('sequence','payload'),BigIntegerField())))['high'] or 0
    budget=min(5,deadline-time.monotonic())
    if budget<=0:raise provider.ProviderUncertain('Startup inspection exceeded its budget')
    result=provider.request('POST','/v1/control/handshake/',values,timeout=budget)
    instance=str(result.get('simulator_instance_id',''))
    if result.get('schema_version')!=1 or result.get('policy_version')!='mprp_v1' or result.get('currency')!='USD' or result.get('sandbox') is not True or result.get('environment_id')!=str(settings.MPRP_ENVIRONMENT_ID) or not result.get('resolve_key'):
        raise ValidationError('Provider handshake contract is incompatible; startup hold remains')
    acknowledged=int(result.get('acknowledged_cursor',0));high=int(result.get('event_high_water',0))
    prefix_valid=True
    if acknowledged<previous_cursor<=high:prefix_valid=_verify_retained_prefix(acknowledged,previous_cursor,deadline)
    remote_items,remote=_provider_inventory(deadline)
    if remote.get('environment_id')!=values['environment_id'] or str(remote.get('simulator_instance_id'))!=instance:
        raise ValidationError('Provider startup inventory binding differs; startup hold remains')
    key_inventory=_startup_keys(remote_items,deadline)
    if acknowledged<previous_cursor and prefix_valid:
        budget=min(5,deadline-time.monotonic())
        if budget<=0:raise provider.ProviderUncertain('Startup acknowledgment exceeded its budget')
        provider.request('POST','/v1/events/ack/',{'environment_id':values['environment_id'],'main_instance_id':values['main_instance_id'],'received_cursor':previous_cursor},timeout=budget)
    with transaction.atomic():
        control=Control.objects.select_for_update().get(pk=1)
        if str(control.run_token)!=values['run_token']:
            control.restore_hold=True;control.hold_reason='Concurrent startup coordinator changed the run token; prepare a new coherent run.';control.save()
            return control
        mismatch=bool((control.simulator_instance_id and control.simulator_instance_id!=instance) or previous_cursor<acknowledged or previous_cursor>high or local_source_high>int(result.get('source_high_water',0)) or result.get('restored') or not prefix_valid or key_inventory['mismatch_count'])
        control.dispatch_epoch=int(result['dispatch_epoch']);control.simulator_instance_id=instance;control.provider_ready=False;control.provider_checked_at=None
        if mismatch:
            control.restore_hold=True
            control.hold_reason='Provider key inventory, database identity, event prefix or restoration evidence differs; reconcile inventory and obtain separate finance release.'
        elif control.hold_reason==STARTUP_HOLD:
            control.restore_hold=preserve_hold;control.hold_reason=previous_reason if preserve_hold else ''
        # An explicit hold admitted while HTTP inspection ran always survives.
        control.save()
        record(None,None,'environment.paired',control.main_instance_id,{'dispatch_epoch':control.dispatch_epoch,'restore_hold':control.restore_hold,**key_inventory})
        if mismatch:record(None,None,'recovery.hold',uuid.uuid5(control.run_token,'startup-hold'),{'main_instance_id':str(control.main_instance_id)},reason=control.hold_reason)
    return control


def hold(operator,reason):
    if not operator or not reason or len(reason)>2000 or reason==STARTUP_HOLD:
        raise ValidationError('Operator identity and bounded reason required')
    with transaction.atomic():
        control=Control.objects.select_for_update().get(pk=1)
        control.restore_hold=True;control.hold_reason=reason;control.release_maker=str(getattr(operator,'pk',operator));control.release_checker=None;control.provider_ready=False
        control.save()
        record(operator,None,'recovery.hold',uuid.uuid4(),{'main_instance_id':str(control.main_instance_id)},reason=reason)


def inventory():
    return list(Operation.objects.filter(status__in=['DISPATCHING','UNKNOWN']).values('id','provider_key','kind','status','merchant_id'))


def authenticate_staff(email,password,code,roles):
    from accounts.models import User
    from accounts.domain import consume_totp
    with transaction.atomic():
        user=User.objects.select_for_update().filter(email=email.strip().lower()).first()
        if not user or not user.is_active or not user.verified or not user.totp_confirmed or user.platform_role not in roles or not user.check_password(password) or not consume_totp(user,code):
            raise PermissionDenied('Current staff credentials and an unused fresh authenticator code are required')
        return user


def _provider_inventory(deadline=None):
    after=0;snapshot=None;operations=[];last=None
    while True:
        query={'after':after}
        if snapshot:query['snapshot']=snapshot
        budget=min(5,deadline-time.monotonic()) if deadline else 5
        if budget<=0:raise provider.ProviderUncertain('Provider inventory exceeded the startup budget')
        page=provider.request('GET','/v1/inventory/?'+urlencode(query),timeout=budget)
        if page.get('schema_version')!=1 or not isinstance(page.get('operations'),list) or len(page['operations'])>500:
            raise ValidationError('Provider inventory page is malformed')
        snapshot=snapshot or page.get('snapshot')
        if snapshot!=page.get('snapshot'):
            raise ValidationError('Provider inventory snapshot changed')
        operations.extend(page['operations']);last=page
        next_after=page.get('next_after')
        if next_after is None:break
        if type(next_after) is not int or next_after<=after:raise ValidationError('Provider inventory cursor made no progress')
        after=next_after
    return operations,last


def _restore_payment_admission(merchant, item, snapshot):
    from payments.models import Payment,CheckoutSession,CheckoutLink
    payment_id=snapshot.get('payment_id') or item['payload'].get('payment_id')
    payment=Payment.objects.filter(pk=payment_id,merchant=merchant).first()
    if payment:return payment
    session_id=item['payload'].get('checkout_session_id')
    checkout=CheckoutSession.objects.select_related('link').filter(pk=session_id,link__merchant=merchant).first() if session_id else None
    if not checkout or Payment.objects.filter(link=checkout.link).exists():
        raise ValidationError('Provider payment has no retained matching checkout admission; restore its source records before hold release')
    payment=Payment.objects.create(id=payment_id,merchant=merchant,link=checkout.link,reference='pay_'+uuid.UUID(str(payment_id)).hex,amount_minor=snapshot.get('authorized_minor') or item['payload']['amount_minor'],customer_name=item['payload'].get('customer_name',''),auto_capture_pending=False)
    CheckoutLink.objects.filter(pk=checkout.link_id).update(status='CONSUMED')
    checkout.payment=payment;checkout.save(update_fields=['payment'])
    return payment


def _apply_provider_operation(item,result):
    from merchants.models import Merchant
    from payments.models import Payment,Refund
    from accounting.domain import apply_economic_event
    merchant=Merchant.objects.get(provider_account_id=item['provider_account_id'])
    snapshot=result.get('snapshot') or {}
    state=result.get('state')
    from types import SimpleNamespace
    expected=SimpleNamespace(provider_key=item['operation_key'],fingerprint=item['payload_hash'],merchant=merchant)
    provider.validate_result(result,expected)
    if state not in {'SUCCEEDED','FAILED','NO_EFFECT'}:
        raise ValidationError('Provider operation has no terminal authoritative outcome')
    kind=PROVIDER_KINDS.get(item['kind'])
    if kind and not Operation.objects.filter(provider_key=item['operation_key']).exists():
        raise ValidationError('Canonical main admission and original request-key protection are missing; restore retained admission evidence before release')
    with transaction.atomic():
        from merchants.models import Merchant
        merchant=Merchant.objects.select_for_update().get(pk=merchant.pk)
        if state=='SUCCEEDED':
            object_type=snapshot.get('object_type')
            if object_type=='payment':
                payment=_restore_payment_admission(merchant,item,snapshot)
                from payments.domain import apply_snapshot
                apply_snapshot(merchant,snapshot)
            elif object_type=='dispute':
                from disputes.domain import apply_dispute_snapshot
                apply_dispute_snapshot(merchant,snapshot)
            elif object_type=='settlement':
                from settlements.domain import apply_batch
                apply_batch(merchant,snapshot)
            elif object_type=='correction':
                for source in sorted(snapshot.get('sources',[]),key=lambda source:source.get('sequence',0)):
                    apply_economic_event(merchant,source)
        if not kind:return
        operation=Operation.objects.select_for_update().filter(provider_key=item['operation_key']).first()
        resource_id=(item['payload'].get('payment_id') or item['payload'].get('payout_id') or item.get('object_id'))
        if kind=='DISPUTE_RESPONSE':
            # A response is nonmonetary. Existing response admission is needed for actor attribution.
            if not operation:return
        if not operation:
            # Provider truth does not reveal a lost original browser action key
            # or maker. Inventing either would permit an old form to pay twice.
            raise ValidationError('Original financial admission is unavailable; its immutable request identity must be restored before release')
        if kind=='REFUND':
            refund_id=item['payload'].get('refund_id')
            payment=Payment.objects.get(pk=item['payload']['payment_id'],merchant=merchant)
            refund=Refund.objects.filter(operation=operation).first()
            if not refund:
                Refund.objects.create(id=refund_id or uuid.uuid4(),merchant=merchant,payment=payment,operation=operation,amount_minor=item['payload']['amount_minor'],reason=item['payload'].get('reason','Recovered authoritative synthetic refund'),reserved=False,status='SUCCEEDED' if state=='SUCCEEDED' else 'FAILED')
        if state=='SUCCEEDED':
            if kind=='PAYOUT':
                from settlements.models import Payout
                payout=Payout.objects.filter(pk=resource_id,merchant=merchant).first()
                if not payout or not payout.checker_id:
                    for source in snapshot.get('sources',[]):apply_economic_event(merchant,source)
                    raise ValidationError('Surviving payout effect lacks retained immutable approval; restore approval evidence before release')
                if not payout.operation_id:payout.operation=operation;payout.save(update_fields=['operation'])
            from .worker import feature
            if operation.status!='SUCCEEDED':feature(operation).complete_operation(operation,snapshot)
            operation.status='SUCCEEDED'
        else:
            from .worker import feature
            if operation.status not in {'FAILED','CANCELLED'}:feature(operation).fail_operation(operation,reason=result.get('reason','Terminal no-effect evidence'))
            operation.status='FAILED'
        operation.completed_at=timezone.now();operation.result={'state':state,'safe_message':'Recovered from immutable provider inventory'};operation.save()
        record(None,merchant,'recovery.operation',operation.pk,{'provider_key':item['operation_key'],'state':operation.status})


def _reconcile_source_inventory(high_water,findings):
    from merchants.models import Merchant
    from accounting.domain import apply_economic_event
    after=0;verified=0
    while True:
        page=provider.request('GET','/v1/inventory/sources/?'+urlencode({'after':after,'snapshot':high_water}))
        if page.get('snapshot')!=high_water or not isinstance(page.get('sources'),list) or len(page['sources'])>500:
            raise ValidationError('Provider source inventory snapshot is malformed')
        for source in page['sources']:
            sequence=source.get('sequence')
            if type(sequence) is not int or not after<sequence<=high_water:
                raise ValidationError('Provider source inventory sequence is inconsistent')
            after=sequence
            try:
                merchant=Merchant.objects.get(provider_account_id=source['provider_account_id'])
                with transaction.atomic():
                    if source['event_type'] in {'DISPUTE_OPEN','DISPUTE_WON'}:
                        from disputes.domain import apply_dispute_event
                        apply_dispute_event(merchant,source)
                    else:
                        apply_economic_event(merchant,source)
                verified+=1
            except Exception as error:
                findings.append({'source_id':source.get('source_id'),'code':type(error).__name__,'message':'Authenticated provider source could not be reconstructed; retained source evidence requires review.'})
        if page.get('next_after') is None:break
        if page['next_after']!=after or not page['sources']:
            raise ValidationError('Provider source inventory made no progress')
    return verified


def reconcile_inventory(operator,reason):
    if not operator or not operator.is_active or operator.platform_role not in {'OPERATIONS','RECONCILIATION'} or not reason or len(reason)>2000:
        raise PermissionDenied('An active operations or reconciliation maker and reason are required')
    hold(operator,reason)
    control=Control.objects.get(pk=1)
    remote_items,remote=_provider_inventory()
    if remote.get('environment_id')!=str(control.environment_id) or remote.get('simulator_instance_id')!=control.simulator_instance_id:
        raise ValidationError('Provider inventory environment or instance is not the prepared binding')
    findings=[];known=set();applied=0
    for item in remote_items:
        key=item.get('operation_key')
        known.add(str(key))
        if item.get('kind') in {'accounts','accounts/retire'}:continue
        try:
            result=provider.request('GET',f'/v1/operations/{key}/?'+urlencode({'provider_account_id':item['provider_account_id']}))
            _apply_provider_operation(item,result);applied+=1
        except Exception as error:
            findings.append({'provider_key':str(key),'code':type(error).__name__,'message':'Provider truth could not be fully reconstructed; inspect the retained resource and recovery logs.'})
    # Absence is resolved atomically; a plain 404 never releases principal.
    for operation in Operation.objects.filter(status__in=['DISPATCHING','UNKNOWN']).select_related('merchant'):
        if str(operation.provider_key) in known:continue
        try:
            result=provider.request('POST','/v1/resolve-key/',provider.envelope(operation.merchant,operation.provider_key,{}))
            item={'operation_key':str(operation.provider_key),'kind':{'AUTHORIZE':'authorizations','CAPTURE':'captures','VOID':'voids','REFUND':'refunds','PAYOUT':'payouts','DISPUTE_RESPONSE':'disputes/respond'}[operation.kind],'provider_account_id':str(operation.merchant.provider_account_id),'object_id':str(operation.resource_id),'payload':operation.payload,'payload_hash':operation.fingerprint}
            _apply_provider_operation(item,result);applied+=1
        except Exception as error:
            findings.append({'provider_key':str(operation.provider_key),'code':type(error).__name__,'message':'Uncertain operation still requires authoritative resolution.'})
    verified_sources=_reconcile_source_inventory(remote['source_high_water'],findings)
    # Admit/replay every durable feed event with its original identity; work stays recoverable in hold.
    from .worker import poll_events
    for _ in range(2000):
        control.refresh_from_db()
        if control.received_cursor>=remote['event_high_water']:break
        before=control.received_cursor;poll_events();control.refresh_from_db()
        if control.received_cursor==before:break
    control.refresh_from_db()
    if control.received_cursor<remote['event_high_water']:
        findings.append({'code':'incomplete_event_feed','message':'The contiguous received prefix is incomplete.'})
    summary={'provider_operations':len(remote_items),'applied_or_verified':applied,'verified_sources':verified_sources,'unresolved':len(findings),'source_snapshot':remote['source_high_water'],'event_snapshot':remote['event_high_water']}
    fingerprint=hashlib.sha256(canonical({'summary':summary,'findings':findings,'environment_id':str(control.environment_id),'simulator_instance_id':control.simulator_instance_id,'dispatch_epoch':control.dispatch_epoch,'received_cursor':control.received_cursor}).encode()).hexdigest()
    with transaction.atomic():
        inventory=RecoveryInventory.objects.create(maker=operator,environment_id=control.environment_id,simulator_instance_id=control.simulator_instance_id,dispatch_epoch=control.dispatch_epoch,event_high_water=remote['event_high_water'],source_high_water=remote['source_high_water'],received_cursor=control.received_cursor,summary=summary,findings=findings,fingerprint=fingerprint,reason=reason,status='BLOCKED' if findings else 'RECONCILED')
        record(operator,None,'recovery.inventory',inventory.pk,{'fingerprint':fingerprint,**summary},reason=reason)
    return inventory


def approve_release(inventory_id,checker):
    if not checker or not checker.is_active or not checker.verified or not checker.totp_confirmed or checker.platform_role!='FINANCE_APPROVER':
        raise PermissionDenied('A current platform finance approver with fresh authentication is required')
    proposal=RecoveryInventory.objects.get(pk=inventory_id)
    if proposal.maker_id==checker.pk:raise PermissionDenied('The inventory maker cannot approve release')
    remote=provider.request('GET','/v1/capabilities/',timeout=2)
    # A changed provider high-water means new truth arrived after inventory, so it must be reviewed.
    if remote.get('simulator_instance_id')!=proposal.simulator_instance_id or remote.get('dispatch_epoch')!=proposal.dispatch_epoch or remote.get('event_high_water')!=proposal.event_high_water or remote.get('source_high_water')!=proposal.source_high_water:
        raise ValidationError('The provider inventory changed; reconcile a fresh inventory')
    if remote.get('restored'):
        with transaction.atomic():
            from accounts.models import User
            current_checker=User.objects.select_for_update().get(pk=checker.pk)
            if not current_checker.is_active or current_checker.platform_role!='FINANCE_APPROVER':raise PermissionDenied('Finance authority changed before restoration acknowledgment')
            current_proposal=RecoveryInventory.objects.select_for_update().get(pk=proposal.pk)
            current_control=Control.objects.select_for_update().get(pk=1)
            if current_proposal.status!='RECONCILED' or current_proposal.findings or current_proposal.maker_id==checker.pk or current_control.dispatch_epoch!=proposal.dispatch_epoch or current_control.received_cursor<proposal.event_high_water or Operation.objects.filter(status__in=['UNKNOWN','DISPATCHING']).exists() or Inbox.objects.filter(cursor__lte=proposal.event_high_water).exclude(status='DONE').exists():
                raise ValidationError('Local recovery is incomplete; the simulator marker remains held')
            record(checker,None,'recovery.provider_release_requested',proposal.pk,{'inventory_hash':proposal.fingerprint,'maker_id':str(proposal.maker_id)},reason=proposal.reason)
        provider.request('POST','/v1/control/restore-release/',{'environment_id':str(proposal.environment_id),'main_instance_id':str(current_control.main_instance_id),'dispatch_epoch':proposal.dispatch_epoch,'restore_marker_id':remote.get('restore_marker_id'),'inventory_hash':proposal.fingerprint,'event_high_water':proposal.event_high_water,'source_high_water':proposal.source_high_water,'maker_id':str(proposal.maker_id),'checker_id':str(checker.pk),'approval_id':str(proposal.pk)})
        remote=provider.request('GET','/v1/capabilities/',timeout=2)
        if remote.get('restored') or remote.get('event_high_water')!=proposal.event_high_water or remote.get('source_high_water')!=proposal.source_high_water:
            raise ValidationError('Simulator restoration acknowledgment is incomplete or inventory changed; main hold remains')
    with transaction.atomic():
        from accounts.models import User
        current_checker=User.objects.select_for_update().get(pk=checker.pk)
        if not current_checker.is_active or not current_checker.verified or not current_checker.totp_confirmed or current_checker.platform_role!='FINANCE_APPROVER':raise PermissionDenied('Finance authority changed before release commitment')
        proposal=RecoveryInventory.objects.select_for_update().get(pk=inventory_id)
        control=Control.objects.select_for_update().get(pk=1)
        if proposal.status!='RECONCILED' or proposal.findings or control.received_cursor<proposal.event_high_water or control.dispatch_epoch!=proposal.dispatch_epoch or str(control.environment_id)!=str(proposal.environment_id) or control.simulator_instance_id!=proposal.simulator_instance_id:
            raise ValidationError('Recovery inventory does not satisfy the current release conditions')
        if Inbox.objects.filter(cursor__lte=proposal.event_high_water).exclude(status='DONE').exists():
            raise ValidationError('Accepted provider events still await aggregate-state application; let the worker finish recovery before approval')
        if Operation.objects.filter(status__in=['UNKNOWN','DISPATCHING']).exists():
            raise ValidationError('Unresolved economic operations still retain reservations')
        proposal.status='APPROVED';proposal.checker=checker;proposal.approved_at=timezone.now();proposal.save(update_fields=['status','checker','approved_at'])
        control.restore_hold=False;control.hold_reason='';control.release_checker=checker;control.provider_ready=False;control.provider_checked_at=None;control.save()
        record(checker,None,'recovery.release',proposal.pk,{'inventory_hash':proposal.fingerprint,'maker_id':str(proposal.maker_id)},reason=proposal.reason)
    return control
