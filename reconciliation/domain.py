"""Statement evidence admission, exact matching, correction approval, and close."""
import csv
import hashlib
import io
import json
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import quote, urlencode
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Sum, Max, Count
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from accounting.models import EconomicSource, Journal, AccountingPeriod, CloseReport
from accounting.domain import balances, trial_balance, post_economic_event, next_month
from .models import StatementImport, StatementLine, ImportRow, MatchGroup, MatchAllocation, ReconciliationException, AdjustmentProposal

PROCESSOR_COLUMNS=['schema_version','line_id','provider_account_id','event_type','source_id','batch_id','occurred_at','currency','amount_minor','reference']
BANK_COLUMNS=['schema_version','line_id','bank_account_id','booked_at','currency','amount_minor','transfer_reference','description']
COLUMNS={'PROCESSOR_ECONOMIC':PROCESSOR_COLUMNS,'PROCESSOR_TRANSFER':PROCESSOR_COLUMNS,'BANK':BANK_COLUMNS}
SIGNS={'CAPTURE':1,'FEE':-1,'REFUND':-1,'DISPUTE_OPEN':-1,'DISPUTE_WON':1,'CORRECTION_CREDIT':1,'CORRECTION_DEBIT':-1,'PAYOUT':-1}
IDENTIFIER=re.compile(r'[A-Za-z0-9_.-]{1,64}\Z',re.ASCII)
INTEGER=re.compile(r'-?(?:0|[1-9][0-9]*)\Z',re.ASCII)


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def _lock_people(*actors):
    from accounts.models import User
    return list(User.objects.select_for_update().filter(pk__in=[getattr(a,'pk',a) for a in actors if a is not None]).order_by('pk'))


def actor_role(actor,allowed):
    from accounts.models import User
    query=User.objects.select_for_update() if transaction.get_connection().in_atomic_block else User.objects
    current=query.get(pk=actor.pk)
    if not current.is_active or not current.verified or not current.totp_confirmed or current.platform_role not in allowed:
        raise PermissionDenied('This action requires an authorized platform role.')
    return current


def canonical_row(row,kind):
    cols=COLUMNS[kind]
    if set(row)!=set(cols): raise ValidationError('Statement row columns differ from schema version 1.')
    out={key:str(row[key]) for key in cols}
    for key in ('schema_version','amount_minor'):
        if not INTEGER.fullmatch(out[key]):raise ValidationError(f'{key} must use ASCII integer notation.')
        out[key]=str(int(out[key]))
    timekey='booked_at' if kind=='BANK' else 'occurred_at'
    parsed=parse_datetime(out[timekey])
    if parsed is None or timezone.is_naive(parsed):raise ValidationError(f'{timekey} must include its timezone.')
    out[timekey]=parsed.astimezone(dt_timezone.utc).isoformat()
    return out


def parse_statement(content,kind,account_id,period_start,period_end,declared_row_count,declared_net_minor,opening_balance_minor,closing_balance_minor):
    errors=[]; rows=[]
    if kind not in COLUMNS: return [],['Unknown statement kind.']
    if len(content)>5*1024*1024:return [],['The CSV exceeds the 5 MiB limit.']
    if content.startswith((b'PK\x03\x04',b'%PDF',b'\xd0\xcf\x11\xe0')) or b'\x00' in content:return [],['Only a plain UTF-8 CSV file is accepted.']
    try:text=content.decode('utf-8',errors='strict')
    except UnicodeDecodeError:return [],['The CSV must be valid UTF-8.']
    if timezone.is_naive(period_start) or timezone.is_naive(period_end) or not timedelta(0)<period_end-period_start<=timedelta(days=31):return [],['Statement period must be an aware, positive half-open interval of at most 31 days.']
    try:
        reader=csv.reader(io.StringIO(text,newline=''),strict=True)
        header=next(reader,None)
        if header!=COLUMNS[kind]:return [],['The header must exactly match the ordered version 1 schema for this statement kind.']
        seen=set()
        for index,values in enumerate(reader,start=2):
            if index>10001:errors.append('CSV exceeds 10000 data rows.');break
            if len(values)>20 or len(values)!=len(header):
                errors.append(f'Row {index}: wrong number of columns.');continue
            if any(len(v)>4096 for v in values):errors.append(f'Row {index}: a field exceeds 4096 characters.');continue
            raw=dict(zip(header,values))
            try:
                row=canonical_row(raw,kind)
                if row['schema_version']!='1':raise ValidationError('Unsupported schema version.')
                if row['currency']!='USD':raise ValidationError('Only USD is supported.')
                for key in ('line_id', 'bank_account_id','transfer_reference') if kind=='BANK' else ('line_id','provider_account_id','source_id','reference'):
                    if not IDENTIFIER.fullmatch(row[key]):raise ValidationError(f'{key} must be a bounded source identifier.')
                accountkey='bank_account_id' if kind=='BANK' else 'provider_account_id'
                if row[accountkey]!=str(account_id):raise ValidationError('Account differs from the selected statement account.')
                if row['line_id'] in seen:raise ValidationError('Duplicate line_id inside this file.')
                seen.add(row['line_id'])
                occurred=parse_datetime(row['booked_at' if kind=='BANK' else 'occurred_at'])
                if not period_start<=occurred<period_end:raise ValidationError('Row time falls outside the declared half-open period.')
                amount=int(row['amount_minor'])
                if abs(amount)>100000000000000:raise ValidationError('Amount is outside the supported integer bounds.')
                if kind=='BANK':
                    if not amount:raise ValidationError('Bank lines must have a nonzero amount.')
                    if len(row['description'])>2000:raise ValidationError('Description is limited to 2000 characters.')
                else:
                    event=row['event_type']
                    if kind=='PROCESSOR_ECONOMIC' and event not in SIGNS.keys()-{'PAYOUT'}:raise ValidationError('Invalid processor economic event kind.')
                    if kind=='PROCESSOR_TRANSFER' and event not in ('SETTLEMENT','PAYOUT','BATCH_ZERO'):raise ValidationError('Invalid processor transfer kind.')
                    if event=='BATCH_ZERO':
                        if amount!=0:raise ValidationError('BATCH_ZERO requires zero cents.')
                    elif not amount or (event in SIGNS and amount*SIGNS[event]<=0):raise ValidationError('Amount direction does not match its economic event.')
                    if event!='PAYOUT' and not IDENTIFIER.fullmatch(row['batch_id']):raise ValidationError('An immutable batch identifier is required.')
                rows.append(row)
            except (ValidationError,ValueError) as exc:
                errors.append(f'Row {index}: {"; ".join(exc.messages) if isinstance(exc,ValidationError) else str(exc)}')
            if len(errors)>=2000:break
    except (csv.Error,UnicodeError) as exc:errors.append('Malformed CSV quoting or encoding.')
    if len(rows)!=declared_row_count:errors.append('Parsed row count differs from the declared count.')
    if sum(int(r['amount_minor']) for r in rows)!=declared_net_minor:errors.append('Signed row total differs from declared net USD cents.')
    if closing_balance_minor-opening_balance_minor!=declared_net_minor:errors.append('Closing minus opening balance or accumulator must equal declared net.')
    return rows,errors[:2000]


