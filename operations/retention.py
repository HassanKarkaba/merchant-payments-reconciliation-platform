"""Selected local retention and a separately privileged complete-tenant purge."""
import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction
from django.db.models import Q
from django.db.models.fields.json import KeyTextTransform
from django.utils import timezone
from accounts.models import User,AuthToken
from merchants.models import Merchant,Membership
from .audit import canonical,record
from .models import PurgeReceipt,Control,Operation,Job,Inbox,Outbox,Export,Notification,Nonce,RateBucket,AuditHead,AuditEvent

TERMINAL={'SUCCEEDED','FAILED','CANCELLED'}


def backup_inventory(root,environment_id,expire=False):
    """Only the explicitly mounted app backup store is inspected or expired."""
    root=Path(root)
    if not root.is_dir() or root.is_symlink():raise ValidationError('Mount the configured local backup store explicitly; its absence is not an empty inventory.')
    marker=root/'.mprp-backup-root'
    if marker.is_symlink() or not marker.is_file() or marker.read_text().strip()!=str(environment_id):
        raise ValidationError('The backup store sentinel does not match this application environment.')
    now=timezone.now();files=[];removed=[]
    for path in sorted(root.iterdir()):
        if path.name=='.mprp-backup-root':continue
        if path.is_symlink() or not path.is_file() or path.suffix!='.dump':
            raise ValidationError('Unexpected backup-store entry requires local inventory review before purge.')
        age=now.timestamp()-path.stat().st_mtime
        identity=hashlib.sha256(path.name.encode()).hexdigest()
        if expire and age>=30*86400:
            path.unlink();removed.append(identity)
        else:files.append({'backup_id':identity,'age_seconds':max(0,int(age))})
    return {'scope':'configured_local_backup_store','empty':not files,'retained_backup_count':len(files),'retained_backup_ids':[f['backup_id'] for f in files],'expired_backup_ids':removed,'checked_at':now.isoformat(),'inventory_hash':hashlib.sha256(canonical(files).encode()).hexdigest()}


def _current_actor(actor,role):
    current=User.objects.select_for_update().get(pk=actor.pk)
    if not current.is_active or not current.verified or not current.totp_confirmed or current.platform_role!=role:
        raise PermissionDenied('Current verified platform authority and MFA are required.')
    return current


def tenant_querysets(merchant):
    from .models import ApprovalReceipt
    from accounting.models import Journal,JournalEntry,LedgerAccount,EconomicSource,AccountingPeriod,CloseReport
    from reconciliation.models import StatementImport,StatementLine,ImportRow,MatchGroup,MatchAllocation,ReconciliationException,AdjustmentProposal
    from disputes.models import Dispute,DisputeResponse
    from payments.models import CheckoutLink,CheckoutSession,Payment,Refund
    from settlements.models import Beneficiary,Payout,SettlementBatch,Transfer
    account=str(merchant.provider_account_id)
    event_ids=Inbox.objects.filter(payload__provider_account_id=account).values_list('event_id',flat=True)
    return [
        DisputeResponse.objects.filter(dispute__merchant=merchant),MatchAllocation.objects.filter(group__merchant=merchant),
        AdjustmentProposal.objects.filter(merchant=merchant),ImportRow.objects.filter(statement__merchant=merchant),
        ReconciliationException.objects.filter(merchant=merchant),StatementLine.objects.filter(merchant=merchant),StatementImport.objects.filter(merchant=merchant),
        JournalEntry.objects.filter(journal__merchant=merchant),Journal.objects.filter(merchant=merchant),EconomicSource.objects.filter(merchant=merchant),
        CloseReport.objects.filter(period__merchant=merchant),AccountingPeriod.objects.filter(merchant=merchant),LedgerAccount.objects.filter(merchant=merchant),
        Transfer.objects.filter(merchant=merchant),SettlementBatch.objects.filter(merchant=merchant),Payout.objects.filter(merchant=merchant),Beneficiary.objects.filter(merchant=merchant),
        Refund.objects.filter(merchant=merchant),CheckoutSession.objects.filter(link__merchant=merchant),Dispute.objects.filter(merchant=merchant),Payment.objects.filter(merchant=merchant),CheckoutLink.objects.filter(merchant=merchant),
        Operation.objects.filter(merchant=merchant),Job.objects.annotate(event_identity=KeyTextTransform('event_id','payload')).filter(Q(merchant=merchant)|Q(kind='callback_apply',event_identity__in=event_ids)),
        Notification.objects.filter(merchant=merchant),Outbox.objects.filter(merchant=merchant),Export.objects.filter(merchant=merchant),AuthToken.objects.filter(merchant=merchant),
        Membership.objects.filter(merchant=merchant),AuditEvent.objects.filter(merchant=merchant),AuditHead.objects.filter(stream=str(merchant.pk)),Inbox.objects.filter(payload__provider_account_id=account),
        MatchGroup.objects.filter(merchant=merchant),ApprovalReceipt.objects.filter(merchant=merchant),Merchant.objects.filter(pk=merchant.pk),
    ]


