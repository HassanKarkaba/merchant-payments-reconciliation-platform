import hashlib
import threading
import uuid
from datetime import timedelta
from django.core.exceptions import ValidationError,PermissionDenied
from django.db import transaction,close_old_connections,connection
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.core.mail import send_mail
from .models import Control,Job,Operation,Inbox,Outbox
from .domain import enqueue
from .audit import canonical,record
from . import provider
RETRY=[1,5,30,120]

RECOVERY_KINDS = ('financial_resolve', 'callback_apply', 'provider_event_poll', 'dispute_reminder', 'dispute_deadline')
SCHEDULER_LOCK = 1297109584


def database_now():
    with connection.cursor() as cursor:
        cursor.execute('SELECT clock_timestamp()')
        return cursor.fetchone()[0]


def _recovery_query():
    # Dispute responses use the financial coordinator but share reserved service
    # capacity with deadlines, callbacks, and uncertainty inspection.
    from django.db.models.functions import Cast
    from django.db.models import CharField
    from django.db.models.fields.json import KeyTextTransform
    responses = Operation.objects.filter(kind='DISPUTE_RESPONSE').annotate(text_id=Cast('id', CharField())).values('text_id')
    return Q(kind__in=RECOVERY_KINDS) | Q(operation_text__isnull=False, operation_text__in=responses, kind='financial_dispatch')



def _terminal_operation_query():
    from django.db.models.functions import Cast
    from django.db.models import CharField
    terminal = Operation.objects.filter(status__in=['SUCCEEDED', 'FAILED', 'CANCELLED']).annotate(text_id=Cast('id', CharField())).values('text_id')
    return Q(kind__in=['financial_dispatch', 'financial_resolve'], operation_text__isnull=False, operation_text__in=terminal)

def claim(owner, lane='any'):
    from django.db.models.fields.json import KeyTextTransform
    now = database_now()
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_try_advisory_xact_lock(%s)', [SCHEDULER_LOCK])
            if not cursor.fetchone()[0]:
                return None
        jobs = Job.objects.annotate(operation_text=KeyTextTransform('operation_id', 'payload'))
        active = jobs.filter(status='CLAIMED', lease_until__gt=now)
        if active.count() >= 4:
            return None
        ordinary_full = active.exclude(_recovery_query()).count() >= 3
        eligible = jobs.filter(Q(status='READY', due_at__lte=now) | Q(status='CLAIMED', lease_until__lte=now)).exclude(_terminal_operation_query())
        if lane == 'normal':
            if ordinary_full:
                return None
            eligible = eligible.exclude(_recovery_query())
        elif lane == 'recovery' or ordinary_full:
            eligible = eligible.filter(_recovery_query())
        # Group eligible merchants BEFORE the 50-scope bound. The latest lease
        # from every previous claim gives a shared least-recently-served order;
        # retained terminal/future jobs cannot occupy the eligible prefix.
        sql, params = eligible.order_by().query.sql_with_params()
        table = connection.ops.quote_name(Job._meta.db_table)
        candidate_sql = f"""WITH eligible AS ({sql}), scopes AS (
            SELECT merchant_id, MIN(due_at) AS first_due FROM eligible GROUP BY merchant_id
        ) SELECT scopes.merchant_id FROM scopes LEFT JOIN LATERAL (
            SELECT MAX(history.lease_until) AS last_claim FROM {table} history
            WHERE history.merchant_id IS NOT DISTINCT FROM scopes.merchant_id
              AND history.lease_owner IS NOT NULL
        ) history ON TRUE ORDER BY history.last_claim ASC NULLS FIRST,
          scopes.first_due ASC, scopes.merchant_id ASC NULLS FIRST LIMIT 50"""
        with connection.cursor() as cursor:
            cursor.execute(candidate_sql, params)
            scopes = [row[0] for row in cursor.fetchall()]
        for merchant_id in scopes:
            job = eligible.filter(merchant_id=merchant_id).select_for_update(skip_locked=True).order_by('due_at', 'id').first()
            if job is None:
                continue
            job.status = 'CLAIMED'
            job.claim_generation += 1
            job.lease_owner = owner
            job.lease_until = now + timedelta(seconds=30)
            job.attempts += 1
            job.save()
            return job
    return None