def _metadata(statement):
    return {key:getattr(statement,key) for key in ('kind','account_id','statement_id','manifest_id','period_start','period_end','opening_balance_minor','closing_balance_minor','declared_row_count','declared_net_minor')}


@transaction.atomic
def stage_import(actor,merchant,content,filename='',**metadata):
    from operations.audit import record
    actor_role(actor,{'RECONCILIATION'})
    if not isinstance(content,bytes):content=bytes(content)
    if len(content)>5*1024*1024:raise ValidationError('The CSV exceeds 5 MiB.')
    from merchants.models import Merchant
    from operations.models import Export
    Merchant.objects.select_for_update().get(pk=merchant.pk)
    kind=metadata['kind'];account_id=str(metadata['account_id'])
    provider=str(getattr(merchant,'provider_account_id',''))
    if kind!='BANK' and account_id!=provider:raise ValidationError('Provider account does not belong to the selected merchant.')
    file_hash=hashlib.sha256(content).hexdigest()
    existing=StatementImport.objects.filter(merchant=merchant,kind=kind,account_id=account_id,file_hash=file_hash,statement_id=metadata['statement_id']).first()
    if existing:
        if any(getattr(existing,k)!=v for k,v in metadata.items()):raise ValidationError('An identical CSV was already staged with different immutable statement metadata.')
        return existing
    if StatementImport.objects.filter(merchant=merchant,status__in=['STAGED','VALIDATED']).count()+Export.objects.filter(merchant=merchant,status__in=['QUEUED','BUILDING']).count()>=10:
        raise ValidationError('This merchant has reached the ten open import/export work limit. Confirm, cancel or wait for existing work.')
    rows,errors=parse_statement(content,kind,account_id,metadata['period_start'],metadata['period_end'],metadata['declared_row_count'],metadata['declared_net_minor'],metadata['opening_balance_minor'],metadata['closing_balance_minor'])
    statement=StatementImport.objects.create(merchant=merchant,uploader=actor,content=content,file_hash=file_hash,filename=filename[:200],parsed_rows=rows,errors=errors,status='INVALID' if errors else 'VALIDATED',expires_at=timezone.now()+timedelta(hours=24),**metadata)
    record(actor=actor,merchant=merchant,action='statement.stage',resource_id=str(statement.pk),summary={'kind':kind,'status':statement.status,'rows':len(rows)})
    return statement


def fetch_manifest(statement):
    from operations.provider import request
    if not IDENTIFIER.fullmatch(statement.manifest_id):raise ValidationError('Invalid manifest identifier.')
    return request('GET','/v1/statements/'+quote(statement.manifest_id,safe='')+'/')


def verify_manifest(statement,manifest):
    errors=[]
    expected={
        'schema_version':1,'policy_version':'mprp_v1','manifest_id':statement.manifest_id,
        'statement_id':statement.statement_id,'kind':statement.kind,
        'period_start':statement.period_start.astimezone(dt_timezone.utc).isoformat(),
        'period_end':statement.period_end.astimezone(dt_timezone.utc).isoformat(),
        'opening_balance_minor':statement.opening_balance_minor,'closing_balance_minor':statement.closing_balance_minor,
        'net_minor':statement.declared_net_minor,
    }
    for key,value in expected.items():
        actual=manifest.get(key)
        if key in ('period_start','period_end'):
            try:actual=parse_datetime(str(actual)).astimezone(dt_timezone.utc).isoformat()
            except (AttributeError,ValueError):actual=None
        if actual!=value:errors.append(f'Manifest {key} does not match the staged declaration.')
    if str(manifest.get('provider_account_id',''))!=str(statement.merchant.provider_account_id):
        errors.append('Manifest belongs to another merchant provider account.')
    if str(manifest.get('account_id',manifest.get('bank_account_id' if statement.kind=='BANK' else 'provider_account_id','')))!=statement.account_id:
        errors.append('Manifest account does not match the statement account.')
    if manifest.get('row_count',manifest.get('count'))!=statement.declared_row_count:errors.append('Manifest row count differs.')
    try:
        provider_rows=[canonical_row(row,statement.kind) for row in manifest.get('rows',[])]
        expected_rows={r['line_id']:r for r in provider_rows}
        local_rows={r['line_id']:r for r in statement.parsed_rows}
        if len(expected_rows)!=len(provider_rows):errors.append('Manifest has duplicate row identities.')
        for line_id in sorted(expected_rows.keys()|local_rows.keys()):
            if expected_rows.get(line_id)!=local_rows.get(line_id):errors.append(f'Manifest content differs for line {line_id}.')
            if len(errors)>=2000:break
    except (ValidationError,TypeError,KeyError):errors.append('Manifest row schema is invalid.')
    return errors


def confirm_import(actor,statement,expected_version=None):
    actor_role(actor,{'RECONCILIATION'})
    if statement.status=='CONFIRMED':return statement
    if statement.status!='VALIDATED':raise ValidationError('Only a completely valid staged statement can be confirmed.')
    # External authentication happens outside the main financial transaction.
    manifest=fetch_manifest(statement)
    errors=verify_manifest(statement,manifest)
    from merchants.models import Merchant
    from operations.audit import record
    with transaction.atomic():
        actor_role(actor,{'RECONCILIATION'})
        Merchant.objects.select_for_update().get(pk=statement.merchant_id)
        statement=StatementImport.objects.select_for_update().get(pk=statement.pk)
        if statement.status=='CONFIRMED':return statement
        if expected_version is not None and statement.version!=expected_version:raise ValidationError('Statement changed. Refresh before confirming.')
        if timezone.now()>=statement.expires_at:
            statement.status='EXPIRED';statement.save(update_fields=['status']);raise ValidationError('This staged import has expired.')
        if statement.status!='VALIDATED':raise ValidationError('This import is no longer available for confirmation.')
        if errors:
            statement.status='QUARANTINED';statement.errors=errors;statement.manifest=manifest;statement.save(update_fields=['status','errors','manifest']);return statement
        conflicts=[]
        for row in statement.parsed_rows:
            line=StatementLine.objects.filter(merchant=statement.merchant,kind=statement.kind,account_id=statement.account_id,line_id=row['line_id']).first()
            if line and line.row_hash!=digest(row):conflicts.append(f'Existing line {row["line_id"]} has different immutable values.')
        if conflicts:
            statement.status='QUARANTINED';statement.errors=conflicts;statement.save(update_fields=['status','errors']);return statement
        for index,row in enumerate(statement.parsed_rows,start=1):
            bank=statement.kind=='BANK'
            line,_=StatementLine.objects.get_or_create(merchant=statement.merchant,kind=statement.kind,account_id=statement.account_id,line_id=row['line_id'],defaults={
                'source_id':row['transfer_reference'] if bank else row['source_id'],'event_type':'BANK_TRANSFER' if bank else row['event_type'],'reference':row['transfer_reference'] if bank else row['reference'],'batch_id':'' if bank else row['batch_id'],'occurred_at':parse_datetime(row['booked_at' if bank else 'occurred_at']),'amount_minor':int(row['amount_minor']),'row_hash':digest(row),'canonical_row':row})
            ImportRow.objects.create(statement=statement,line=line,row_index=index)
        statement.status='CONFIRMED';statement.manifest=manifest;statement.confirmed_at=timezone.now();statement.version+=1
        statement.save(update_fields=['status','manifest','confirmed_at','version'])
        record(actor=actor,merchant=statement.merchant,action='statement.confirm',resource_id=str(statement.pk),summary={'kind':statement.kind,'rows':statement.declared_row_count,'manifest_id':statement.manifest_id})
        return statement


