import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone
class Control(models.Model):
    id=models.PositiveSmallIntegerField(primary_key=True,default=1)
    environment_id=models.UUIDField()
    main_instance_id=models.UUIDField(default=uuid.uuid4)
    run_token=models.UUIDField(default=uuid.uuid4)
    dispatch_epoch=models.PositiveBigIntegerField(default=0)
    simulator_instance_id=models.CharField(max_length=64,blank=True)
    received_cursor=models.PositiveBigIntegerField(default=0)
    restore_hold=models.BooleanField(default=True)
    hold_reason=models.TextField(default='Initial pairing required')
    release_maker=models.CharField(max_length=200,blank=True)
    release_checker=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True)
    worker_heartbeat=models.DateTimeField(null=True)
    last_clock=models.DateTimeField(null=True)
    provider_checked_at=models.DateTimeField(null=True)
    provider_ready=models.BooleanField(default=False)
class Operation(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    merchant=models.ForeignKey('merchants.Merchant',on_delete=models.PROTECT)
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True)
    kind=models.CharField(max_length=32)
    resource_id=models.UUIDField()
    action_key=models.CharField(max_length=128)
    fingerprint=models.CharField(max_length=64)
    provider_key=models.UUIDField(default=uuid.uuid4,unique=True,editable=False)
    payload=models.JSONField(default=dict)
    status=models.CharField(max_length=24,default='QUEUED')
    result=models.JSONField(default=dict)
    attempts=models.PositiveSmallIntegerField(default=0)
    due_at=models.DateTimeField(default=timezone.now)
    cancellation_requested=models.BooleanField(default=False)
    schema_version=models.PositiveSmallIntegerField(default=1)
    policy_version=models.CharField(max_length=20,default='mprp_v1')
    created_at=models.DateTimeField(auto_now_add=True)
    updated_at=models.DateTimeField(auto_now=True)
    completed_at=models.DateTimeField(null=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['merchant','actor','kind','resource_id','action_key'],name='operation_action_identity')]
        indexes=[models.Index(fields=['status','due_at'])]
class Job(models.Model):
    schema_version=models.PositiveSmallIntegerField(default=1)
    policy_version=models.CharField(max_length=20,default='mprp_v1')
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    merchant=models.ForeignKey('merchants.Merchant',on_delete=models.PROTECT,null=True)
    kind=models.CharField(max_length=40)
    payload=models.JSONField(default=dict)
    status=models.CharField(max_length=24,default='READY')
    due_at=models.DateTimeField(default=timezone.now)
    claim_generation=models.PositiveIntegerField(default=0)
    lease_owner=models.UUIDField(null=True)
    lease_until=models.DateTimeField(null=True)
    attempts=models.PositiveSmallIntegerField(default=0)
    last_error=models.CharField(max_length=100,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    updated_at=models.DateTimeField(auto_now=True)
    class Meta: indexes=[models.Index(fields=['status','due_at'])]
class AuditHead(models.Model):
    stream=models.CharField(primary_key=True,max_length=64)
    sequence=models.PositiveBigIntegerField(default=0)
    digest=models.CharField(max_length=64,default='0'*64)
class AuditEvent(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    stream=models.CharField(max_length=64)
    sequence=models.PositiveBigIntegerField()
    previous_hash=models.CharField(max_length=64)
    event_hash=models.CharField(max_length=64)
    actor_id_safe=models.CharField(max_length=64)
    merchant=models.ForeignKey('merchants.Merchant',on_delete=models.PROTECT,null=True)
    action=models.CharField(max_length=100)
    resource_id=models.CharField(max_length=100,blank=True)
    summary=models.JSONField(default=dict)
    reason=models.CharField(max_length=2000,blank=True)
    policy_version=models.CharField(max_length=20,default='mprp_v1')
    created_at=models.DateTimeField(default=timezone.now)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['stream','sequence'],name='audit_stream_sequence')]
        ordering=['-created_at']
class Notification(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4)
    recipient=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,db_constraint=False)
    merchant=models.ForeignKey('merchants.Merchant',on_delete=models.PROTECT,null=True)
    event_key=models.CharField(max_length=150)
    title=models.CharField(max_length=200)
    url=models.CharField(max_length=300,blank=True)
    read_at=models.DateTimeField(null=True)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta: constraints=[models.UniqueConstraint(fields=['recipient','event_key'],name='notification_identity')]
class Outbox(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4)
    merchant=models.ForeignKey('merchants.Merchant',on_delete=models.PROTECT,null=True)
    recipient=models.EmailField()
    subject=models.CharField(max_length=200)
    encrypted_body=models.TextField()
    expires_at=models.DateTimeField()
    status=models.CharField(max_length=20,default='PENDING')
    created_at=models.DateTimeField(auto_now_add=True)