def inventory(merchant):
    counts={query.model._meta.db_table:query.count() for query in tenant_querysets(merchant)}
    head=AuditHead.objects.filter(stream=str(merchant.pk)).first()
    heads={str(merchant.pk):{'sequence':head.sequence,'digest':head.digest}} if head else {}
    result={'merchant_id':str(merchant.pk),'provider_account_id':str(merchant.provider_account_id),'environment_id':str(settings.MPRP_ENVIRONMENT_ID),'closed_at':merchant.closed_at.isoformat() if merchant.closed_at else None,'counts':counts,'audit_heads':heads}
    result['hash']=hashlib.sha256(canonical(result).encode()).hexdigest()
    return result


def purge_blockers(merchant):
    blockers=[]
    if merchant.status!='CLOSED' or merchant.closed_at is None:blockers.append('Merchant must be fully closed.')
    elif timezone.now()<merchant.closed_at+timedelta(days=365):blockers.append('Financial retention continues until 365 days after merchant closure.')
    if Operation.objects.filter(merchant=merchant).exclude(status__in=TERMINAL).exists():blockers.append('All accepted operations must be terminal, including unknown outcomes.')
    control=Control.objects.first()
    if not control or control.restore_hold:blockers.append('Resolve the environment recovery hold before purge.')
    from .lifecycle import closure_blockers
    blockers.extend(closure_blockers(merchant))
    return list(dict.fromkeys(blockers))


@transaction.atomic
def propose_purge(actor,merchant,backup,reason=''):
    actor=_current_actor(actor,'OPERATIONS')
    merchant=Merchant.objects.select_for_update().get(pk=merchant.pk)
    blockers=purge_blockers(merchant)
    if not backup.get('empty'):blockers.append('Retained local backups still contain the closed merchant dataset; expire or explicitly retire them under the selected procedure.')
    if blockers:raise ValidationError(blockers)
    data=inventory(merchant)
    receipt,created=PurgeReceipt.objects.select_for_update().get_or_create(merchant_id_safe=merchant.pk,defaults={'provider_account_id':merchant.provider_account_id,'environment_id':settings.MPRP_ENVIRONMENT_ID,'closed_at':merchant.closed_at,'eligible_at':merchant.closed_at+timedelta(days=365)})
    if receipt.status in ['RETIRED','PURGING','COMPLETE']:raise ValidationError('This namespace is already retired; resume the existing purge receipt.')
    receipt.maker_id_safe=actor.pk;receipt.checker_id_safe=None;receipt.status='PROPOSED';receipt.inventory_hash=data['hash'];receipt.dataset_counts=data['counts'];receipt.final_audit_heads=data['audit_heads'];receipt.backup_inventory=backup;receipt.approved_at=None;receipt.findings=[];receipt.save()
    record(actor,None,'purge.proposed',receipt.pk,{'merchant_id':str(merchant.pk),'inventory_hash':receipt.inventory_hash,'counts':receipt.dataset_counts},reason=reason[:2000])
    return receipt


@transaction.atomic
def approve_purge(actor,receipt,reason=''):
    list(User.objects.select_for_update().filter(pk__in=[actor.pk,receipt.maker_id_safe]).order_by('pk'))
    actor=_current_actor(actor,'FINANCE_APPROVER')
    receipt=PurgeReceipt.objects.select_for_update().get(pk=receipt.pk)
    if actor.pk==receipt.maker_id_safe:raise PermissionDenied('A different finance approver must approve purge.')
    if receipt.status!='PROPOSED':raise ValidationError('A current immutable purge proposal is required.')
    maker=User.objects.get(pk=receipt.maker_id_safe)
    if not maker.is_active or maker.platform_role!='OPERATIONS':raise PermissionDenied('The purge maker no longer has authority.')
    merchant=Merchant.objects.select_for_update().get(pk=receipt.merchant_id_safe)
    blockers=purge_blockers(merchant)
    if blockers:raise ValidationError(blockers)
    if inventory(merchant)['hash']!=receipt.inventory_hash:raise ValidationError('The dataset inventory changed. A new purge proposal is required.')
    receipt.checker_id_safe=actor.pk;receipt.status='APPROVED';receipt.approved_at=timezone.now();receipt.save(update_fields=['checker_id_safe','status','approved_at','updated_at'])
    record(actor,None,'purge.approved',receipt.pk,{'merchant_id':str(receipt.merchant_id_safe),'maker_id':str(receipt.maker_id_safe),'inventory_hash':receipt.inventory_hash},reason=reason[:2000])
    return receipt