@transaction.atomic
def cancel_import(actor,statement):
    from operations.audit import record
    actor_role(actor,{'RECONCILIATION'})
    statement=StatementImport.objects.select_for_update().get(pk=statement.pk)
    if statement.status=='CONFIRMED':raise ValidationError('Confirmed evidence cannot be withdrawn.')
    statement.status='CANCELLED';statement.content=b'';statement.parsed_rows=[];statement.version+=1
    statement.save(update_fields=['status','content','parsed_rows','version'])
    record(actor=actor,merchant=statement.merchant,action='statement.cancel',resource_id=str(statement.pk),summary={})
    return statement


def line_remaining(line):
    used=MatchAllocation.objects.filter(line=line,group__status='MATCHED').aggregate(n=Sum('amount_minor'))['n'] or 0
    return line.amount_minor-used


def source_remaining(source,kind):
    used=MatchAllocation.objects.filter(source=source,line__kind=kind,group__status='MATCHED').aggregate(n=Sum('amount_minor'))['n'] or 0
    return source.amount_minor-used


def _period_open(merchant,occurred_at):
    month=occurred_at.astimezone(dt_timezone.utc).date().replace(day=1)
    period=AccountingPeriod.objects.filter(merchant=merchant,month=month).first()
    if period and period.state in ('CLOSED','REOPEN_PROPOSED'):raise ValidationError('Closed-period evidence cannot be changed until an approved reopen.')


def compatible(line,source,aggregate=False):
    if line.merchant_id!=source.merchant_id or line.currency!=source.currency:return False
    if line.kind=='BANK':return source.event_type in ('SETTLEMENT','PAYOUT') and line.reference==source.reference
    if line.account_id!=source.provider_account_id:return False
    if line.event_type!=source.event_type:return False
    if source.event_type=='BATCH_ZERO':
        return line.kind=='PROCESSOR_TRANSFER' and line.amount_minor==source.amount_minor==0 and source.source_id==source.batch_id==line.source_id==line.batch_id
    if line.source_id==source.source_id and line.reference==source.reference:return True
    return aggregate and bool(source.batch_id) and line.source_id==source.batch_id and line.reference==source.batch_id


@transaction.atomic
def allocate_match(actor,merchant,allocations,reason=''):
    """Explicit one-to-many or many-to-one, same-direction exact-reference allocations."""
    from merchants.models import Merchant
    from operations.audit import record
    actor_role(actor,{'RECONCILIATION'})
    Merchant.objects.select_for_update().get(pk=merchant.pk)
    if not allocations or len(allocations)>10000:raise ValidationError('Select a bounded set of exact allocations.')
    line_ids=sorted({str(a['line_id']) for a in allocations});source_ids=sorted({str(a['source_id']) for a in allocations})
    lines={str(l.pk):l for l in StatementLine.objects.select_for_update().filter(merchant=merchant,pk__in=line_ids).order_by('pk')}
    sources={str(s.pk):s for s in EconomicSource.objects.select_for_update().filter(merchant=merchant,pk__in=source_ids).order_by('pk')}
    if len(lines)!=len(line_ids) or len(sources)!=len(source_ids):raise ValidationError('An allocation is outside the selected merchant scope.')
    if len(lines)>1 and len(sources)>1:raise ValidationError('Arbitrary many-to-many matching is not supported.')
    kinds={l.kind for l in lines.values()}
    if len(kinds)!=1:raise ValidationError('Match one statement layer at a time.')
    group=MatchGroup.objects.create(merchant=merchant,actor=actor,reason=reason[:500])
    line_totals={};source_totals={};pairs=set();signs=set()
    for item in allocations:
        line=lines[str(item['line_id'])];source=sources[str(item['source_id'])];amount=item['amount_minor']
        if isinstance(amount,bool) or not isinstance(amount,int):raise ValidationError('Allocation must be integer USD cents.')
        _period_open(merchant,line.occurred_at)
        if not compatible(line,source,aggregate=len(lines)==1 and len(sources)>1):raise ValidationError('Merchant, account, event kind, currency and exact reference must agree.')
        if not source.recognized and source.event_type!='BATCH_ZERO':raise ValidationError('An unrecognized economic correction or missing journal cannot be matched.')
        if source.event_type=='BATCH_ZERO':
            if amount or line.amount_minor or source.amount_minor:raise ValidationError('Zero evidence is nonmonetary.')
            if MatchAllocation.objects.filter(line=line,group__status='MATCHED').exists() or MatchAllocation.objects.filter(source=source,line__kind=line.kind,group__status='MATCHED').exists():raise ValidationError('Zero-batch evidence is already matched.')
        elif amount==0 or amount*line.amount_minor<=0 or amount*source.amount_minor<=0:raise ValidationError('Opposite-sign netting and zero-capacity allocations are prohibited.')
        if (line.pk,source.pk) in pairs:raise ValidationError('Duplicate allocation pair.')
        pairs.add((line.pk,source.pk));signs.add(1 if amount>0 else -1 if amount<0 else 0)
        line_totals[line.pk]=line_totals.get(line.pk,0)+amount;source_totals[source.pk]=source_totals.get(source.pk,0)+amount
        MatchAllocation.objects.create(group=group,line=line,source=source,amount_minor=amount)
    if len(signs)>1:raise ValidationError('Match groups must have a single direction.')
    # Exclude this newly created group to measure prior capacity.
    for line in lines.values():
        prior=MatchAllocation.objects.filter(line=line,group__status='MATCHED').exclude(group=group).aggregate(n=Sum('amount_minor'))['n'] or 0
        if abs(line_totals[line.pk])!=abs(line.amount_minor-prior):raise ValidationError('A matched group must completely allocate each selected statement line remaining amount.')
    for source in sources.values():
        prior=MatchAllocation.objects.filter(source=source,line__kind=next(iter(kinds)),group__status='MATCHED').exclude(group=group).aggregate(n=Sum('amount_minor'))['n'] or 0
        if abs(source_totals[source.pk])>abs(source.amount_minor-prior):raise ValidationError('The source has insufficient remaining evidence capacity.')
    for line in lines.values():
        for exc in ReconciliationException.objects.filter(line=line).exclude(state__in=['RESOLVED','ACCEPTED_NONFINANCIAL']):
            exc.state='RESOLVED';exc.resolved_at=timezone.now();exc.reason='Exact evidence allocation completed.';exc.save(update_fields=['state','resolved_at','reason'])
    record(actor=actor,merchant=merchant,action='reconciliation.match',resource_id=str(group.pk),summary={'lines':len(lines),'sources':len(sources),'amount_minor':sum(line_totals.values())})
    return group


