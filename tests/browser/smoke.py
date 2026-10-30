"""Real server, PostgreSQL, simulator and locked browsers. No browser authority shortcuts."""
import os
import sys
import time
import uuid
import secrets
import re
import argparse
from pathlib import Path
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--engine',choices=['chromium','firefox','webkit'],help='Run one browser engine; the default runs all three.')
arguments=parser.parse_args()
engines=[arguments.engine] if arguments.engine else ['chromium','firefox','webkit']
started=time.monotonic()
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
if not os.environ.get('MPRP_VERIFY_PROJECT','').startswith('mprp_verify_'):raise SystemExit('An isolated verification project is required')
# Playwright's synchronous facade owns an event loop in this fixture process.
# ORM setup/inspection remains synchronous; the separately running web process
# never receives this fixture-only setting.
os.environ['DJANGO_ALLOW_ASYNC_UNSAFE']='true'
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
import django
django.setup()
import pyotp
from playwright.sync_api import sync_playwright,expect
from accounts.models import User
from accounts.domain import encrypt
from merchants.models import Merchant,Membership
from payments.models import Payment
from settlements.models import Beneficiary,Payout
from operations import provider
from accounting.domain import balances
if not os.environ.get('MPRP_VERIFY_PROJECT','').startswith('mprp_verify_'):raise SystemExit('An isolated verification project is required')
URL='http://web:8000'
EVIDENCE=Path('/evidence');EVIDENCE.mkdir(exist_ok=True)

def progress(engine,phase):
    # Only fixed phase names and the validated engine appear here. Never log
    # identities, credentials, one-time codes, checkout URLs or provider tokens.
    print(f'PROGRESS browser {engine}: {phase} ({time.monotonic()-started:.1f}s)',flush=True)

def user(role,merchant=None):
    password=secrets.token_urlsafe(24);secret=pyotp.random_base32()
    person=User.objects.create_user(username=str(uuid.uuid4()),email='browser_'+uuid.uuid4().hex+'@example.test',password=password,verified=True,totp_confirmed=True,totp_secret=encrypt(secret),platform_role=role if merchant is None else '')
    if merchant:Membership.objects.create(user=person,merchant=merchant,role=role)
    return person,password,secret

def login(page,identity,engine,identity_label='finance'):
    person,password,secret=identity
    progress(engine,identity_label+' login navigation start')
    page.goto(URL+'/accounts/login/')
    progress(engine,identity_label+' login navigation complete; email fill start')
    page.get_by_label('Email',exact=True).fill(person.email)
    progress(engine,identity_label+' password fill start')
    page.get_by_label('Password',exact=True).fill(password)
    progress(engine,identity_label+' login submit start')
    page.get_by_role('button',name=re.compile(r'^Continue')).click()
    page.wait_for_url('**/accounts/mfa/')
    progress(engine,identity_label+' MFA navigation complete; code fill start')
    page.get_by_label('Authenticator or recovery code').fill(pyotp.TOTP(secret).now())
    progress(engine,identity_label+' MFA submit start')
    page.get_by_role('button',name=re.compile(r'^Continue')).click()
    page.wait_for_url('**/m/**/')
    progress(engine,identity_label+' login and MFA complete')

def stepup(page,identity,target,engine):
    person,password,secret=identity
    # Freshness is tested at the actual next TOTP counter, not by bypassing replay protection.
    progress(engine,'step-up fresh TOTP counter wait start')
    person.refresh_from_db()
    while int(time.time())//30<=person.last_totp_counter:time.sleep(.2)
    progress(engine,'step-up navigation start')
    page.goto(URL+'/accounts/step-up/?next='+target)
    progress(engine,'step-up navigation complete; password fill start')
    page.get_by_label('Password',exact=True).fill(password)
    progress(engine,'step-up code fill start')
    page.get_by_label('Fresh authenticator code').fill(pyotp.TOTP(secret).now())
    progress(engine,'step-up submit start')
    page.get_by_role('button',name=re.compile(r'^Continue')).click()
    page.wait_for_url('**'+target)
    progress(engine,'step-up complete')

def wait_state(model,pk,field,state):
    end=time.monotonic()+60
    while time.monotonic()<end:
        obj=model.objects.get(pk=pk)
        if getattr(obj,field)==state:return obj
        time.sleep(.2)
    raise AssertionError('A browser-created action did not reach '+state)

def no_overflow(page,width):
    page.set_viewport_size({'width':width,'height':900})
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth+1'),(page.url,width)