def refresh_lease(job):
    now = database_now()
    return bool(Job.objects.filter(pk=job.pk, status='CLAIMED', claim_generation=job.claim_generation,
        lease_owner=job.lease_owner, lease_until__gt=now).update(lease_until=now + timedelta(seconds=30)))


class LeaseHeartbeat:
    def __init__(self, job):
        self.job = job
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='mprp-lease-heartbeat', daemon=True)

    def run(self):
        try:
            while not self.stop.wait(10):
                try:
                    close_old_connections()
                    if not refresh_lease(self.job):
                        return
                except Exception:
                    # A failed refresh grants no new authority. Every financial
                    # write still checks generation, ownership, and live lease.
                    close_old_connections()
        finally:
            connection.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=2)


def mark_blocked(job, reason):
    current = current_job(job)
    if current:
        current.status = 'BLOCKED'
        current.last_error = reason[:100]
        current.due_at = database_now() + timedelta(seconds=5)
        current.save(update_fields=['status', 'last_error', 'due_at', 'updated_at'])


def unknown_origin(op):
    value = op.result.get('uncertain_since') or op.result.get('dispatched_at')
    parsed = parse_datetime(value) if value else None
    return parsed or op.created_at


def mark_unknown(op):
    op.status = 'UNKNOWN'
    op.result = {**op.result, 'uncertain_since': op.result.get('uncertain_since') or database_now().isoformat()}
    op.save(update_fields=['status', 'result', 'updated_at'])


def needs_review(job, op=None):
    current = current_job(job)
    if not current:
        return
    if current:
        current.status = 'NEEDS_REVIEW'
        current.last_error = 'Authoritative inspection exceeded its 24-hour budget' if op else 'Transient attempt budget exhausted'
        current.save(update_fields=['status', 'last_error', 'updated_at'])
    if op:
        record(op.actor, op.merchant, 'operation.needs_review', op.pk, {'status': 'UNKNOWN', 'principal_reserved': True})

def feature(op):
    if op.kind in ('DISPUTE_OPEN', 'SETTLEMENT_FLUSH', 'CORRECTION'):
        from . import sandbox as domain
    elif op.kind=='DISPUTE_RESPONSE':
        from disputes import domain
    elif op.kind=='PAYOUT':
        from settlements import domain
    else:
        from payments import domain
    return domain

def current_job(job):
    return Job.objects.select_for_update().filter(pk=job.pk,status='CLAIMED',claim_generation=job.claim_generation,lease_owner=job.lease_owner,lease_until__gt=database_now()).first()

def lock_scope(op):
    from accounts.models import User
    from merchants.models import Merchant,Membership
    from accounting.models import AccountingPeriod
    users=[op.actor_id] if op.actor_id else []
    if op.kind=='PAYOUT':
        from settlements.models import Payout
        payout=Payout.objects.filter(pk=op.resource_id).first()
        if payout:users.extend([payout.maker_id,payout.checker_id])
    list(User.objects.select_for_update().filter(pk__in=[x for x in users if x]).order_by('pk'))
    Merchant.objects.select_for_update().get(pk=op.merchant_id)
    list(Membership.objects.select_for_update().filter(merchant_id=op.merchant_id,user_id__in=[x for x in users if x]).order_by('pk'))
    list(AccountingPeriod.objects.select_for_update().filter(merchant_id=op.merchant_id).order_by('month'))