def _matching_index(merchant):
    sources=list(EconomicSource.objects.filter(merchant=merchant).defer('payload'))
    index={}
    for source in sources:
        layer='PROCESSOR_TRANSFER' if source.event_type in ('SETTLEMENT','PAYOUT','BATCH_ZERO') else 'PROCESSOR_ECONOMIC'
        key=(layer,source.provider_account_id,'BATCH_ZERO',source.source_id) if source.event_type=='BATCH_ZERO' else (layer,source.provider_account_id,source.event_type,source.source_id,source.reference)
        index.setdefault(key,[]).append(source)
        if source.event_type in ('SETTLEMENT','PAYOUT'):
            index.setdefault(('BANK',source.reference),[]).append(source)
    return index


def _exception_for_line(line,category,amount):
    return ReconciliationException(merchant_id=line.merchant_id,category=category,source_reference=line.line_id,line=line,amount_minor=amount,reason='No unique recognized source with exact account, reference, direction and remaining cents is available.')


@transaction.atomic
def _allocate_automatic_batch(actor,merchant,pairs,guard=None):
    from merchants.models import Merchant
    from operations.audit import record
    actor_role(actor,{'RECONCILIATION'})
    Merchant.objects.select_for_update().get(pk=merchant.pk)
    if guard is not None and not guard():raise ValidationError('The matching worker lease is no longer current.')
    line_ids=[pair[0] for pair in pairs];source_ids=[pair[1] for pair in pairs]
    closed=set(AccountingPeriod.objects.select_for_update().filter(merchant=merchant,state__in=['CLOSED','REOPEN_PROPOSED']).order_by('month').values_list('month',flat=True))
    lines={str(line.pk):line for line in StatementLine.objects.select_for_update().filter(merchant=merchant,pk__in=line_ids).order_by('pk')}
    sources={str(source.pk):source for source in EconomicSource.objects.select_for_update().filter(merchant=merchant,pk__in=source_ids).order_by('pk')}
    used_lines={str(item['line_id']):(item['amount'] or 0,item['count']) for item in MatchAllocation.objects.filter(line_id__in=line_ids,group__status='MATCHED').values('line_id').annotate(amount=Sum('amount_minor'),count=Count('pk'))}
    used_sources={(str(item['source_id']),item['line__kind']):(item['amount'] or 0,item['count']) for item in MatchAllocation.objects.filter(source_id__in=source_ids,group__status='MATCHED').values('source_id','line__kind').annotate(amount=Sum('amount_minor'),count=Count('pk'))}
    groups=[];allocations=[];exceptions=[];matched_ids=[]
    for line_id,source_id in pairs:
        line=lines.get(str(line_id));source=sources.get(str(source_id))
        if line is None or source is None:continue
        used,count=used_lines.get(str(line.pk),(0,0));remaining=line.amount_minor-used
        if remaining==0 and (line.amount_minor or count):continue
        source_used,source_count=used_sources.get((str(source.pk),line.kind),(0,0))
        source_left=source.amount_minor-source_used
        if line.occurred_at.astimezone(dt_timezone.utc).date().replace(day=1) in closed:
            exceptions.append(_exception_for_line(line,'CLOSED_PERIOD',remaining));continue
        if not compatible(line,source) or (not source.recognized and source.event_type!='BATCH_ZERO') or remaining!=source_left or (not source.amount_minor and source_count):
            exceptions.append(_exception_for_line(line,'AMOUNT_DIFFERENCE',remaining));continue
        group=MatchGroup(merchant=merchant,actor=actor,reason='Exact automatic reference match')
        groups.append(group);allocations.append(MatchAllocation(group=group,line=line,source=source,amount_minor=remaining));matched_ids.append(line.pk)
        used_lines[str(line.pk)]=(line.amount_minor,count+1)
        used_sources[(str(source.pk),line.kind)]=(source_used+remaining,source_count+1)
    if groups:
        MatchGroup.objects.bulk_create(groups,batch_size=250)
        MatchAllocation.objects.bulk_create(allocations,batch_size=250)
        ReconciliationException.objects.filter(merchant=merchant,line_id__in=matched_ids).exclude(state__in=['RESOLVED','ACCEPTED_NONFINANCIAL']).update(state='RESOLVED',resolved_at=timezone.now(),reason='Exact evidence allocation completed.')
        record(actor=actor,merchant=merchant,action='reconciliation.match_batch',resource_id=str(groups[0].pk),summary={'groups':len(groups),'lines':len(matched_ids),'amount_minor':sum(a.amount_minor for a in allocations)})
    if exceptions:ReconciliationException.objects.bulk_create(exceptions,ignore_conflicts=True,batch_size=250)
    if guard is not None and not guard():raise ValidationError('The matching worker lease expired before commitment.')
    return len(groups),len(exceptions)


@transaction.atomic
def _store_matching_exceptions(actor,merchant,exceptions,guard=None):
    from merchants.models import Merchant
    actor_role(actor,{'RECONCILIATION'})
    Merchant.objects.select_for_update().get(pk=merchant.pk)
    if guard is not None and not guard():raise ValidationError('Matching worker ownership changed.')
    ReconciliationException.objects.bulk_create(exceptions,ignore_conflicts=True,batch_size=250)
    if guard is not None and not guard():raise ValidationError('Matching worker lease expired.')


