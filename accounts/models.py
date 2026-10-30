import uuid
from django.db import models
from django.contrib.auth.models import AbstractUser
class User(AbstractUser):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    email=models.EmailField(unique=True)
    verified=models.BooleanField(default=False)
    platform_role=models.CharField(max_length=30,blank=True,choices=[(x,x.replace('_',' ').title()) for x in ['OPERATIONS','RECONCILIATION','FINANCE_APPROVER','AUDITOR']])
    auth_epoch=models.PositiveIntegerField(default=1)
    totp_secret=models.TextField(blank=True)
    totp_confirmed=models.BooleanField(default=False)
    last_totp_counter=models.BigIntegerField(default=-1)
    recovery_hashes=models.JSONField(default=list)
    deactivation_requested=models.BooleanField(default=False)
    USERNAME_FIELD='email'
    REQUIRED_FIELDS=['username']
class AuthToken(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    token_hash=models.CharField(max_length=64,unique=True)
    kind=models.CharField(max_length=20)
    email=models.EmailField()
    user=models.ForeignKey(User,on_delete=models.CASCADE,null=True)
    merchant=models.ForeignKey('merchants.Merchant',on_delete=models.CASCADE,null=True)
    role=models.CharField(max_length=20,blank=True)
    expires_at=models.DateTimeField()
    used_at=models.DateTimeField(null=True)
    created_at=models.DateTimeField(auto_now_add=True)
