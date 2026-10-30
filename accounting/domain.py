from datetime import date, datetime, timezone as dt_timezone
import re
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum, Q, Max
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import LedgerAccount, AccountingPeriod, EconomicSource, Journal, JournalEntry

ACCOUNT_SIDES = {'processor_clearing':'DEBIT','bank_control':'DEBIT','merchant_payable':'CREDIT','adjustment_suspense':'DEBIT'}
MAPPING = {
    'CAPTURE': ('processor_clearing','merchant_payable',1),
    'FEE': ('merchant_payable','processor_clearing',-1),
    'REFUND': ('merchant_payable','processor_clearing',-1),
    'DISPUTE_OPEN': ('merchant_payable','processor_clearing',-1),
    'DISPUTE_WON': ('processor_clearing','merchant_payable',1),
    'PAYOUT': ('merchant_payable','bank_control',-1),
    'CORRECTION_CREDIT': ('processor_clearing','merchant_payable',1),
    'CORRECTION_DEBIT': ('merchant_payable','processor_clearing',-1),
}


def fee_for(gross):
    if isinstance(gross, bool) or not isinstance(gross,int) or not 100 <= gross <= 100000000:
        raise ValidationError('Capture must be 100 through 100000000 USD cents.')
    return (29 * gross + 500) // 1000 + 30


def ensure_accounts(merchant):
    return {code: LedgerAccount.objects.get_or_create(merchant=merchant,code=code,defaults={'normal_side':side})[0] for code,side in ACCOUNT_SIDES.items()}


def balances(merchant, cutoff=None, sequence=None, period_month=None):
    result = {code:0 for code in ACCOUNT_SIDES}
    entries = JournalEntry.objects.filter(journal__merchant=merchant)
    if cutoff: entries = entries.filter(journal__occurred_at__lt=cutoff)
    if period_month is not None: entries=entries.filter(journal__period__month__lte=period_month)
    if sequence is not None: entries = entries.filter(journal__sequence__lte=sequence)
    rows = entries.values('account__code','side').annotate(total=Sum('amount_minor'))
    for row in rows:
        code = row['account__code']
        result[code] += row['total'] * (1 if row['side'] == ACCOUNT_SIDES[code] else -1)
    return result


def trial_balance(merchant, sequence=None, period_month=None):
    entries = JournalEntry.objects.filter(journal__merchant=merchant)
    if sequence is not None: entries = entries.filter(journal__sequence__lte=sequence)
    if period_month is not None: entries=entries.filter(journal__period__month__lte=period_month)
    totals = entries.aggregate(debits=Sum('amount_minor',filter=Q(side='DEBIT')),credits=Sum('amount_minor',filter=Q(side='CREDIT')))
    totals = {k:v or 0 for k,v in totals.items()}
    totals['balanced'] = totals['debits']==totals['credits']
    return totals


def next_month(month):
    return date(month.year+1,1,1) if month.month==12 else date(month.year,month.month+1,1)


def posting_period(merchant, occurred_at):
    month = occurred_at.astimezone(dt_timezone.utc).date().replace(day=1)
    original=month
    while True:
        period,_ = AccountingPeriod.objects.get_or_create(merchant=merchant,month=month)
        period=AccountingPeriod.objects.select_for_update().get(pk=period.pk)
        if period.state not in ('CLOSED','REOPEN_PROPOSED'):
            return period,month!=original
        month=next_month(month)


def _event_time(value):
    if isinstance(value,str): value=parse_datetime(value)
    if not isinstance(value,datetime) or timezone.is_naive(value): raise ValidationError('Provider event time must include a UTC offset.')
    return value.astimezone(dt_timezone.utc)