def run_matching(actor,merchant,guard=None):
    """Prefetch evidence capacity and exact references; lock and bulk-commit bounded groups."""
    actor_role(actor,{'RECONCILIATION'})
    index=_matching_index(merchant)
    used_lines={str(item['line_id']):(item['amount'] or 0,item['count']) for item in MatchAllocation.objects.filter(line__merchant=merchant,group__status='MATCHED').values('line_id').annotate(amount=Sum('amount_minor'),count=Count('pk'))}
    used_sources={(str(item['source_id']),item['line__kind']):(item['amount'] or 0) for item in MatchAllocation.objects.filter(source__merchant=merchant,group__status='MATCHED').values('source_id','line__kind').annotate(amount=Sum('amount_minor'))}
    counts={'matched':0,'exceptions':0};pending=[];exceptions=[]
    for line in StatementLine.objects.filter(merchant=merchant).order_by('occurred_at','pk').iterator(chunk_size=500):
        used,n=used_lines.get(str(line.pk),(0,0));remaining=line.amount_minor-used
        if remaining==0 and (line.amount_minor or n):continue
        key=('BANK',line.reference) if line.kind=='BANK' else (line.kind,line.account_id,'BATCH_ZERO',line.batch_id) if line.event_type=='BATCH_ZERO' else (line.kind,line.account_id,line.event_type,line.source_id,line.reference)
        candidates=index.get(key,[])
        if len(candidates)==1 and (candidates[0].recognized or candidates[0].event_type=='BATCH_ZERO') and candidates[0].amount_minor-used_sources.get((str(candidates[0].pk),line.kind),0)==remaining:
            pending.append((str(line.pk),str(candidates[0].pk)))
            if len(pending)>=250:
                matched,blocked=_allocate_automatic_batch(actor,merchant,pending,guard=guard);counts['matched']+=matched;counts['exceptions']+=blocked;pending=[]
        else:
            category='AMBIGUOUS_REFERENCE' if len(candidates)>1 else 'AMOUNT_DIFFERENCE' if candidates else 'MISSING_LOCAL_EVENT'
            exceptions.append(_exception_for_line(line,category,remaining))
            if len(exceptions)>=250:
                _store_matching_exceptions(actor,merchant,exceptions,guard);counts['exceptions']+=len(exceptions);exceptions=[]
    if pending:
        matched,blocked=_allocate_automatic_batch(actor,merchant,pending,guard=guard);counts['matched']+=matched;counts['exceptions']+=blocked
    if exceptions:_store_matching_exceptions(actor,merchant,exceptions,guard);counts['exceptions']+=len(exceptions)
    return counts


@transaction.atomic
def reverse_match(actor,group,reason):
    from merchants.models import Merchant
    from operations.audit import record
    actor_role(actor,{'RECONCILIATION'})
    Merchant.objects.select_for_update().get(pk=group.merchant_id)
    group=MatchGroup.objects.select_for_update().get(pk=group.pk)
    if group.status=='REVERSED':return group
    if not reason.strip():raise ValidationError('A reversal reason is required.')
    for allocation in group.allocations.select_related('line'):_period_open(group.merchant,allocation.line.occurred_at)
    group.status='REVERSED';group.reason=reason[:500];group.reversed_at=timezone.now();group.version+=1;group.save(update_fields=['status','reason','reversed_at','version'])
    record(actor=actor,merchant=group.merchant,action='reconciliation.reverse',resource_id=str(group.pk),summary={'reason':reason[:500]})
    return group


def late_event_evidence(exception):
    """Return authoritative proof references, never a caller's monetary waiver."""
    sources=list(EconomicSource.objects.select_for_update().filter(merchant=exception.merchant,source_id=exception.source_reference).order_by('pk'))
    if not sources:raise ValidationError('The authenticated economic source is missing.')
    evidence=[]
    for source in sources:
        if not source.recognized or source.currency!='USD' or source.provider_account_id!=str(exception.merchant.provider_account_id) or not source.amount_minor:
            raise ValidationError('The late economic source is not fully recognized in its merchant account.')
        journals=list(Journal.objects.filter(merchant=exception.merchant,source=source,purpose=source.event_type).values_list('pk',flat=True))
        if not journals:raise ValidationError('The late event has no posted source-unique journal.')
        required=('PROCESSOR_TRANSFER','BANK') if source.event_type in ('SETTLEMENT','PAYOUT') else ('PROCESSOR_ECONOMIC',)
        allocations=list(MatchAllocation.objects.filter(source=source,group__status='MATCHED').select_related('line'))
        layers={}
        for kind in required:
            selected=[allocation for allocation in allocations if allocation.line.kind==kind]
            if not selected or source_remaining(source,kind)!=0 or any(item.line.merchant_id!=source.merchant_id or item.line.currency!='USD' for item in selected):
                raise ValidationError('Every required statement layer must have complete exact matched evidence before resolving a late event.')
            layers[kind]={'allocation_ids':[str(item.pk) for item in selected],'match_group_ids':sorted({str(item.group_id) for item in selected}),'statement_line_ids':sorted({str(item.line_id) for item in selected})}
        evidence.append({'source_record_id':str(source.pk),'source_id':source.source_id,'event_type':source.event_type,'journal_ids':list(map(str,journals)),'layers':layers})
    return evidence


@transaction.atomic
def update_exception(actor,exception,action,reason='',owner=None,proof=''):
    from operations.audit import record
    actor_role(actor,{'RECONCILIATION'})
    from merchants.models import Merchant
    Merchant.objects.select_for_update().get(pk=exception.merchant_id)
    exception=ReconciliationException.objects.select_for_update().get(pk=exception.pk)
    verified_evidence=[]
    if action=='assign':exception.owner=owner;exception.state='INVESTIGATING'
    elif action=='comment':
        if not reason.strip():raise ValidationError('Enter a comment.')
    elif action=='resolve':
        if exception.category=='LATE_EVENT':
            verified_evidence=late_event_evidence(exception)
            exception.state='RESOLVED';proof='Server-verified posted journals and complete required statement allocations.'
        else:
            if exception.amount_minor!=0 or exception.category not in ('DUPLICATE_NONFINANCIAL','IRRELEVANT_NONFINANCIAL') or not proof.strip():
                raise ValidationError('Monetary differences require corrected evidence, an exact match, or approved recognition. Reason-only closure is limited to proven nonfinancial duplicate or irrelevant evidence.')
            exception.state='ACCEPTED_NONFINANCIAL'
        exception.resolved_at=timezone.now()
    else:raise ValidationError('Unknown exception action.')
    exception.reason=reason[:2000];exception.history=[*exception.history,{'at':timezone.now().isoformat(),'actor':str(actor.pk),'action':action,'reason':reason[:2000],'proof':proof[:500],**({'verified_evidence':verified_evidence} if verified_evidence else {})}];exception.version+=1
    exception.save()
    record(actor=actor,merchant=exception.merchant,action='exception.'+action,resource_id=str(exception.pk),summary={'state':exception.state,'reason':reason[:500],**({'verified_evidence':verified_evidence} if verified_evidence else {})})
    return exception