def dispatch(job):
    op_id=job.payload['operation_id']
    with transaction.atomic():
        if not current_job(job):return
        preliminary=Operation.objects.get(pk=op_id)
        if preliminary.schema_version != 1 or preliminary.policy_version != 'mprp_v1':
            raise ValidationError('Unsupported admitted operation schema or policy.', code='unsupported_version')
        lock_scope(preliminary)
        op=Operation.objects.select_for_update(of=('self',)).select_related('merchant','actor').get(pk=op_id)
        if op.status in ['SUCCEEDED','FAILED','CANCELLED']:return
        control=Control.objects.get(pk=1)
        resolving=op.status in ['UNKNOWN','DISPATCHING'] or job.kind=='financial_resolve'
        if resolving and database_now() >= unknown_origin(op) + timedelta(hours=24):
            if op.status != 'UNKNOWN':mark_unknown(op)
            needs_review(job, op)
            return
        if not resolving and (control.restore_hold or not control.provider_ready):
            mark_blocked(job, 'Recovery hold or provider qualification unavailable')
            return
        if not resolving:
            try:payload=feature(op).prepare_operation(op)
            except (ValidationError,PermissionDenied) as exc:
                if getattr(exc,'code',None)=='temporarily_blocked':
                    mark_blocked(job, 'Financial prerequisites temporarily blocked');return
                if op.kind=='PAYOUT':feature(op).reject_pre_dispatch(op,reason='Current approval or financial guards changed')
                else:feature(op).fail_operation(op,reason='Current authority or financial guards changed',cancelled=True)
                op.status='CANCELLED';op.completed_at=timezone.now();op.save();record(op.actor,op.merchant,'operation.cancelled',op.pk);return
            op.status='DISPATCHING';op.attempts+=1
            op.result={**op.result,'dispatched_at':op.result.get('dispatched_at') or database_now().isoformat()}
            op.save(update_fields=['status','attempts','result','updated_at'])
        else:payload=op.payload
        key=str(op.provider_key);merchant=op.merchant
    try:
        if resolving:
            result=provider.request('POST','/v1/resolve-key/',provider.envelope(merchant,key,{}))
        else:
            route={'AUTHORIZE':'authorizations','CAPTURE':'captures','VOID':'voids','REFUND':'refunds','PAYOUT':'payouts','DISPUTE_RESPONSE':'disputes/respond','DISPUTE_OPEN':'disputes/open','SETTLEMENT_FLUSH':'settlements/flush','CORRECTION':'corrections'}[op.kind]
            result=provider.request('POST',f'/v1/{route}/',provider.envelope(merchant,key,payload))
        provider.validate_result(result, op)
        if result.get('schema_version')!=1 or result.get('policy_version')!='mprp_v1':raise provider.ProviderUncertain('Unsupported provider schema')
        if str(result.get('operation_key'))!=key:raise provider.ProviderUncertain('Provider identity mismatch')
        state=result.get('state')
        if state not in ['SUCCEEDED','FAILED','NO_EFFECT']:raise provider.ProviderUncertain('Uncertain provider result')
    except provider.ProviderError:
        with transaction.atomic():
            if not current_job(job):return
            op=Operation.objects.select_for_update().get(pk=op_id)
            mark_unknown(op)
            if database_now() >= unknown_origin(op) + timedelta(hours=24):
                needs_review(job, op)
            else:
                enqueue('financial_resolve',op.merchant,{'operation_id':str(op.pk)},database_now()+timedelta(seconds=1 if not resolving else 60))
            record(op.actor,op.merchant,'operation.unknown',op.pk)
        return
    with transaction.atomic():
        if not current_job(job):return
        preliminary=Operation.objects.get(pk=op_id)
        lock_scope(preliminary)
        op=Operation.objects.select_for_update(of=('self',)).select_related('merchant','actor').get(pk=op_id)
        if state=='SUCCEEDED':
            snapshot=result.get('snapshot',{})
            feature(op).complete_operation(op,snapshot)
            op.status='SUCCEEDED'
        else:
            feature(op).fail_operation(op,reason=result.get('reason','Provider confirmed no effect'))
            op.status='FAILED'
        op.result={'state':state,'safe_message':'Authoritative provider result recorded'};op.completed_at=timezone.now();op.save()
        record(op.actor,op.merchant,'operation.result',op.pk,{'status':op.status})
        from .domain import notify
        if op.actor_id:notify(op.actor,op.kind.title()+' '+op.status.lower(),f'/m/{op.merchant_id}/operations/{op.pk}/',event_key='result:'+str(op.pk),merchant=op.merchant)
        if not current_job(job):
            raise ValidationError('The job lease expired before financial result commitment.', code='stale_job')