def retire_provider_namespace(receipt,backup):
    from .provider import request,envelope
    if receipt.status not in ['APPROVED','RETIRED','PURGING']:raise ValidationError('A separately approved purge receipt is required.')
    if not backup.get('empty'):raise ValidationError('Retained local backups must be retired before purge execution.')
    if receipt.status in ['RETIRED','PURGING']:
        receipt.backup_inventory=backup;receipt.save(update_fields=['backup_inventory','updated_at']);return receipt
    merchant=Merchant.objects.get(pk=receipt.merchant_id_safe)
    blockers=purge_blockers(merchant)
    if blockers:raise ValidationError(blockers)
    if inventory(merchant)['hash']!=receipt.inventory_hash:raise ValidationError('Dataset changed after approval. Re-propose before retirement.')
    result=request('POST',f'/v1/accounts/{receipt.provider_account_id}/retire/',envelope(merchant,str(receipt.pk),{'merchant_closed_at':receipt.closed_at.isoformat(),'approval_id':str(receipt.pk),'inventory_hash':receipt.inventory_hash,'reason':'Complete closed-merchant retention purge'}))
    result=result.get('snapshot',result)
    if result.get('retired') is not True or str(result.get('provider_account_id'))!=str(receipt.provider_account_id):raise ValidationError('The simulator has not permanently retired this exact namespace.')
    with transaction.atomic():
        receipt=PurgeReceipt.objects.select_for_update().get(pk=receipt.pk)
        receipt.status='RETIRED';receipt.simulator_receipt=result;receipt.backup_inventory=backup;receipt.save(update_fields=['status','simulator_receipt','backup_inventory','updated_at'])
        record(None,None,'purge.namespace_retired',receipt.pk,{'merchant_id':str(receipt.merchant_id_safe),'provider_account_id':str(receipt.provider_account_id)})
    return receipt


def refresh_provider_purge(receipt):
    from .provider import request
    result=request('GET',f'/v1/accounts/{receipt.provider_account_id}/retirement/')
    if result.get('retired') is not True or result.get('purge_complete') is not True or str(result.get('provider_account_id'))!=str(receipt.provider_account_id) or result.get('remaining_rows')!=0:
        raise ValidationError('Run the separately privileged simulator purge_retired_namespace command first, then resume this receipt. The retired namespace remains closed.')
    receipt.simulator_receipt=result;receipt.save(update_fields=['simulator_receipt','updated_at'])
    return receipt


def execute_purge_batches(receipt,purge_database_url,batch_size=500,max_batches=100,operator='local-purger',reason=''):
    import psycopg
    if not purge_database_url:raise ValidationError('MPRP_PURGE_DATABASE_URL must select the separate mprp_purge role; ordinary runtime credentials cannot purge.')
    if not 1<=batch_size<=1000 or not 1<=max_batches<=10000:raise ValidationError('Purge batch bounds are invalid.')
    with psycopg.connect(purge_database_url,autocommit=True) as database:
        for _ in range(max_batches):
            result=database.execute('SELECT public.mprp_purge_merchant_batch(%s,%s)',(receipt.pk,batch_size)).fetchone()[0]
            if isinstance(result,str):result=json.loads(result)
            if result.get('status')=='COMPLETE':
                record(operator,None,'purge.complete',receipt.pk,{'merchant_id':str(receipt.merchant_id_safe),'provider_account_id':str(receipt.provider_account_id),'counts':result.get('deleted_counts',{})},reason=reason[:2000])
                return result
    return result


def cleanup_transient(backup_root=None):
    now=timezone.now();counts={}
    with transaction.atomic():
        counts['export_bytes']=Export.objects.filter(expires_at__lte=now).update(content=None,status='EXPIRED')
        counts['outbox_secrets']=Outbox.objects.filter(expires_at__lte=now).update(encrypted_body='',status='EXPIRED')
        counts['token_secrets']=AuthToken.objects.filter(Q(expires_at__lte=now)|Q(used_at__isnull=False)).delete()[0]
        counts['nonces']=Nonce.objects.filter(expires_at__lte=now).delete()[0]
        counts['rate_buckets']=RateBucket.objects.filter(expires_at__lte=now).delete()[0]
        from reconciliation.models import StatementImport
        counts['unconfirmed_uploads']=StatementImport.objects.filter(expires_at__lte=now).exclude(status='CONFIRMED').update(content=b'',parsed_rows=[],errors=[],status='EXPIRED')
        counts['notification_bodies']=Notification.objects.filter(created_at__lte=now-timedelta(days=7)).exclude(title='Expired notification').update(title='Expired notification',url='')
        counts['outbox_bodies']=Outbox.objects.filter(created_at__lte=now-timedelta(days=7),status__in=['SENT','EXPIRED']).update(recipient='retained@example.test',subject='Expired notification',encrypted_body='')
        # Financial request fingerprints/provider keys remain with the closed dataset.
        # Clearing retained response content after 90d does not delete tombstones.
        counts['expired_response_bodies']=Operation.objects.filter(status__in=TERMINAL,completed_at__lte=now-timedelta(days=90)).exclude(result={}).update(result={})
        counts['eligible_merchants']=0
        for merchant in Merchant.objects.filter(status='CLOSED',closed_at__lte=now-timedelta(days=365)):
            if purge_blockers(merchant):continue
            _,created=PurgeReceipt.objects.get_or_create(merchant_id_safe=merchant.pk,defaults={'provider_account_id':merchant.provider_account_id,'environment_id':settings.MPRP_ENVIRONMENT_ID,'closed_at':merchant.closed_at,'eligible_at':merchant.closed_at+timedelta(days=365)})
            counts['eligible_merchants']+=int(created)
    if backup_root:
        counts['backups']=backup_inventory(backup_root,settings.MPRP_ENVIRONMENT_ID,expire=True)
    return counts