@transaction.atomic
def propose_adjustment(actor,exception,source,rationale):
    from operations.audit import record
    actor_role(actor,{'RECONCILIATION'})
    if source.merchant_id!=exception.merchant_id or source.event_type not in ('CORRECTION_CREDIT','CORRECTION_DEBIT') or source.recognized:raise ValidationError('Select an unrecognized authenticated correction belonging to this merchant.')
    if not 1<=abs(source.amount_minor)<=100000000 or not rationale.strip():raise ValidationError('A bounded correction and rationale are required.')
    month=source.occurred_at.astimezone(dt_timezone.utc).date().replace(day=1)
    _period_open(source.merchant,source.occurred_at)
    payload={'merchant':str(source.merchant_id),'source':source.source_id,'event_type':source.event_type,'amount_minor':source.amount_minor,'posting_month':str(month),'rationale':rationale}
    proposal,created=AdjustmentProposal.objects.get_or_create(source=source,defaults={'merchant':source.merchant,'exception':exception,'maker':actor,'amount_minor':source.amount_minor,'posting_month':month,'rationale':rationale,'payload_hash':digest(payload)})
    if not created and proposal.payload_hash!=digest(payload):raise ValidationError('This source already has a frozen adjustment proposal.')
    record(actor=actor,merchant=source.merchant,action='adjustment.propose',resource_id=str(proposal.pk),summary={'amount_minor':source.amount_minor})
    return proposal


@transaction.atomic
def decide_adjustment(actor,proposal,approve,expected_version=None):
    _lock_people(actor,proposal.maker_id)
    from merchants.models import Merchant
    from operations.audit import record
    actor_role(actor,{'FINANCE_APPROVER'})
    Merchant.objects.select_for_update().get(pk=proposal.merchant_id)
    proposal=AdjustmentProposal.objects.select_for_update().select_related('source','exception','maker').get(pk=proposal.pk)
    if proposal.maker_id==actor.pk:raise PermissionDenied('A different person must approve the correction.')
    if not proposal.maker.is_active or getattr(proposal.maker,'platform_role','')!='RECONCILIATION':raise PermissionDenied('The maker no longer has authority.')
    if expected_version is not None and expected_version!=proposal.version:raise ValidationError('Proposal changed. Refresh the page.')
    if proposal.state!='PROPOSED':return proposal
    source=proposal.source
    _period_open(proposal.merchant,source.occurred_at)
    if approve:
        payload={'merchant':str(source.merchant_id),'source':source.source_id,'event_type':source.event_type,'amount_minor':source.amount_minor,'posting_month':str(proposal.posting_month),'rationale':proposal.rationale}
        if digest(payload)!=proposal.payload_hash or source.amount_minor!=proposal.amount_minor:raise ValidationError('The immutable correction payload no longer agrees.')
        post_economic_event(proposal.merchant,source.source_id,source.event_type,source.amount_minor,source.occurred_at,actor=actor,reference=source.reference,approved_correction=True,provider_account_id=source.provider_account_id)
        exc=proposal.exception;exc.state='RESOLVED';exc.resolved_at=timezone.now();exc.reason='Authoritative correction recognized after independent approval.';exc.save(update_fields=['state','resolved_at','reason'])
    proposal.state='APPROVED' if approve else 'REJECTED';proposal.checker=actor;proposal.decided_at=timezone.now();proposal.version+=1;proposal.save(update_fields=['state','checker','decided_at','version'])
    record(actor=actor,merchant=proposal.merchant,action='adjustment.'+proposal.state.lower(),resource_id=str(proposal.pk),summary={'maker':str(proposal.maker_id),'amount_minor':proposal.amount_minor})
    return proposal


def _event_instant(value):
    parsed=parse_datetime(str(value))
    if parsed is None or timezone.is_naive(parsed):raise ValidationError('Manifest instant requires a timezone.')
    return parsed.astimezone(dt_timezone.utc)


def month_bounds(month):
    start=datetime(month.year,month.month,1,tzinfo=dt_timezone.utc)
    endmonth=next_month(month)
    return start,datetime(endmonth.year,endmonth.month,1,tzinfo=dt_timezone.utc)


def month_manifests(merchant,month):
    from operations.provider import request
    start,end=month_bounds(month)
    manifests=[]
    def bounded_manifest(kind,left,right):
        query=urlencode({'provider_account_id':str(merchant.provider_account_id),'kind':kind,'period_start':left.isoformat(),'period_end':right.isoformat()})
        manifest=request('GET','/v1/statements/?'+query)
        if manifest.get('row_count',0)<=10000:
            manifests.append(manifest);return
        if right-left<=timedelta(seconds=1):raise ValidationError('Statement density exceeds the bounded import capacity; close remains blocked.')
        midpoint=left+(right-left)/2
        bounded_manifest(kind,left,midpoint);bounded_manifest(kind,midpoint,right)
    for kind in COLUMNS:bounded_manifest(kind,start,end)
    return manifests


