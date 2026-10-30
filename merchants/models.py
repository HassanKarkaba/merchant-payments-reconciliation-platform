import uuid
from django.db import models
from django.conf import settings
class Merchant(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    name=models.CharField(max_length=100)
    timezone=models.CharField(max_length=64,default='UTC')
    status=models.CharField(max_length=20,default='PENDING',choices=[(x,x.title()) for x in ['PENDING','ACTIVE','SUSPENDED','REJECTED','CLOSING','CLOSED']])
    status_reason=models.CharField(max_length=2000,blank=True)
    provider_account_id=models.UUIDField(default=uuid.uuid4,unique=True)
    bank_account_id=models.CharField(max_length=64,blank=True)
    funds_version=models.PositiveBigIntegerField(default=0)
    ledger_sequence=models.PositiveBigIntegerField(default=0)
    version=models.PositiveIntegerField(default=1)
    closed_at=models.DateTimeField(null=True)
    created_at=models.DateTimeField(auto_now_add=True)
    @property
    def display_name(self): return self.name
    def __str__(self): return self.name
class Membership(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    merchant=models.ForeignKey(Merchant,on_delete=models.PROTECT,related_name='memberships')
    user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='memberships')
    role=models.CharField(max_length=20,choices=[('OWNER','Owner'),('FINANCE','Finance operator'),('ANALYST','Analyst')])
    active=models.BooleanField(default=True)
    version=models.PositiveIntegerField(default=1)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['merchant','user'],name='merchant_user_unique')]