def admit_event(event,cursor=None):
    # Feed positions are transport metadata; the immutable callback body is identical.
    event={key:value for key,value in event.items() if key not in {'sequence','position','cursor'}}
    identity=str(event['event_id']);digest=hashlib.sha256(canonical(event).encode()).hexdigest()
    existing,created=Inbox.objects.get_or_create(event_id=identity,defaults={'payload_hash':digest,'payload':event,'cursor':cursor})
    if existing.payload_hash!=digest:raise ValidationError('Conflicting provider event identity')
    if cursor is not None:
        if existing.cursor is not None and existing.cursor!=cursor:raise ValidationError('Conflicting provider feed position')
        if existing.cursor is None:
            existing.cursor=cursor;existing.save(update_fields=['cursor'])
    if created:enqueue('callback_apply',payload={'event_id':identity})
    return existing

def poll_events():
    control=Control.objects.first()
    if not control or not control.dispatch_epoch:return
    result=provider.request('GET',f'/v1/events/?after={control.received_cursor}')
    events=result.get('events',[])
    with transaction.atomic():
        control=Control.objects.select_for_update().get(pk=1)
        for event in events:
            position=event.get('position',event.get('sequence',event.get('cursor')))
            if position is None:raise ValidationError('Missing feed position')
            if position<=control.received_cursor:continue
            if position!=control.received_cursor+1:raise ValidationError('Event feed gap')
            admit_event(event,position);control.received_cursor=position
        control.save(update_fields=['received_cursor'])
    provider.request('POST','/v1/events/ack/',{'environment_id':str(control.environment_id),'main_instance_id':str(control.main_instance_id),'received_cursor':control.received_cursor})

def apply_event(job):
    inbox=Inbox.objects.get(pk=job.payload['event_id']);event=inbox.payload
    if inbox.schema_version != 1 or inbox.policy_version != 'mprp_v1':
        raise ValidationError('Unsupported retained inbox schema or policy.', code='unsupported_version')
    from merchants.models import Merchant
    merchant=Merchant.objects.get(provider_account_id=event['provider_account_id'])
    object_id=event.get('object_id')
    object_type=event.get('object_type','payment').lower()
    # Signed callbacks admit observations; the independent provider lookup supplies truth.
    result=provider.request('GET',f'/v1/objects/{object_id}/')
    provider.validate_result(result, provider_account_id=merchant.provider_account_id)
    snapshot=result.get('snapshot',result)
    if str(snapshot.get('provider_account_id'))!=str(merchant.provider_account_id):
        raise ValidationError('Provider event object scope mismatch')
    payment_snapshot=None
    if object_type=='dispute':
        payment_result=provider.request('GET',f"/v1/payments/{snapshot['payment_id']}/")
        provider.validate_result(payment_result, provider_account_id=merchant.provider_account_id)
        payment_snapshot=payment_result.get('snapshot',payment_result)
    with transaction.atomic():
        if not current_job(job):return
        # Accepted service observations share the financial control order;
        # domain payment/payout locks must never precede this tenant lock.
        merchant=Merchant.objects.select_for_update().get(pk=merchant.pk)
        if object_type=='payment':
            from payments.domain import apply_snapshot,ensure_automatic_capture
            payment=apply_snapshot(merchant,snapshot)
            if payment:ensure_automatic_capture(payment)
        elif object_type=='dispute':
            from disputes.domain import apply_dispute_snapshot
            from payments.domain import apply_snapshot
            apply_snapshot(merchant,payment_snapshot)
            apply_dispute_snapshot(merchant,snapshot)
        elif object_type=='settlement':
            from settlements.domain import apply_batch
            apply_batch(merchant,snapshot)
        elif object_type=='payout':
            operation=Operation.objects.select_for_update().filter(merchant=merchant,resource_id=object_id,kind='PAYOUT').first()
            if not operation:
                raise ValidationError('Provider payout lacks local immutable admission; recovery inventory is required')
            if operation.status!='SUCCEEDED':
                from settlements.domain import complete_operation
                complete_operation(operation,snapshot)
                operation.status='SUCCEEDED';operation.completed_at=timezone.now();operation.save()
                record(None,merchant,'operation.provider_event',operation.pk,{'status':'SUCCEEDED'})
        elif object_type=='correction':
            from accounting.domain import apply_economic_event
            for source in sorted(snapshot.get('sources',[]),key=lambda item:item.get('sequence',0)):
                apply_economic_event(merchant,source)
        else:raise ValidationError('Unsupported provider event object')
        inbox.status='DONE';inbox.save(update_fields=['status'])
        if not current_job(job):
            raise ValidationError('The job lease expired before callback commitment.', code='stale_job')

