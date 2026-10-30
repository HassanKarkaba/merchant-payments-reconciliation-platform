import secrets
import time
import uuid
import pyotp
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction, IntegrityError
from django.shortcuts import render, redirect
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST, require_GET, require_http_methods
from merchants.models import Merchant, Membership
from operations.audit import record
from operations.domain import enqueue
from operations.protection import rate_limit, check_auth_budget, record_auth_failure
from .models import User, AuthToken
from .forms import RegistrationForm, LoginForm, CodeForm, StepUpForm, ResetForm, PasswordForm
from .domain import *

def form_page(request,title,form,**context):
    return render(request,'accounts/form.html',{'title':title,'form':form,**context}, status=400 if form is not None and getattr(form,'unknown_fields',None) else 200)
def safe_next(request):
    value=request.POST.get('next') or request.GET.get('next') or '/'
    return value if value.startswith('/') and not value.startswith('//') and url_has_allowed_host_and_scheme(value,{request.get_host()}) else '/'

@require_http_methods(['GET', 'POST'])
@sensitive_post_parameters()
def register(request):
    form=RegistrationForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        rate_limit('register:'+request.META.get('REMOTE_ADDR',''),10,900)
        with transaction.atomic():
            email=form.cleaned_data['email']
            if not User.objects.filter(email=email).exists():
                user=User(email=email,username=str(uuid.uuid4()));user.set_password(form.cleaned_data['password']);user.save()
                merchant=Merchant.objects.create(name=form.cleaned_data['merchant_name'],timezone=form.cleaned_data['timezone'])
                Membership.objects.create(user=user,merchant=merchant,role='OWNER')
                issue_token('verify',email,user,merchant=merchant)
                record(user,merchant,'account.registered',user.pk)
        messages.success(request,'If this fictional address can be registered, a verification link is in the local mailbox.')
        return redirect('accounts:login')
    return form_page(request,'Create your sandbox business',form,description='Use fictional identities only. Your merchant starts pending activation.')

@require_http_methods(['GET', 'POST'])
def verify(request,token):
    if request.method=='POST':
        try:
            with transaction.atomic():
                obj=consume_token(token,'verify');user=User.objects.select_for_update().get(pk=obj.user_id);user.verified=True;user.save(update_fields=['verified']);record(user,None,'account.verified',user.pk)
            messages.success(request,'Address verified. Sign in and enroll your authenticator.');return redirect('accounts:login')
        except ValidationError as exc:messages.error(request,' '.join(exc.messages))
    return form_page(request,'Verify your fictional address',None,button='Verify address')

@require_http_methods(['GET', 'POST'])
@sensitive_post_parameters()
def login_view(request):
    form=LoginForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        check_auth_budget(form.cleaned_data['email'], request.META.get('REMOTE_ADDR', ''))
        user=authenticate(request,email=form.cleaned_data['email'],password=form.cleaned_data['password'])
        if user:
            login(request,user);bind_session(request,user);request.session['mfa_ok']=False;request.session['safe_next']=safe_next(request)
            return redirect('accounts:mfa' if user.totp_confirmed else 'accounts:enroll')
        record_auth_failure(form.cleaned_data['email'], request.META.get('REMOTE_ADDR', ''))
        form.add_error(None,'Unable to sign in with those details.')
    return form_page(request,'Sign in to the sandbox',form,next=safe_next(request))

@login_required
@require_http_methods(['GET', 'POST'])
@sensitive_post_parameters()
def mfa(request):
    form=CodeForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        check_auth_budget(request.user.email, request.META.get('REMOTE_ADDR', ''))
        code=form.cleaned_data['code']
        if consume_totp(request.user,code) or consume_recovery(request.user,code):
            request.user.refresh_from_db();bind_session(request,request.user);request.session.cycle_key();return redirect(request.session.pop('safe_next','/'))
        record_auth_failure(request.user.email, request.META.get('REMOTE_ADDR', ''))
        form.add_error('code','This code is invalid or has already been used.')
    return form_page(request,'Confirm your authenticator',form)

