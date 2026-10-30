import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class StatementImport(models.Model):
    schema_version=models.PositiveSmallIntegerField(default=1)
    policy_version=models.CharField(max_length=20,default='mprp_v1')
    KINDS=[('PROCESSOR_ECONOMIC','Processor economic'),('PROCESSOR_TRANSFER','Processor transfers'),('BANK','Bank')]
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    merchant=models.ForeignKey('merchants.Merchant',on_delete=models.PROTECT)
    uploader=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    kind=models.CharField(max_length=24,choices=KINDS)
    account_id=models.CharField(max_length=128)
    statement_id=models.CharField(max_length=128)
    manifest_id=models.CharField(max_length=128)
    period_start=models.DateTimeField()
    period_end=models.DateTimeField()
    opening_balance_minor=models.BigIntegerField()
    closing_balance_minor=models.BigIntegerField()
    declared_row_count=models.PositiveIntegerField()
    declared_net_minor=models.BigIntegerField()
    file_hash=models.CharField(max_length=64)
    content=models.BinaryField()
    filename=models.CharField(max_length=200,blank=True)
    status=models.CharField(max_length=16,default='STAGED')
    parsed_rows=models.JSONField(default=list)
    errors=models.JSONField(default=list)
    manifest=models.JSONField(default=dict)
    created_at=models.DateTimeField(auto_now_add=True)
    expires_at=models.DateTimeField()
    confirmed_at=models.DateTimeField(null=True)
    version=models.PositiveIntegerField(default=1)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['merchant','kind','account_id','file_hash','statement_id'],name='identical_statement_import')]
        ordering=['-created_at']


class StatementLine(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    merchant=models.ForeignKey('merchants.Merchant',on_delete=models.PROTECT)
    kind=models.CharField(max_length=24)
    account_id=models.CharField(max_length=128)
    line_id=models.CharField(max_length=128)
    source_id=models.CharField(max_length=128)
    event_type=models.CharField(max_length=32)
    reference=models.CharField(max_length=128)
    batch_id=models.CharField(max_length=128,blank=True)
    occurred_at=models.DateTimeField()
    amount_minor=models.BigIntegerField()
    currency=models.CharField(max_length=3,default='USD')
    row_hash=models.CharField(max_length=64)
    canonical_row=models.JSONField()
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['merchant','account_id','kind','line_id'],name='statement_line_identity'),models.CheckConstraint(condition=Q(currency='USD'),name='statement_line_usd')]
        indexes=[models.Index(fields=['merchant','source_id']),models.Index(fields=['merchant','reference'])]


class ImportRow(models.Model):
    statement=models.ForeignKey(StatementImport,on_delete=models.PROTECT,related_name='associations')
    line=models.ForeignKey(StatementLine,on_delete=models.PROTECT,related_name='imports')
    row_index=models.PositiveIntegerField()
    class Meta:
        constraints=[models.UniqueConstraint(fields=['statement','row_index'],name='statement_row_position')]


class MatchGroup(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    merchant=models.ForeignKey('merchants.Merchant',on_delete=models.PROTECT)
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    status=models.CharField(max_length=16,default='MATCHED')
    reason=models.CharField(max_length=500,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    reversed_at=models.DateTimeField(null=True)
    version=models.PositiveIntegerField(default=1)
    class Meta:
        ordering=['-created_at']


class MatchAllocation(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    group=models.ForeignKey(MatchGroup,on_delete=models.PROTECT,related_name='allocations')
    line=models.ForeignKey(StatementLine,on_delete=models.PROTECT,related_name='allocations')
    source=models.ForeignKey('accounting.EconomicSource',on_delete=models.PROTECT,related_name='allocations')
    amount_minor=models.BigIntegerField()
    class Meta:
        constraints=[models.UniqueConstraint(fields=['group','line','source'],name='allocation_group_pair')]


class ReconciliationException(models.Model):
    STATES=[(v,v.replace('_',' ').title()) for v in ('OPEN','INVESTIGATING','WAITING_PROVIDER','RESOLVED','ACCEPTED_NONFINANCIAL')]
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    merchant=models.ForeignKey('merchants.Merchant',on_delete=models.PROTECT)
    category=models.CharField(max_length=48)
    source_reference=models.CharField(max_length=128)
    line=models.ForeignKey(StatementLine,null=True,on_delete=models.PROTECT)
    amount_minor=models.BigIntegerField(default=0)
    state=models.CharField(max_length=24,choices=STATES,default='OPEN')
    owner=models.ForeignKey(settings.AUTH_USER_MODEL,null=True,blank=True,on_delete=models.PROTECT,related_name='+')
    reason=models.TextField(blank=True)
    history=models.JSONField(default=list)
    created_at=models.DateTimeField(auto_now_add=True)
    resolved_at=models.DateTimeField(null=True)
    version=models.PositiveIntegerField(default=1)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['merchant','category','source_reference'],name='exception_source_category')]
        ordering=['-created_at']


class AdjustmentProposal(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    merchant=models.ForeignKey('merchants.Merchant',on_delete=models.PROTECT)
    source=models.OneToOneField('accounting.EconomicSource',on_delete=models.PROTECT)
    exception=models.ForeignKey(ReconciliationException,on_delete=models.PROTECT,related_name='adjustments')
    maker=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='+')
    checker=models.ForeignKey(settings.AUTH_USER_MODEL,null=True,on_delete=models.PROTECT,related_name='+')
    amount_minor=models.BigIntegerField()
    posting_month=models.DateField()
    rationale=models.TextField()
    payload_hash=models.CharField(max_length=64)
    state=models.CharField(max_length=16,default='PROPOSED')
    version=models.PositiveIntegerField(default=1)
    created_at=models.DateTimeField(auto_now_add=True)
    decided_at=models.DateTimeField(null=True)