def run_job(job):
    if job.schema_version != 1 or job.policy_version != 'mprp_v1':
        raise ValidationError('Unsupported retained job schema or policy.', code='unsupported_version')
    if job.kind=='reconciliation_match':
        from accounts.models import User
        from reconciliation.domain import run_matching
        actor=User.objects.get(pk=job.payload['actor_id'])
        try:summary=run_matching(actor,job.merchant,guard=lambda:current_job(job) is not None)
        except PermissionDenied:
            with transaction.atomic():
                owned=current_job(job)
                if owned:
                    owned.status='BLOCKED';owned.last_error='Current reconciliation authority is unavailable';owned.save(update_fields=['status','last_error','updated_at'])
            return
        with transaction.atomic():
            owned=current_job(job)
            if owned:
                owned.payload={**owned.payload,'summary':summary};owned.save(update_fields=['payload','updated_at'])
                record(actor,job.merchant,'reconciliation.completed',job.pk,summary)
        return
    if job.kind=='retention_cleanup':
        from .retention import cleanup_transient
        return cleanup_transient()
    if job.kind=='dispute_reminder':
        from disputes.models import Dispute
        from .notifications import announce
        dispute=Dispute.objects.filter(pk=job.payload['dispute_id'],state__in=['OPEN','RESPONDED'],deadline__gt=database_now()).first()
        if dispute:
            announce('dispute.reminder'+str(job.payload['hours']),dispute.pk,merchant=dispute.merchant)
        return
    if job.kind in ['financial_dispatch','financial_resolve']:return dispatch(job)
    if job.kind=='callback_apply':return apply_event(job)
    if job.kind=='provider_event_poll':return poll_events()
    if job.kind=='notification_send':
        out=Outbox.objects.get(pk=job.payload['outbox_id'])
        if out.status=='SENT':return
        if timezone.now()>=out.expires_at:out.encrypted_body='';out.status='EXPIRED';out.save();return
        if out.subject.startswith('MPRP activity:'):
            from accounts.models import User
            eligible=User.objects.filter(email=out.recipient,is_active=True,verified=True)
            if out.merchant_id:eligible=eligible.filter(memberships__merchant=out.merchant,memberships__active=True,memberships__role__in=['OWNER','FINANCE'])
            else:eligible=eligible.filter(platform_role__in=['OPERATIONS','FINANCE_APPROVER'])
            if not eligible.exists():out.status='CANCELLED';out.encrypted_body='';out.save();return
        from accounts.domain import decrypt
        send_mail(out.subject,decrypt(out.encrypted_body),None,[out.recipient])
        out.status='SENT';out.encrypted_body='';out.save()
    elif job.kind=='export_build':
        from .reports import build_export
        build_export(job.payload['export_id'], job=job)
    else:raise ValidationError('Unsupported job kind')