@login_required
@require_http_methods(['GET', 'POST'])
@sensitive_post_parameters()
def enroll(request):
    if not request.user.verified:return redirect('accounts:profile')
    if request.user.totp_confirmed:return redirect('home')
    form=StepUpForm(request.POST or None)
    encrypted_enrollment=request.session.get('enrollment_secret')
    secret_value=decrypt(encrypted_enrollment) if encrypted_enrollment else None
    if form.unknown_fields:
        return form_page(request,'Set up your authenticator',form,enrollment=True)
    if request.method=='POST' and request.POST.get('begin'):
        check_auth_budget(request.user.email, request.META.get('REMOTE_ADDR', ''))
        if request.user.check_password(request.POST.get('password','')):
            secret_value=pyotp.random_base32();request.session['enrollment_secret']=encrypt(secret_value)
            return form_page(request,'Add your authenticator',form,secret=secret_value,uri=pyotp.TOTP(secret_value).provisioning_uri(request.user.email,issuer_name='MPRP Sandbox'))
        record_auth_failure(request.user.email, request.META.get('REMOTE_ADDR', ''))
        messages.error(request,'Password confirmation failed.')
    elif request.method=='POST' and form.is_valid() and secret_value:
        check_auth_budget(request.user.email, request.META.get('REMOTE_ADDR', ''))
        if request.user.check_password(form.cleaned_data['password']):
            with transaction.atomic():
                user=User.objects.select_for_update().get(pk=request.user.pk);user.totp_secret=encrypt(secret_value);user.save(update_fields=['totp_secret'])
                if consume_totp(user,form.cleaned_data['code']):
                    user.refresh_from_db();user.totp_confirmed=True;user.save(update_fields=['totp_confirmed']);codes=recovery_codes(user);record(user,None,'mfa.enrolled',user.pk)
                    request.session.pop('enrollment_secret',None);bind_session(request,user)
                    return render(request,'accounts/codes.html',{'codes':codes})
                user.totp_secret='';user.save(update_fields=['totp_secret'])
        record_auth_failure(request.user.email, request.META.get('REMOTE_ADDR', ''))
        form.add_error(None,'Password or authenticator code was not accepted.')
    return form_page(request,'Set up your authenticator',form,enrollment=True,has_secret=bool(secret_value),description='Confirm your password to show a secret once, then add it to an authenticator and confirm a fresh code.')

@login_required
@require_http_methods(['GET', 'POST'])
@sensitive_post_parameters()
def step_up(request):
    form=StepUpForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        check_auth_budget(request.user.email, request.META.get('REMOTE_ADDR', ''))
        if request.user.check_password(form.cleaned_data['password']) and consume_totp(request.user,form.cleaned_data['code']):
            request.session.cycle_key();request.session['step_up_at']=time.time();request.session['step_up_reference']=secrets.token_urlsafe(24)
            messages.success(request,'Sensitive approvals enabled for five minutes.');return redirect(safe_next(request))
        record_auth_failure(request.user.email, request.META.get('REMOTE_ADDR', ''))
        form.add_error(None,'Password or fresh code was not accepted.')
    return form_page(request,'Confirm a sensitive action',form,next=safe_next(request))

@login_required
@require_POST
def logout_view(request):logout(request);return redirect('accounts:login')
@login_required
@require_POST
def logout_all(request):
    with transaction.atomic():
        user=User.objects.select_for_update().get(pk=request.user.pk);user.auth_epoch+=1;user.save(update_fields=['auth_epoch']);record(user,None,'sessions.revoked',user.pk)
    logout(request);return redirect('accounts:login')

@require_http_methods(['GET', 'POST'])
@sensitive_post_parameters()
def reset(request,token=None):
    form=(PasswordForm if token else ResetForm)(request.POST or None)
    if request.method=='POST' and form.is_valid():
        if token:
            try:
                with transaction.atomic():
                    obj=consume_token(token,'reset');user=User.objects.select_for_update().get(pk=obj.user_id);user.set_password(form.cleaned_data['password']);user.auth_epoch+=1;user.save();record(user,None,'password.reset',user.pk)
                messages.success(request,'Password reset. Your authenticator is still required.');return redirect('accounts:login')
            except ValidationError as exc:form.add_error(None,exc)
        else:
            rate_limit('reset:'+request.META.get('REMOTE_ADDR',''),10,900)
            user=User.objects.filter(email=form.cleaned_data['email'].strip().lower(),is_active=True).first()
            if user:issue_token('reset',user.email,user)
            messages.success(request,'If the address is eligible, a reset link is in the local mailbox.');return redirect('accounts:login')
    return form_page(request,'Reset your password',form)

@login_required
@require_GET
def profile(request):return render(request,'accounts/profile.html')

@login_required
@require_http_methods(['GET', 'POST'])
@sensitive_post_parameters()
def codes(request):
    form=StepUpForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        check_auth_budget(request.user.email, request.META.get('REMOTE_ADDR', ''))
        if request.user.check_password(form.cleaned_data['password']) and consume_totp(request.user,form.cleaned_data['code']):
            with transaction.atomic():
                current=User.objects.select_for_update().get(pk=request.user.pk)
                codes=recovery_codes(current)
                record(current,None,'recovery_codes.rotated',current.pk)
            return render(request,'accounts/codes.html',{'codes':codes})
        record_auth_failure(request.user.email, request.META.get('REMOTE_ADDR', ''))
        form.add_error(None,'Password or fresh authenticator code was not accepted.')
    return form_page(request,'Replace recovery codes',form)