@transaction.atomic
def observe_economic_source(merchant, source_id, event_type, amount_minor, occurred_at, reference='', **metadata):
    if event_type not in (*MAPPING,'SETTLEMENT','BATCH_ZERO'):
        raise ValidationError('Unsupported economic event.')
    if isinstance(amount_minor,bool) or not isinstance(amount_minor,int) or abs(amount_minor)>10**14:
        raise ValidationError('Economic amount must be bounded integer USD cents.')
    if event_type=='BATCH_ZERO':
        if amount_minor: raise ValidationError('Zero batch must have zero amount.')
    elif not amount_minor: raise ValidationError('Economic amount cannot be zero.')
    elif event_type in MAPPING and amount_minor*MAPPING[event_type][2] <= 0:
        raise ValidationError('Economic event has the wrong direction.')
    occurred_at=_event_time(occurred_at)
    provider_account_id=str(metadata.get('provider_account_id') or getattr(merchant,'provider_account_id',''))
    if str(getattr(merchant,'provider_account_id',provider_account_id)) != provider_account_id:
        raise ValidationError('Provider account does not belong to this merchant.')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',str(source_id),flags=re.ASCII) or (reference and not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',reference,flags=re.ASCII)):
        raise ValidationError('Economic source identifiers and references use 1 through 64 ASCII letters, digits, underscores, periods or hyphens.')
    batch=str(metadata.get('batch_id') or '')
    if batch and not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',batch,flags=re.ASCII):raise ValidationError('Invalid settlement batch identifier.')
    defaults={'provider_account_id':provider_account_id,'amount_minor':amount_minor,'occurred_at':occurred_at,'reference':reference or str(source_id),'batch_id':str(metadata.get('batch_id') or ''),'provider_version':metadata.get('provider_version',1),'payload':metadata}
    source,created=EconomicSource.objects.get_or_create(merchant=merchant,source_id=str(source_id),event_type=event_type,defaults=defaults)
    if not created and (source.amount_minor != amount_minor or source.occurred_at != occurred_at or source.provider_account_id != provider_account_id or source.reference != defaults['reference']):
        raise ValidationError('Conflicting immutable economic source.')
    incoming_batch=str(metadata.get('batch_id') or '')
    if incoming_batch and source.batch_id and incoming_batch!=source.batch_id:
        raise ValidationError('An economic source cannot move to a different settlement batch.')
    if incoming_batch and not source.batch_id:
        source.batch_id=incoming_batch
        source.save(update_fields=['batch_id'])
    return source


@transaction.atomic
def post_economic_event(merchant, source_id, event_type, amount_minor, occurred_at, actor=None, reference='', approved_correction=False, **metadata):
    # A caller must supply only a validated authenticated provider observation.
    from merchants.models import Merchant
    from operations.audit import record
    merchant=Merchant.objects.select_for_update().get(pk=merchant.pk)
    source=observe_economic_source(merchant,source_id,event_type,amount_minor,occurred_at,reference,**metadata)
    existing=Journal.objects.filter(source=source,purpose=event_type).first()
    if existing:return existing
    if event_type=='BATCH_ZERO': return None
    if event_type.startswith('CORRECTION_') and not approved_correction:
        from reconciliation.models import ReconciliationException
        ReconciliationException.objects.get_or_create(merchant=merchant,category='UNRECOGNIZED_CORRECTION',source_reference=str(source_id),defaults={'amount_minor':amount_minor,'reason':'Authenticated correction awaits independent recognition approval.'})
        return None
    accounts=ensure_accounts(merchant)
    period,late=posting_period(merchant,source.occurred_at)
    if event_type=='SETTLEMENT':
        debit,credit=('bank_control','processor_clearing') if amount_minor>0 else ('processor_clearing','bank_control')
    else: debit,credit,_=MAPPING[event_type]
    sequence=(Journal.objects.filter(merchant=merchant).aggregate(n=Max('sequence'))['n'] or 0)+1
    # The merchant lock serializes sequences with all funds and close decisions.
    if hasattr(merchant,'ledger_sequence'):
        merchant.ledger_sequence=sequence
        fields=['ledger_sequence']
        if hasattr(merchant,'funds_version'):
            merchant.funds_version+=1
            fields.append('funds_version')
        merchant.save(update_fields=fields)
    journal=Journal.objects.create(merchant=merchant,period=period,source=source,purpose=event_type,sequence=sequence,occurred_at=source.occurred_at,explanation=f'{event_type.replace("_"," ").title()} · {source.reference}'[:500],late=late)
    JournalEntry.objects.bulk_create([JournalEntry(journal=journal,account=accounts[debit],side='DEBIT',amount_minor=abs(amount_minor)),JournalEntry(journal=journal,account=accounts[credit],side='CREDIT',amount_minor=abs(amount_minor))])
    source.recognized=True
    source.save(update_fields=['recognized'])
    record(actor=actor,merchant=merchant,action='ledger.post',resource_id=str(journal.pk),summary={'source_id':str(source_id),'event_type':event_type,'sequence':sequence,'amount_minor':amount_minor})
    if late:
        from reconciliation.models import ReconciliationException
        ReconciliationException.objects.get_or_create(merchant=merchant,category='LATE_EVENT',source_reference=str(source_id),defaults={'amount_minor':amount_minor,'reason':'Original event month is closed; posted in the next chronological open month.'})
    return journal


def apply_economic_event(merchant,event,actor=None,**kwargs):
    event=dict(event)
    source_id=event.pop('source_id',None) or event.pop('id',None)
    kind=event.pop('event_type',None) or event.pop('kind',None)
    amount=event.pop('amount_minor')
    occurred=event.pop('occurred_at')
    reference=event.pop('reference','')
    return post_economic_event(merchant,source_id,kind,amount,occurred,actor=actor,reference=reference,**event,**kwargs)
