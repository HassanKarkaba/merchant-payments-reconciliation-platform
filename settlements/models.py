import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q
from payments.models import new_action_key


class Beneficiary(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey('merchants.Merchant', on_delete=models.PROTECT)
    token = models.CharField(max_length=64)
    version = models.PositiveIntegerField(default=1)
    maker = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='beneficiary_proposals')
    checker = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='beneficiary_approvals', null=True, blank=True)
    status = models.CharField(max_length=20, default='PROPOSED')
    payload_hash = models.CharField(max_length=64)
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [models.UniqueConstraint(fields=['merchant', 'version'], name='beneficiary_merchant_version'), models.UniqueConstraint(fields=['merchant'], condition=Q(status='ACTIVE'), name='one_active_beneficiary'), models.CheckConstraint(condition=Q(checker__isnull=True) | ~Q(checker=models.F('maker')), name='beneficiary_distinct_checker')]


class Payout(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey('merchants.Merchant', on_delete=models.PROTECT)
    maker = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='payout_proposals')
    checker = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='payout_approvals', null=True, blank=True)
    beneficiary = models.ForeignKey(Beneficiary, on_delete=models.PROTECT)
    beneficiary_version = models.PositiveIntegerField()
    amount_minor = models.BigIntegerField()
    reason = models.TextField(max_length=2000)
    payload_hash = models.CharField(max_length=64)
    version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=24, default='PROPOSED')
    reserved = models.BooleanField(default=False)
    funds_version = models.PositiveBigIntegerField(default=0)
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_expires_at = models.DateTimeField(null=True, blank=True)
    operation = models.OneToOneField('operations.Operation', on_delete=models.PROTECT, related_name='payout', null=True, blank=True)
    action_key = models.CharField(max_length=64, default=new_action_key, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [models.CheckConstraint(condition=Q(amount_minor__gte=1, amount_minor__lte=100000000), name='payout_amount_bounds'), models.CheckConstraint(condition=Q(checker__isnull=True) | ~Q(checker=models.F('maker')), name='payout_distinct_checker')]


class SettlementBatch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey('merchants.Merchant', on_delete=models.PROTECT)
    provider_id = models.UUIDField(unique=True)
    net_minor = models.BigIntegerField()
    cutoff = models.CharField(max_length=128)
    transfer_ref = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=24)
    sources = models.JSONField(default=list)
    occurred_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']


class Transfer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey('merchants.Merchant', on_delete=models.PROTECT)
    batch = models.OneToOneField(SettlementBatch, on_delete=models.PROTECT, null=True, blank=True, related_name='transfer')
    payout = models.OneToOneField(Payout, on_delete=models.PROTECT, null=True, blank=True, related_name='transfer')
    kind = models.CharField(max_length=16)
    reference = models.CharField(max_length=128, unique=True)
    amount_minor = models.BigIntegerField()
    state = models.CharField(max_length=20)
    occurred_at = models.DateTimeField()