@require_http_methods(['GET', 'POST'])
@sensitive_post_parameters()
def invitation(request,token):
    # For invitations AuthToken.user is the issuing owner; other token kinds
    # bind that field to their receiving account. Unbound legacy invites expire.
    obj=AuthToken.objects.select_related('merchant').filter(token_hash=token_hash(token),kind='invite',used_at=None,expires_at__gt=timezone.now()).first()
    if not obj or not obj.user_id or obj.merchant.status in ['CLOSING','CLOSED'] or not Membership.objects.filter(merchant=obj.merchant,user_id=obj.user_id,active=True,role='OWNER',user__is_active=True,user__verified=True,user__totp_confirmed=True).exists():
        return form_page(request,'Invitation unavailable',None,description='This invitation is unavailable. Ask a current owner for a new invitation.')
    form=PasswordForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        recipient=User.objects.filter(email=obj.email).first()
        try:
            with transaction.atomic():
                ids=[obj.user_id]+([recipient.pk] if recipient else [])
                current={user.pk:user for user in User.objects.select_for_update().filter(pk__in=ids).order_by('pk')}
                issuer=current.get(obj.user_id)
                if not issuer or not issuer.is_active or not issuer.verified or not issuer.totp_confirmed:raise PermissionDenied('The issuing owner no longer has authority.')
                if recipient:
                    user=current.get(recipient.pk)
                    if not user or user.email!=obj.email or not user.is_active or not user.check_password(form.cleaned_data['password']):raise PermissionDenied('Use the invited active account and its current password.')
                else:
                    user=User(email=obj.email,username=str(uuid.uuid4()),verified=True)
                    user.set_password(form.cleaned_data['password']);user.save()
                merchant=Merchant.objects.select_for_update().get(pk=obj.merchant_id)
                if merchant.status in ['CLOSING','CLOSED']:raise PermissionDenied('This merchant is no longer accepting invitations.')
                if merchant.memberships.filter(active=True).count()>=100:raise ValidationError('The active membership limit has been reached.')
                members=list(Membership.objects.select_for_update().filter(merchant=merchant,user_id__in=[issuer.pk,user.pk]).order_by('pk'))
                if not any(member.user_id==issuer.pk and member.active and member.role=='OWNER' for member in members):raise PermissionDenied('The issuing owner no longer has authority.')
                existing=next((member for member in members if member.user_id==user.pk),None)
                if existing and existing.active:raise ValidationError('This account is already an active member. An invitation cannot change its role.')
                invitation=consume_token(token,'invite')
                if invitation.user_id!=issuer.pk or invitation.email!=user.email or invitation.merchant_id!=merchant.pk or invitation.role not in ['OWNER','FINANCE','ANALYST']:raise PermissionDenied('The invitation scope is no longer valid.')
                if existing:
                    existing.role=invitation.role;existing.active=True;existing.version+=1;existing.save(update_fields=['role','active','version'])
                else:Membership.objects.create(merchant=merchant,user=user,role=invitation.role,active=True)
                user.verified=True;user.save(update_fields=['verified'])
                record(user,merchant,'invitation.accepted',invitation.pk,{'issuer_id':str(issuer.pk),'role':invitation.role})
            messages.success(request,'Invitation accepted. Sign in to continue.');return redirect('accounts:login')
        except ValidationError as error:form.add_error(None,error)
        except IntegrityError:form.add_error(None,'The account or invitation changed while accepting. Reload and use the current invited account.')
    return form_page(request,'Join '+obj.merchant.name,form,description='Invited fictional address: '+obj.email)

@login_required
@require_POST
def deactivate(request):
    require_step_up(request)
    with transaction.atomic():
        user=User.objects.select_for_update().get(pk=request.user.pk)
        for membership in user.memberships.filter(active=True,role='OWNER').exclude(merchant__status='CLOSED').order_by('merchant_id'):
            Merchant.objects.select_for_update().get(pk=membership.merchant_id)
            if membership.merchant.memberships.filter(active=True,role='OWNER').count()<=1:raise PermissionDenied('Replace the last owner before deactivating.')
        previous_email=user.email
        user.is_active=False;user.auth_epoch+=1;user.deactivation_requested=True;user.email='deactivated+'+user.pk.hex+'@demo.invalid';user.first_name='';user.last_name='';user.totp_secret='';user.totp_confirmed=False;user.recovery_hashes=[];user.set_unusable_password();user.save();user.memberships.update(active=False);AuthToken.objects.filter(user=user,used_at=None).update(used_at=timezone.now());AuthToken.objects.filter(kind='invite',email=previous_email,used_at=None).update(used_at=timezone.now());record(user,None,'account.deactivated',user.pk)
    logout(request);return redirect('accounts:login')

@login_required
@require_POST
def resend_verification(request):
    if not request.user.verified:
        rate_limit('verification-resend:'+str(request.user.pk),3,900)
        merchant=request.user.memberships.filter(role='OWNER',active=True).select_related('merchant').first()
        issue_token('verify',request.user.email,request.user,merchant=merchant.merchant if merchant else None)
    messages.success(request,'If verification is still needed, a replacement link is queued in the local mailbox. Earlier unused links are invalid.')
    return redirect('accounts:profile')