def close_snapshot(period,manifests):
    merchant=period.merchant;start,end=month_bounds(period.month)
    blockers=[]
    if timezone.now()<end:blockers.append('This UTC calendar month has not ended.')
    if not trial_balance(merchant)['balanced']:blockers.append('Trial balance is not balanced.')
    if balances(merchant).get('adjustment_suspense'):blockers.append('Adjustment suspense must be zero.')
    if len(manifests)<3 or {m.get('kind') for m in manifests}!=set(COLUMNS):blockers.append('All three authenticated account manifest kinds are required, including zero-activity statements.')
    for kind in COLUMNS:
        intervals=[]
        for manifest in manifests:
            if manifest.get('kind')==kind:
                try:intervals.append((_event_instant(manifest.get('period_start')),_event_instant(manifest.get('period_end'))))
                except ValidationError:blockers.append('Manifest period is malformed.')
        boundary=start
        for left,right in sorted(intervals):
            if left!=boundary or right<=left or right>end:blockers.append(f'{kind} manifest intervals have a gap, overlap or incorrect cutoff.')
            boundary=right
        if boundary!=end:blockers.append(f'{kind} manifest coverage does not reach month end.')
    statement_ids=[]
    expected_source_keys=set()
    for manifest in manifests:
        if str(manifest.get('provider_account_id'))!=str(merchant.provider_account_id):
            blockers.append('Manifest provider account is inconsistent.');continue
        imports=StatementImport.objects.filter(merchant=merchant,status='CONFIRMED',manifest_id=str(manifest.get('manifest_id')))
        valid=[imp for imp in imports if not verify_manifest(imp,manifest)]
        if not valid:blockers.append(f'Confirmed manifest-verified {manifest.get("kind","unknown")} statement is missing.');continue
        statement_ids.extend(str(imp.pk) for imp in valid)
        for row in manifest.get('rows',[]):
            kind=manifest['kind']
            if kind=='BANK':
                source=EconomicSource.objects.filter(merchant=merchant,reference=row.get('transfer_reference'),event_type__in=['SETTLEMENT','PAYOUT']).first()
            else:
                source=EconomicSource.objects.filter(merchant=merchant,source_id=row.get('source_id'),event_type=row.get('event_type')).first()
            if source is None:blockers.append(f'Manifest source {row.get("source_id",row.get("transfer_reference"))} is missing locally.');continue
            expected_source_keys.add((source.source_id,source.event_type))
            if not source.recognized and source.event_type!='BATCH_ZERO':blockers.append(f'Economic source {source.source_id} has no recognized journal.')
            if source_remaining(source,kind)!=0 or (source.amount_minor==0 and not source.allocations.filter(line__kind=kind,group__status='MATCHED').exists()):blockers.append(f'Manifest source {source.source_id} lacks complete {kind} matching evidence.')
    sources=EconomicSource.objects.filter(merchant=merchant,occurred_at__gte=start,occurred_at__lt=end)
    for source in EconomicSource.objects.filter(merchant=merchant,occurred_at__lt=end):
        kinds=('PROCESSOR_TRANSFER','BANK') if source.event_type in ('SETTLEMENT','PAYOUT') else ('PROCESSOR_TRANSFER',) if source.event_type=='BATCH_ZERO' else ('PROCESSOR_ECONOMIC',)
        if not source.recognized and source.event_type!='BATCH_ZERO':blockers.append(f'Unrecognized source {source.source_id} affects this cutoff.')
        if any(source_remaining(source,kind)!=0 or (source.amount_minor==0 and not source.allocations.filter(line__kind=kind,group__status='MATCHED').exists()) for kind in kinds):blockers.append(f'Source {source.source_id} through cutoff lacks complete matched evidence.')
    timing_differences=[]
    from settlements.models import SettlementBatch
    batch_ids={row.get('batch_id') for m in manifests if m.get('kind')=='PROCESSOR_ECONOMIC' for row in m.get('rows',[])}
    for batch_id in batch_ids:
        try:batch=SettlementBatch.objects.filter(merchant=merchant,provider_id=batch_id).first()
        except (ValueError,ValidationError):batch=None
        if batch is None:blockers.append(f'Processor batch {batch_id} is missing locally.');continue
        detail=[item for item in batch.sources if item.get('event_type',item.get('kind')) not in ('SETTLEMENT','BATCH_ZERO')]
        if sum(item.get('amount_minor',0) for item in detail)!=batch.net_minor:blockers.append(f'Batch {batch_id} detail does not equal its signed net.')
        local_keys={str(item.get('source_id',item.get('id'))) for item in detail}
        expected_keys={row['source_id'] for m in manifests if m.get('kind')=='PROCESSOR_ECONOMIC' for row in m.get('rows',[]) if row.get('batch_id')==batch_id}
        if not expected_keys<=local_keys:blockers.append(f'Batch {batch_id} has incomplete source membership.')
        transfer=getattr(batch,'transfer',None)
        if batch.net_minor and (transfer is None or transfer.state!='SUCCEEDED' or transfer.amount_minor!=batch.net_minor):blockers.append(f'Batch {batch_id} lacks an exact successful transfer disposition.')
        elif batch.net_minor and transfer.occurred_at>=end:timing_differences.append({'batch_id':str(batch_id),'transfer_reference':transfer.reference,'amount_minor':transfer.amount_minor,'transferred_at':transfer.occurred_at.isoformat(),'meaning':'Clearing receivable or payable at this cutoff; transfer posts in the later UTC month.'})
        elif not batch.net_minor and transfer is not None:blockers.append(f'Zero batch {batch_id} must not have a transfer.')
    for source in sources:
        if (source.source_id,source.event_type) not in expected_source_keys:blockers.append(f'Local source {source.source_id} is absent from the cutoff manifests.')
    unresolved=ReconciliationException.objects.filter(merchant=merchant).exclude(state__in=['RESOLVED','ACCEPTED_NONFINANCIAL']).filter(created_at__lt=end)
    # A newly detected issue about an earlier source also blocks its cutoff.
    unresolved_ids=set(str(e.pk) for e in unresolved)
    for exc in ReconciliationException.objects.filter(merchant=merchant).exclude(state__in=['RESOLVED','ACCEPTED_NONFINANCIAL']):
        if exc.line_id and exc.line.occurred_at<end or sources.filter(source_id=exc.source_reference).exists():unresolved_ids.add(str(exc.pk))
    if unresolved_ids:blockers.append(f'{len(unresolved_ids)} unresolved reconciliation exception(s) affect this cutoff.')
    try:
        from operations.models import Operation
        pending=Operation.objects.filter(merchant=merchant).exclude(status__in=['SUCCEEDED','FAILED','CANCELLED'])
        if pending.filter(created_at__lt=end).exists():blockers.append('Unresolved economic operations affect this cutoff.')
    except (ImportError,AttributeError):
        blockers.append('Operation inventory is unavailable; close cannot establish completeness.')
    journals=Journal.objects.filter(merchant=merchant,period__month__lte=period.month)
    seq=journals.aggregate(n=Max('sequence'))['n'] or 0
    match_ids=list(MatchAllocation.objects.filter(line__merchant=merchant,line__occurred_at__lt=end,group__status='MATCHED').order_by('pk').values_list('pk',flat=True))
    snapshot={
        'month':str(period.month),'cutoff':end.isoformat(),'ledger_sequence':seq,
        'journals':list(map(str,journals.order_by('sequence').values_list('pk',flat=True))),
        'statement_ids':sorted(set(statement_ids)),'match_allocation_ids':list(map(str,match_ids)),
        'manifest_ids':sorted(str(m.get('manifest_id')) for m in manifests),
        'control_totals':balances(merchant,period_month=period.month),'trial_balance':trial_balance(merchant,period_month=period.month),'settlement_timing_differences':timing_differences,
        'exceptions':sorted(unresolved_ids),'blockers':sorted(set(blockers)),
        'source_high_water':{str(m.get('kind')):m.get('source_high_water') for m in manifests},
    }
    return snapshot


def propose_close(actor,period):
    actor_role(actor,{'RECONCILIATION'})
    manifests=month_manifests(period.merchant,period.month)
    from merchants.models import Merchant
    from operations.audit import record
    with transaction.atomic():
        _lock_people(actor,period.proposed_by_id)
        actor_role(actor,{'RECONCILIATION'})
        Merchant.objects.select_for_update().get(pk=period.merchant_id)
        period=AccountingPeriod.objects.select_for_update().select_related('merchant').get(pk=period.pk)
        if period.state not in ('OPEN','CLOSE_PROPOSED'):raise ValidationError('Only an open period can be proposed for close.')
        snapshot=close_snapshot(period,manifests)
        if snapshot['blockers']:raise ValidationError(snapshot['blockers'])
        period.state='CLOSE_PROPOSED';period.proposed_by=actor;period.proposal_snapshot=snapshot;period.proposal_hash=digest(snapshot);period.version+=1
        period.save(update_fields=['state','proposed_by','proposal_snapshot','proposal_hash','version'])
        record(actor=actor,merchant=period.merchant,action='period.propose_close',resource_id=str(period.pk),summary={'month':str(period.month),'hash':period.proposal_hash})
        return period


