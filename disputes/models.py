import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q

class Dispute(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    merchant=models.ForeignKey('merchants.Merchant',on_delete=models.PROTECT)
    payment=models.OneToOneField('payments.Payment',on_delete=models.PROTECT,related_name='dispute')
    provider_id=models.CharField(max_length=128,unique=True)
    principal_minor=models.BigIntegerField()
    state=models.CharField(max_length=16,default='OPEN')
    provider_version=models.PositiveBigIntegerField(default=1)
    opened_at=models.DateTimeField()
    deadline=models.DateTimeField()
    response_text=models.TextField(blank=True)
    response_version=models.PositiveIntegerField(default=0)
    first_responded_at=models.DateTimeField(null=True)
    version=models.PositiveIntegerField(default=1)
    updated_at=models.DateTimeField(auto_now=True)
    class Meta:
        constraints=[models.CheckConstraint(condition=Q(principal_minor__gt=0),name='dispute_positive_principal')]
        ordering=['deadline']

class DisputeResponse(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    dispute=models.ForeignKey(Dispute,on_delete=models.PROTECT,related_name='responses')
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    text=models.TextField()
    payload_hash=models.CharField(max_length=64)
    status=models.CharField(max_length=16,default='QUEUED')
    created_at=models.DateTimeField(auto_now_add=True)
    accepted_at=models.DateTimeField(null=True)
    provider_version=models.PositiveIntegerField(default=0)
