import hashlib
import secrets
import time
from datetime import timedelta
import pyotp
from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.hashers import make_password, check_password
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from django.utils import timezone
from .models import User, AuthToken

def normalize_email(value):
    value=value.strip().lower()
    if not value.isascii() or len(value)>254 or value.count('@')!=1 or value.split('@')[-1] not in ('example.test','demo.invalid'):
        raise ValidationError('Use a fictional address at example.test or demo.invalid.')
    from django.core.validators import validate_email
    validate_email(value)
    return value

def encrypt(value): return Fernet(settings.FACTOR_ENCRYPTION_KEY.encode()).encrypt(value.encode()).decode()
def decrypt(value): return MultiFernet([Fernet(key.encode()) for key in [settings.FACTOR_ENCRYPTION_KEY,*getattr(settings,'FACTOR_ENCRYPTION_PREVIOUS_KEYS',[])]]).decrypt(value.encode()).decode()
def token_hash(value): return hashlib.sha256(value.encode()).hexdigest()

class EmailBackend(ModelBackend):
    def authenticate(self,request,username=None,password=None,**kwargs):
        email=(kwargs.get('email') or username or '').strip().lower()
        user=User.objects.filter(email=email).first()
        if user and user.check_password(password) and self.user_can_authenticate(user):return user
        User().set_password(password or '')

@transaction.atomic
def issue_token(kind,email,user=None,merchant=None,role=''):
    # All resends for an account share one row lock, including the empty-token
    # case. Locking only the previous token permits a new-row phantom race.
    if user is not None:user=User.objects.select_for_update().get(pk=user.pk)
    AuthToken.objects.filter(kind=kind,email=email,merchant=merchant,used_at=None).update(used_at=timezone.now())
    raw=secrets.token_urlsafe(32)
    expires=timezone.now()+timedelta(minutes=30 if kind=='reset' else 1440)
    obj=AuthToken.objects.create(kind=kind,email=email,user=user,merchant=merchant,role=role,token_hash=token_hash(raw),expires_at=expires)
    from operations.domain import mail, notify
    path={'verify':'verify','reset':'reset','invite':'invitations'}[kind]
    mail(email,'MPRP sandbox '+kind,f'Use this single-use sandbox link before {expires.isoformat()}: {settings.PUBLIC_ORIGIN}/accounts/{path}/{raw}/',expires,merchant=merchant)
    if user: notify(user,'Account '+kind+' requested','/accounts/profile/',event_key=str(obj.pk))
    return obj

@transaction.atomic
def consume_token(raw,kind):
    hint=AuthToken.objects.filter(token_hash=token_hash(raw),kind=kind).values('user_id').first()
    if hint is None:raise ValidationError('This link is invalid, expired, or already used.')
    if hint['user_id'] is None:raise ValidationError('This link is unavailable.')
    try:User.objects.select_for_update().get(pk=hint['user_id'])
    except User.DoesNotExist:raise ValidationError('This link is unavailable.') from None
    obj=AuthToken.objects.select_for_update().filter(token_hash=token_hash(raw),kind=kind,used_at=None).first()
    if not obj or obj.user_id!=hint['user_id'] or timezone.now()>=obj.expires_at: raise ValidationError('This link is invalid, expired, or already used.')
    obj.used_at=timezone.now();obj.save(update_fields=['used_at'])
    return obj

@transaction.atomic
def consume_totp(user,code):
    user=User.objects.select_for_update().get(pk=user.pk)
    if not user.totp_secret or not str(code).isascii() or len(str(code))!=6:return False
    totp=pyotp.TOTP(decrypt(user.totp_secret));counter=int(timezone.now().timestamp())//30
    for tick in range(counter-1,counter+2):
        if tick>user.last_totp_counter and secrets.compare_digest(totp.at(tick*30),str(code)):
            user.last_totp_counter=tick;user.save(update_fields=['last_totp_counter']);return True
    return False

@transaction.atomic
def consume_recovery(user,code):
    user=User.objects.select_for_update().get(pk=user.pk)
    for idx,digest in enumerate(user.recovery_hashes):
        if check_password(code,digest):
            user.recovery_hashes.pop(idx);user.auth_epoch+=1;user.save(update_fields=['recovery_hashes','auth_epoch']);return True
    return False

def recovery_codes(user):
    codes=[secrets.token_hex(8) for _ in range(10)]
    user.recovery_hashes=[make_password(x) for x in codes];user.save(update_fields=['recovery_hashes'])
    return codes

def require_step_up(request,reference=None):
    ts=request.session.get('step_up_at',0)
    if time.time() < ts or time.time()-ts>=300 or not ts or (reference and reference!=request.session.get('step_up_reference')):
        raise PermissionDenied('Confirm your password and a fresh authenticator code on the step-up page.')
    return True

def bind_session(request,user):
    request.session['auth_epoch']=user.auth_epoch
    request.session['absolute_start']=time.time()
    request.session['last_activity']=time.time()
    request.session['mfa_ok']=True