class Inbox(models.Model):
    schema_version=models.PositiveSmallIntegerField(default=1)
    policy_version=models.CharField(max_length=20,default='mprp_v1')
    event_id=models.CharField(primary_key=True,max_length=100)
    payload_hash=models.CharField(max_length=64)
    payload=models.JSONField()
    status=models.CharField(max_length=20,default='PENDING')
    cursor=models.PositiveBigIntegerField(null=True)
    created_at=models.DateTimeField(auto_now_add=True)
class Nonce(models.Model):
    value=models.CharField(primary_key=True,max_length=150)
    expires_at=models.DateTimeField()
class RateBucket(models.Model):
    key=models.CharField(primary_key=True,max_length=100)
    count=models.PositiveIntegerField(default=0)
    expires_at=models.DateTimeField()
class Export(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4)
    requester=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    merchant=models.ForeignKey('merchants.Merchant',on_delete=models.PROTECT,null=True)
    kind=models.CharField(max_length=30,default='ledger')
    filters=models.JSONField(default=dict)
    status=models.CharField(max_length=20,default='QUEUED')
    content=models.BinaryField(null=True)
    cutoff_sequence=models.PositiveBigIntegerField(default=0)
    generated_at=models.DateTimeField(null=True)
    expires_at=models.DateTimeField(null=True)
    created_at=models.DateTimeField(auto_now_add=True)


class RecoveryInventory(models.Model):
    """An immutable reconciliation proposal precedes separate hold-release approval."""
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    maker=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='recovery_inventories')
    checker=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,related_name='recovery_approvals')
    environment_id=models.UUIDField()
    simulator_instance_id=models.CharField(max_length=64)
    dispatch_epoch=models.PositiveBigIntegerField()
    event_high_water=models.PositiveBigIntegerField(default=0)
    source_high_water=models.PositiveBigIntegerField(default=0)
    received_cursor=models.PositiveBigIntegerField(default=0)
    summary=models.JSONField(default=dict)
    findings=models.JSONField(default=list)
    fingerprint=models.CharField(max_length=64)
    reason=models.CharField(max_length=2000)
    status=models.CharField(max_length=24,default='RECONCILED')
    created_at=models.DateTimeField(auto_now_add=True)
    approved_at=models.DateTimeField(null=True)
    class Meta:
        constraints=[models.CheckConstraint(condition=models.Q(checker__isnull=True)|~models.Q(checker=models.F('maker')),name='recovery_distinct_checker')]


class PurgeReceipt(models.Model):
    """Safe permanent namespace retirement and resumable purge progress; no financial content."""
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    merchant_id_safe=models.UUIDField(unique=True)
    provider_account_id=models.UUIDField(unique=True)
    environment_id=models.UUIDField()
    closed_at=models.DateTimeField()
    eligible_at=models.DateTimeField()
    maker_id_safe=models.UUIDField(null=True)
    checker_id_safe=models.UUIDField(null=True)
    status=models.CharField(max_length=24,default='ELIGIBLE')
    inventory_hash=models.CharField(max_length=64,blank=True)
    dataset_counts=models.JSONField(default=dict)
    final_audit_heads=models.JSONField(default=dict)
    backup_inventory=models.JSONField(default=dict)
    simulator_receipt=models.JSONField(default=dict)
    stage=models.PositiveIntegerField(default=0)
    deleted_counts=models.JSONField(default=dict)
    findings=models.JSONField(default=list)
    approved_at=models.DateTimeField(null=True)
    completed_at=models.DateTimeField(null=True)
    created_at=models.DateTimeField(auto_now_add=True)
    updated_at=models.DateTimeField(auto_now=True)
    class Meta:
        constraints=[models.CheckConstraint(condition=models.Q(checker_id_safe__isnull=True)|~models.Q(checker_id_safe=models.F('maker_id_safe')),name='purge_distinct_checker')]


class ApprovalReceipt(models.Model):
    """Finalized local approval identity, retained with its financial tenant."""
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='approval_receipts')
    merchant=models.ForeignKey('merchants.Merchant',on_delete=models.PROTECT)
    kind=models.CharField(max_length=64)
    resource_id=models.UUIDField()
    action_key=models.CharField(max_length=128)
    fingerprint=models.CharField(max_length=64)
    result_ref=models.UUIDField()
    result_model=models.CharField(max_length=64)
    schema_version=models.PositiveSmallIntegerField(default=1)
    policy_version=models.CharField(max_length=20,default='mprp_v1')
    created_at=models.DateTimeField(default=timezone.now)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['actor','merchant','kind','resource_id','action_key'],name='approval_receipt_identity')]