def approve_close(actor,period,expected_version=None,manifests=None):
    actor_role(actor,{'FINANCE_APPROVER'})
    if manifests is None:manifests=month_manifests(period.merchant,period.month)
    from merchants.models import Merchant
    from operations.audit import record
    with transaction.atomic():
        _lock_people(actor,period.proposed_by_id)
        actor_role(actor,{'FINANCE_APPROVER'})
        Merchant.objects.select_for_update().get(pk=period.merchant_id)
        period=AccountingPeriod.objects.select_for_update(of=('self',)).select_related('merchant','proposed_by').get(pk=period.pk)
        if period.state!='CLOSE_PROPOSED':raise ValidationError('This period has no current close proposal.')
        if period.proposed_by_id==actor.pk:raise PermissionDenied('A different person must approve period close.')
        if not period.proposed_by.is_active or getattr(period.proposed_by,'platform_role','')!='RECONCILIATION':raise PermissionDenied('The close maker no longer has authority.')
        if expected_version is not None and expected_version!=period.version:raise ValidationError('Stale period version. Refresh before approval.')
        snapshot=close_snapshot(period,manifests)
        if snapshot['blockers']:raise ValidationError(snapshot['blockers'])
        if digest(snapshot)!=period.proposal_hash:raise ValidationError('Cutoff evidence or controls changed after proposal. A new close proposal is required.')
        period.state='CLOSED';period.approved_by=actor;period.closed_at=timezone.now();period.version+=1
        period.save(update_fields=['state','approved_by','closed_at','version'])
        report=CloseReport.objects.create(period=period,version=period.close_reports.count()+1,snapshot=snapshot,snapshot_hash=period.proposal_hash,maker=period.proposed_by,checker=actor)
        record(actor=actor,merchant=period.merchant,action='period.close',resource_id=str(period.pk),summary={'report_id':str(report.pk),'month':str(period.month),'hash':period.proposal_hash})
        return period


@transaction.atomic
def propose_reopen(actor,period,reason):
    from merchants.models import Merchant
    from operations.audit import record
    actor_role(actor,{'RECONCILIATION'})
    Merchant.objects.select_for_update().get(pk=period.merchant_id)
    period=AccountingPeriod.objects.select_for_update().get(pk=period.pk)
    reason=reason.strip()
    if period.state!='CLOSED' or not reason or len(reason)>2000:raise ValidationError('A closed period and a correction reason of 1 through 2000 characters are required.')
    prior=period.close_reports.order_by('-version').first()
    period.proposal_snapshot={'action':'REOPEN','period_id':str(period.pk),'closed_version':period.version,'close_report_id':str(prior.pk) if prior else None,'reason':reason}
    period.proposal_hash=digest(period.proposal_snapshot)
    period.state='REOPEN_PROPOSED';period.proposed_by=actor;period.reason=reason;period.version+=1;period.save(update_fields=['state','proposed_by','reason','proposal_snapshot','proposal_hash','version'])
    record(actor=actor,merchant=period.merchant,action='period.propose_reopen',resource_id=str(period.pk),summary={'reason':period.reason})
    return period


@transaction.atomic
def approve_reopen(actor,period,expected_version=None):
    _lock_people(actor,period.proposed_by_id)
    from merchants.models import Merchant
    from operations.audit import record
    actor_role(actor,{'FINANCE_APPROVER'})
    Merchant.objects.select_for_update().get(pk=period.merchant_id)
    period=AccountingPeriod.objects.select_for_update(of=('self',)).select_related('proposed_by').get(pk=period.pk)
    if period.state!='REOPEN_PROPOSED':raise ValidationError('A current reopen proposal is required.')
    if actor.pk==period.proposed_by_id:raise PermissionDenied('A different person must approve reopening.')
    if not period.proposed_by.is_active or getattr(period.proposed_by,'platform_role','')!='RECONCILIATION':raise PermissionDenied('The maker no longer has authority.')
    if expected_version is not None and expected_version!=period.version:raise ValidationError('This reopen proposal has changed.')
    period.close_reports.filter(invalidated_at__isnull=True).update(invalidated_at=timezone.now())
    period.state='OPEN';period.proposal_hash='';period.proposal_snapshot={};period.approved_by=actor;period.version+=1;period.save(update_fields=['state','proposal_hash','proposal_snapshot','approved_by','version'])
    record(actor=actor,merchant=period.merchant,action='period.reopen',resource_id=str(period.pk),summary={'reason':period.reason})
    return period


@transaction.atomic
def create_period(actor,merchant,month):
    from merchants.models import Merchant
    from operations.audit import record
    actor_role(actor,{'RECONCILIATION'})
    Merchant.objects.select_for_update().get(pk=merchant.pk)
    if month.day!=1:raise ValidationError('Accounting periods must begin on the first day of a UTC calendar month.')
    if month>timezone.now().date().replace(day=1):raise ValidationError('Future accounting periods cannot be created manually.')
    period,created=AccountingPeriod.objects.get_or_create(merchant=merchant,month=month)
    if created:record(actor=actor,merchant=merchant,action='period.create',resource_id=str(period.pk),summary={'month':str(month)})
    return period


def stage_simulator_statement(actor,merchant,kind,period_start,period_end):
    from operations.provider import request
    actor_role(actor,{'RECONCILIATION'})
    if kind not in COLUMNS or timezone.is_naive(period_start) or timezone.is_naive(period_end) or not timedelta(0)<period_end-period_start<=timedelta(days=31) or period_end>timezone.now():raise ValidationError('Choose a completed UTC statement interval of at most 31 days.')
    query=urlencode({'provider_account_id':str(merchant.provider_account_id),'kind':kind,'period_start':period_start.isoformat(),'period_end':period_end.isoformat()})
    manifest=request('GET','/v1/statements/?'+query)
    if manifest.get('schema_version')!=1 or manifest.get('policy_version')!='mprp_v1' or str(manifest.get('provider_account_id'))!=str(merchant.provider_account_id):raise ValidationError('Simulator statement identity or schema does not match this merchant.')
    if manifest.get('row_count',10001)>10000:raise ValidationError('This interval exceeds 10000 rows. Choose a shorter interval.')
    output=io.StringIO(newline='');writer=csv.DictWriter(output,fieldnames=COLUMNS[kind],lineterminator='\n');writer.writeheader();writer.writerows(manifest['rows'])
    content=output.getvalue().encode('utf-8')
    return stage_import(actor,merchant,content,filename=f'{kind.lower()}-{period_start.date()}.csv',kind=kind,account_id=manifest['account_id'],statement_id=manifest['statement_id'],manifest_id=manifest['manifest_id'],period_start=period_start,period_end=period_end,opening_balance_minor=manifest['opening_balance_minor'],closing_balance_minor=manifest['closing_balance_minor'],declared_row_count=manifest['row_count'],declared_net_minor=manifest['net_minor'])
