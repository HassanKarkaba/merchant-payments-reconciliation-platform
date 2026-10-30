"""Transactional notification admission, current-recipient selection and unique publication."""
from django.conf import settings
from django.db import transaction
from accounts.models import User
from .models import Notification
TITLES={
 'dispute.reminder48':'A dispute response is due within 48 hours',
 'dispute.reminder24':'A dispute response is due within 24 hours',
 'beneficiary.proposed':'A synthetic beneficiary is awaiting owner approval',
 'beneficiary.approved':'A synthetic beneficiary was approved',
 'payout.proposed':'A payout proposal is awaiting a different owner',
 'payout.approved':'A payout was approved for processing',
 'payment_operation.failed':'A simulated refund could not be completed',
 'exception.assign':'A reconciliation exception was assigned to you',
 'payout.paid':'A simulated payout completed',
 'payout.no_effect':'A payout ended without a successful transfer',
 'payout.needs_reapproval':'A payout needs a new owner review',
 'refund.completed':'A simulated refund completed',
 'dispute.opened':'A dispute needs your attention',
 'dispute.observe':'A dispute was updated',
 'period.close':'An accounting period was closed',
 'period.reopen':'An accounting period was reopened',
 'recovery.hold':'Financial work is on recovery hold',
 'recovery.released':'Recovery hold was released',
}

@transaction.atomic
def announce(action,resource_id,merchant=None,summary=None):
    title=TITLES.get(action)
    if not title:return
    if action=='payment_operation.failed':
        from .models import Operation
        if not Operation.objects.filter(pk=resource_id,kind='REFUND').exists():return
    if action=='exception.assign':
        from reconciliation.models import ReconciliationException
        case=ReconciliationException.objects.select_related('owner').filter(pk=resource_id).first()
        recipients=User.objects.filter(pk=case.owner_id if case else None,is_active=True,verified=True,platform_role='RECONCILIATION')
        url=f'/operations/exceptions/{resource_id}/'
    elif merchant:
        recipients=User.objects.filter(is_active=True,verified=True,memberships__merchant=merchant,memberships__active=True,memberships__role__in=['OWNER','FINANCE']).distinct()
        url=f'/m/{merchant.pk}/'
        if action.startswith('payout'):url=f'/m/{merchant.pk}/payouts/'
        elif action.startswith('refund'):url=f'/m/{merchant.pk}/payments/'
        elif action.startswith('dispute'):url=f'/m/{merchant.pk}/disputes/'
        elif action.startswith('period'):url=f'/m/{merchant.pk}/reports/'
    else:
        recipients=User.objects.filter(is_active=True,verified=True,platform_role__in=['OPERATIONS','FINANCE_APPROVER'])
        url='/operations/status/'
    from .domain import mail
    for recipient in recipients:
        _,created=Notification.objects.get_or_create(recipient=recipient,event_key=action+':'+str(resource_id),defaults={'merchant':merchant,'title':title,'url':url})
        if created:mail(recipient.email,'MPRP activity: '+title,'New sandbox activity is available. Sign in to inspect: '+settings.PUBLIC_ORIGIN+url,merchant=merchant)


def visible_notifications(user):
    from django.db.models import Q
    query=Notification.objects.filter(recipient=user)
    if not user.is_active:return query.none()
    if user.platform_role in ['OPERATIONS','RECONCILIATION','FINANCE_APPROVER','AUDITOR']:return query
    return query.filter(Q(merchant=None)|Q(merchant__memberships__user=user,merchant__memberships__active=True)).distinct()
