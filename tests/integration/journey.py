"""Run against a complete isolated verification environment, never development data."""
import os
import sys
import uuid
import time
import csv
import io
from datetime import timedelta
from urllib.parse import urlencode
sys.path.insert(0,str(__import__('pathlib').Path(__file__).resolve().parents[2]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
import django
django.setup()
from django.utils import timezone
from django.core.exceptions import ValidationError,PermissionDenied
from accounts.models import User
from merchants.models import Merchant,Membership
from payments.models import Payment,Refund
from payments import domain as payments
from settlements import domain as settlements
from accounting.domain import balances
from accounting.models import Journal
from operations.models import Operation,Control,Inbox
from operations import provider
from operations.audit import inspect
from reconciliation import domain as reconciliation
from reconciliation.models import StatementLine,MatchAllocation

if not os.environ.get('MPRP_VERIFY_PROJECT','').startswith('mprp_verify_'):
    raise SystemExit('Refusing to target anything except an isolated verification project')

def wait_for(function,description,seconds=60):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        result=function()
        if result:return result
        time.sleep(.2)
    raise AssertionError('Timed out waiting for '+description)

def fresh(model,pk,field,value):
    obj=model.objects.get(pk=pk)
    return obj if getattr(obj,field)==value else None

def actor(role,merchant=None):
    user=User.objects.create_user(username=str(uuid.uuid4()),email='verify_'+uuid.uuid4().hex+'@example.test',password=uuid.uuid4().hex+' test phrase',verified=True,totp_confirmed=True,platform_role=role if merchant is None else '')
    if merchant:Membership.objects.create(user=user,merchant=merchant,role=role)
    return user

wait_for(lambda:Control.objects.filter(provider_ready=True,worker_heartbeat__gte=timezone.now()-timedelta(seconds=30)).exists(),'worker and provider readiness')
merchant=Merchant.objects.create(name='Verification Juniper',status='ACTIVE')
owner=actor('OWNER',merchant);finance=actor('FINANCE',merchant);analyst=actor('RECONCILIATION');operator=actor('OPERATIONS')
account=provider.request('POST','/v1/accounts/',provider.envelope(merchant,'account-'+merchant.pk.hex,{'merchant_id':str(merchant.pk)}))
merchant.bank_account_id='bank_demo_'+merchant.provider_account_id.hex;merchant.save(update_fields=['bank_account_id'])
link,token=payments.create_link(finance,merchant,10000,'MANUAL',60)
browser_key=uuid.uuid4().hex;session=payments.get_checkout_session(link,browser_key)
payment=payments.confirm_checkout(token,session.pk,browser_key,session.attempt_key,'tok_demo_success','Fictional Customer')
# The request may return (payment,operation) across implementation versions.
if isinstance(payment,tuple):payment=payment[0]
payment=wait_for(lambda:fresh(Payment,payment.pk,'state','AUTHORIZED'),'authorization')
op=payments.capture(finance,merchant,payment.pk,10000,payment.version,uuid.uuid4())
payment=wait_for(lambda:fresh(Payment,payment.pk,'state','CAPTURED'),'capture')
assert payment.captured_minor==10000 and payment.fee_minor==320
assert balances(merchant)['merchant_payable']==9680
journal_count=Journal.objects.filter(merchant=merchant).count()
try:payments.capture(finance,merchant,payment.pk,10000,payment.version,uuid.uuid4())
except ValidationError:pass
else:raise AssertionError('Second final capture was admitted')
refund=payments.refund(finance,merchant,payment.pk,2500,'Synthetic partial return',payment.version,uuid.uuid4())
if isinstance(refund,tuple):refund=refund[0]
wait_for(lambda:fresh(Refund,refund.pk,'status','SUCCEEDED'),'refund')
assert balances(merchant)['merchant_payable']==7180
from tests.integration.fixture_control import flush_settlement
flush_settlement(operator,merchant)
wait_for(lambda:balances(merchant)['bank_control']==7180,'settlement and bank movement')
beneficiary=settlements.propose_beneficiary(finance,merchant,settlements.expected_beneficiary_token(merchant))
try:settlements.approve_beneficiary(finance,merchant,beneficiary.pk,beneficiary.version,True)
except PermissionDenied:pass
else:raise AssertionError('Self-approved beneficiary')
beneficiary=settlements.approve_beneficiary(owner,merchant,beneficiary.pk,beneficiary.version,True)
payout=settlements.propose_payout(finance,merchant,2000,beneficiary.version,'Synthetic verification payout',uuid.uuid4())
payout=settlements.approve_payout(owner,merchant,payout.pk,payout.version,True)
from settlements.models import Payout
wait_for(lambda:fresh(Payout,payout.pk,'status','PAID'),'payout')
assert balances(merchant)['merchant_payable']==5180 and balances(merchant)['bank_control']==5180
# Immutable provider manifests, CSV admission, and evidence-only matching.
now=timezone.now();start=now.replace(hour=0,minute=0,second=0,microsecond=0);end=timezone.now()
for kind in ['PROCESSOR_ECONOMIC','PROCESSOR_TRANSFER','BANK']:
    manifest=provider.request('GET','/v1/statements/?'+urlencode({'provider_account_id':str(merchant.provider_account_id),'kind':kind,'period_start':start.isoformat(),'period_end':end.isoformat()}))
    content=io.StringIO(newline='');writer=csv.DictWriter(content,fieldnames=reconciliation.COLUMNS[kind],lineterminator='\n');writer.writeheader();writer.writerows(manifest['rows'])
    meta={'kind':kind,'account_id':manifest['account_id'],'statement_id':manifest['statement_id'],'manifest_id':manifest['manifest_id'],'period_start':start,'period_end':end,'opening_balance_minor':manifest['opening_balance_minor'],'closing_balance_minor':manifest['closing_balance_minor'],'declared_row_count':manifest['row_count'],'declared_net_minor':manifest['net_minor']}
    statement=reconciliation.stage_import(analyst,merchant,content.getvalue().encode(),kind+'.csv',**meta)
    assert statement.status=='VALIDATED',statement.errors
    statement=reconciliation.confirm_import(analyst,statement,statement.version)
    assert statement.status=='CONFIRMED',statement.errors
before=Journal.objects.filter(merchant=merchant).count()
reconciliation.run_matching(analyst,merchant)
allocations=MatchAllocation.objects.filter(group__merchant=merchant).count()
assert allocations>=7,allocations
reconciliation.run_matching(analyst,merchant)
assert MatchAllocation.objects.filter(group__merchant=merchant).count()==allocations
assert Journal.objects.filter(merchant=merchant).count()==before
assert inspect(str(merchant.pk))
# Independent commit with lost authorization response must recover the same key.
link,token=payments.create_link(finance,merchant,1000,'AUTOMATIC',60)
key=uuid.uuid4().hex;session=payments.get_checkout_session(link,key)
lost=payments.confirm_checkout(token,session.pk,key,session.attempt_key,'tok_demo_timeout_after_commit')
if isinstance(lost,tuple):lost=lost[0]
wait_for(lambda:fresh(Payment,lost.pk,'state','CAPTURED'),'lost response recovery',90)
assert Operation.objects.filter(merchant=merchant,resource_id=lost.pk,kind='AUTHORIZE').count()==1
assert Journal.objects.filter(merchant=merchant,source__payload__payment_id=str(lost.pk),purpose='CAPTURE').count()<=1
print('PASS: independent HTTP authorization/capture/refund/settlement/payout/manifest/import/matching/lost-response/audit journey')