def schedule_maintenance():
    """Durable, idempotent local schedules; no money effects in this coordinator."""
    now = database_now()
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_try_advisory_xact_lock(%s)', [SCHEDULER_LOCK + 1])
            if not cursor.fetchone()[0]:
                return
        from django.db.models.fields.json import KeyTextTransform
        Job.objects.annotate(operation_text=KeyTextTransform('operation_id', 'payload')).filter(_terminal_operation_query()).exclude(status='DONE').update(status='DONE', updated_at=now)
        day = now.date().isoformat()
        if not Job.objects.filter(kind='retention_cleanup', payload__day=day).exists():
            enqueue('retention_cleanup', payload={'day': day})
        from disputes.models import Dispute
        for dispute in Dispute.objects.filter(state__in=['OPEN', 'RESPONDED'], deadline__gt=now, deadline__lte=now + timedelta(hours=48)).order_by('deadline', 'id')[:50]:
            for hours in (48, 24):
                if now >= dispute.deadline - timedelta(hours=hours) and not Job.objects.filter(kind='dispute_reminder', payload__dispute_id=str(dispute.pk), payload__hours=hours).exists():
                    enqueue('dispute_reminder', dispute.merchant, {'dispute_id': str(dispute.pk), 'hours': hours})
        control = Control.objects.first()
        if control and not control.restore_hold and control.provider_ready:
            Job.objects.filter(status='BLOCKED', due_at__lte=now, kind='financial_dispatch').update(status='READY')


def tick(owner=None, lane='any'):
    owner=owner or uuid.uuid4();close_old_connections();now=database_now()
    control=Control.objects.first()
    if control:
        from . import run_identity
        if run_identity.is_bound() and not run_identity.is_current(control,bind_if_unbound=False):return False
        scope=run_identity.control_scope(control)
        if not control.provider_checked_at or (now-control.provider_checked_at).total_seconds()>=10:
            from .health import refresh_provider_readiness
            refresh_provider_readiness(control)
        with transaction.atomic():
            fresh=Control.objects.select_for_update().filter(**scope).first()
            if not fresh:return False
            progress_now=database_now()
            if fresh.last_clock and (fresh.last_clock-progress_now).total_seconds()>2:
                if not (fresh.restore_hold and fresh.hold_reason=='Backward clock movement detected'):
                    previous=fresh.last_clock
                    fresh.restore_hold=True;fresh.hold_reason='Backward clock movement detected';fresh.provider_ready=False
                    record(None,None,'recovery.hold',uuid.uuid4(),{'main_instance_id':str(fresh.main_instance_id),'previous_progress_at':previous.isoformat(),'observed_at':progress_now.isoformat()},reason=fresh.hold_reason)
            fresh.worker_heartbeat=progress_now;fresh.last_clock=progress_now
            fresh.save(update_fields=['restore_hold','hold_reason','provider_ready','worker_heartbeat','last_clock'])
    job=claim(owner, lane=lane)
    if not job:return False
    try:
        with LeaseHeartbeat(job):
            run_job(job)
            with transaction.atomic():
                current=current_job(job)
                if current:current.status='DONE';current.save(update_fields=['status','updated_at'])
    except Exception as exc:
        with transaction.atomic():
            current=current_job(job)
            if current:
                current.last_error=type(exc).__name__
                if getattr(exc, 'code', None) == 'unsupported_version':
                    current.status='NEEDS_REVIEW';current.last_error='Unsupported retained schema or policy version'
                    current.save(update_fields=['status','last_error','updated_at'])
                elif job.kind in ('financial_dispatch', 'financial_resolve'):
                    op=Operation.objects.select_for_update().filter(pk=job.payload.get('operation_id')).first()
                    if op is None:
                        current.status='NEEDS_REVIEW';current.last_error='Missing immutable operation admission';current.save(update_fields=['status','last_error','updated_at'])
                        Control.objects.filter(pk=1).update(restore_hold=True,hold_reason='A durable financial job lacks its immutable admission; reconcile provider inventory.')
                        record(None,None,'recovery.hold',current.pk,{'reason':'missing_operation_admission'})
                    elif op.status in ('DISPATCHING', 'UNKNOWN'):
                        mark_unknown(op)
                        if database_now() >= unknown_origin(op) + timedelta(hours=24):
                            needs_review(job, op)
                        else:
                            current.kind='financial_resolve';current.status='READY';current.due_at=database_now()+timedelta(seconds=60);current.save()
                    else:
                        mark_blocked(job, type(exc).__name__)
                else:
                    current.status='READY' if current.attempts<5 else 'NEEDS_REVIEW'
                    current.due_at=database_now()+timedelta(seconds=RETRY[min(current.attempts-1,3)])
                    current.save()
    finally:close_old_connections()
    return True