with sync_playwright() as p:
    for name in engines:
        progress(name,'launch start')
        browser=getattr(p,name).launch()
        progress(name,'launch complete; no-JS context creation start')
        context=browser.new_context(viewport={'width':1440,'height':1050},java_script_enabled=False)
        page=context.new_page()
        progress(name,'page creation complete; merchant and provider fixtures start')
        merchant=Merchant.objects.create(name='Browser Juniper '+name,status='ACTIVE')
        provider.request('POST','/v1/accounts/',provider.envelope(merchant,'account-'+merchant.pk.hex,{'merchant_id':str(merchant.pk)}))
        finance=user('FINANCE',merchant);owner=user('OWNER',merchant);operator=user('OPERATIONS')
        progress(name,'merchant and provider fixtures complete')
        login(page,finance,name)
        expect(page.get_by_role('heading',name='A clear view of your money.')).to_be_visible()
        for width in [360,768,1440]:no_overflow(page,width)
        page.set_viewport_size({'width':1440,'height':1050})
        if name=='chromium':page.screenshot(path=str(EVIDENCE/'overview-empty.png'),full_page=True)
        progress(name,'overview assertions complete; checkout link navigation start')
        page.goto(URL+f'/m/{merchant.pk}/links/new/');page.get_by_label('Amount',exact=True).fill('100.00');page.get_by_label('Capture mode').select_option('AUTOMATIC');page.get_by_label('Expiry minutes').fill('60');page.get_by_role('button',name='Create checkout link',exact=True).click()
        checkout=page.locator('#checkout-url').input_value()
        progress(name,'checkout link created; no-JS guest navigation start')
        guest=browser.new_context(viewport={'width':360,'height':850},java_script_enabled=False);customer=guest.new_page();customer.goto(checkout)
        assert Payment.objects.filter(merchant=merchant).count()==0,'GET checkout created a payment'
        progress(name,'guest checkout GET assertion complete; checkout fill and submit start')
        customer.get_by_label('Fictional customer name').fill('Fictional Guest');customer.get_by_label('Synthetic payment scenario').select_option('tok_demo_success');customer.get_by_role('button',name='Confirm simulated payment').click()
        customer.wait_for_url('**/checkout/result/**/')
        progress(name,'guest checkout submitted; automatic capture wait start')
        payment=Payment.objects.get(merchant=merchant);wait_state(Payment,payment.pk,'state','CAPTURED')
        customer.reload();expect(customer.get_by_role('heading',name='Payment captured',exact=True)).to_be_visible();no_overflow(customer,360)
        guest.close()
        progress(name,'checkout capture assertions complete; protected screens start')
        for path in [f'/m/{merchant.pk}/payments/',f'/m/{merchant.pk}/payments/{payment.pk}/',f'/m/{merchant.pk}/balances/',f'/m/{merchant.pk}/payouts/',f'/m/{merchant.pk}/disputes/',f'/m/{merchant.pk}/reports/']:
            response=page.goto(URL+path);assert response.status==200,(path,response.status)
            for width in [360,768,1440]:no_overflow(page,width)
        progress(name,'protected screen and width assertions complete')
        if name=='chromium':
            page.goto(URL+f'/m/{merchant.pk}/');page.screenshot(path=str(EVIDENCE/'overview.png'),full_page=True)
            progress(name,'export navigation and admission start')
            page.goto(URL+f'/m/{merchant.pk}/reports/');page.get_by_role('button',name='Export ledger CSV').click();page.wait_for_url('**/exports/**/')
            progress(name,'export admitted; publication wait start')
            end=time.monotonic()+30
            while time.monotonic()<end and page.get_by_role('link',name='Download CSV').count()==0:page.reload();time.sleep(.2)
            with page.expect_download() as download:
                page.get_by_role('link',name='Download CSV').click()
            assert download.value.suggested_filename=='sandbox-report.csv'
            progress(name,'export download assertion complete; settlement flush start')
            # Distinct maker/checker and real step-up through browser forms.
            from tests.integration.fixture_control import flush_settlement
            flush_settlement(operator[0],merchant)
            end=time.monotonic()+30
            while time.monotonic()<end and balances(merchant)['bank_control']!=9680:time.sleep(.2)
            assert balances(merchant)['bank_control']==9680
            progress(name,'settlement assertion complete; beneficiary proposal start')
            target=f'/m/{merchant.pk}/beneficiaries/new/'
            page.goto(URL+target);page.get_by_label('Synthetic beneficiary token').fill('acct_demo_'+merchant.provider_account_id.hex);page.get_by_role('button',name='Record beneficiary proposal').click()
            progress(name,'beneficiary proposal complete; no-JS checker context start')
            ownercontext=browser.new_context(java_script_enabled=False);checker=ownercontext.new_page();login(checker,owner,name,'checker');stepup(checker,owner,target,name)
            progress(name,'beneficiary approval start')
            checker.get_by_role('button',name='Approve version 1',exact=True).click()
            assert Beneficiary.objects.get(merchant=merchant).status=='ACTIVE'
            progress(name,'beneficiary approval assertion complete; payout proposal start')
            page.goto(URL+f'/m/{merchant.pk}/payouts/new/');page.get_by_label('Amount',exact=True).fill('10.00');page.get_by_label('Reason').fill('Synthetic browser payout');page.get_by_role('button',name='Record payout proposal').click()
            progress(name,'payout proposal complete; checker approval start')
            payout=Payout.objects.get(merchant=merchant);checker.goto(URL+f'/m/{merchant.pk}/payouts/{payout.pk}/');checker.get_by_role('button',name='Approve and reserve $10.00').click();wait_state(Payout,payout.pk,'status','PAID')
            checker.reload();expect(checker.get_by_text('Paid',exact=True)).to_be_visible();ownercontext.close()
            progress(name,'payout paid assertion complete; checker context closed')
        progress(name,'context and browser close start')
        context.close();browser.close()
        progress(name,'context and browser close complete')
        print('PASS browser '+name+': real sign-in/MFA, no-JS checkout, capture, protected screens,360/768/1440 widths',flush=True)
if 'chromium' in engines:
    print('PASS browser: Chromium also export download, maker/checker beneficiary and payout with real fresh TOTP step-up',flush=True)
