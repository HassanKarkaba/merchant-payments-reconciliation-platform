"""Independent synthetic provider records; this app never imports main-domain models."""
import uuid
from django.db import models
from django.db.models import Q


class Control(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    instance_id = models.UUIDField(default=uuid.uuid4, editable=False)
    environment_id = models.CharField(max_length=64, blank=True)
    main_instance_id = models.CharField(max_length=64, blank=True)
    run_token = models.CharField(max_length=128, blank=True)
    retired_tokens = models.JSONField(default=list)
    dispatch_epoch = models.PositiveBigIntegerField(default=0)
    acknowledged_cursor = models.PositiveBigIntegerField(default=0)
    next_source_sequence = models.PositiveBigIntegerField(default=1)
    next_event_sequence = models.PositiveBigIntegerField(default=1)
    restored = models.BooleanField(default=False)
    restore_marker_id = models.UUIDField(null=True)
    restore_operator = models.CharField(max_length=128, blank=True)
    restore_reason = models.CharField(max_length=2000, blank=True)
    restore_marked_at = models.DateTimeField(null=True)
    worker_heartbeat = models.DateTimeField(null=True)


class TransportNonce(models.Model):
    nonce = models.CharField(max_length=128, primary_key=True)
    expires_at = models.DateTimeField(db_index=True)


class ProviderAccount(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant_id = models.UUIDField(unique=True)
    environment_id = models.CharField(max_length=64)
    bank_balance_minor = models.BigIntegerField(default=0)
    retired = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def beneficiary_token(self):
        return f'acct_demo_{self.id.hex}'

    @property
    def bank_account_id(self):
        return f'bank_demo_{self.id.hex}'


class ProviderPayment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(ProviderAccount, on_delete=models.PROTECT)
    authorized_minor = models.BigIntegerField()
    captured_minor = models.BigIntegerField(default=0)
    refunded_minor = models.BigIntegerField(default=0)
    fee_minor = models.BigIntegerField(default=0)
    state = models.CharField(max_length=24, default='AUTHORIZED')
    version = models.PositiveBigIntegerField(default=1)
    authorization_expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(authorized_minor__gte=100, authorized_minor__lte=100000000), name='sim_authorization_bounds'),
            models.CheckConstraint(condition=Q(captured_minor__gte=0, refunded_minor__gte=0), name='sim_principal_nonnegative'),
            models.CheckConstraint(condition=Q(captured_minor__lte=models.F('authorized_minor')), name='sim_capture_bounded'),
            models.CheckConstraint(condition=Q(refunded_minor__lte=models.F('captured_minor')), name='sim_refund_bounded'),
        ]


class ProviderOperation(models.Model):
    key = models.CharField(max_length=128, primary_key=True)
    payload_hash = models.CharField(max_length=64, blank=True)
    kind = models.CharField(max_length=40)
    account = models.ForeignKey(ProviderAccount, on_delete=models.PROTECT, null=True)
    object_id = models.UUIDField(null=True)
    state = models.CharField(max_length=24)
    payload = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class SettlementBatch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(ProviderAccount, on_delete=models.PROTECT)
    cutoff_identity = models.CharField(max_length=100)
    cutoff = models.DateTimeField()
    net_minor = models.BigIntegerField(default=0)
    state = models.CharField(max_length=24, default='COMPLETED')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['account', 'cutoff_identity'], name='sim_unique_batch_cutoff')]


class EconomicSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sequence = models.PositiveBigIntegerField(unique=True)
    account = models.ForeignKey(ProviderAccount, on_delete=models.PROTECT)
    object_id = models.UUIDField(db_index=True)
    payment_id = models.UUIDField(null=True, db_index=True)
    event_type = models.CharField(max_length=32)
    amount_minor = models.BigIntegerField()
    occurred_at = models.DateTimeField(db_index=True)
    reference = models.CharField(max_length=64, unique=True)
    dependencies = models.JSONField(default=list)
    batch = models.ForeignKey(SettlementBatch, on_delete=models.PROTECT, null=True, related_name='economic_sources')
    clearing_line = models.BooleanField(default=True)
    reason = models.CharField(max_length=2000, blank=True)

    class Meta:
        ordering = ['sequence']
        indexes = [models.Index(fields=['account', 'occurred_at']), models.Index(fields=['account', 'batch'])]
        constraints = [models.CheckConstraint(condition=~Q(amount_minor=0), name='sim_source_nonzero')]


class BankTransfer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(ProviderAccount, on_delete=models.PROTECT)
    batch = models.OneToOneField(SettlementBatch, on_delete=models.PROTECT, null=True)
    kind = models.CharField(max_length=16)
    amount_minor = models.BigIntegerField()
    reference = models.CharField(max_length=64, unique=True)
    occurred_at = models.DateTimeField()
    beneficiary_token = models.CharField(max_length=64, blank=True)
    source = models.OneToOneField(EconomicSource, on_delete=models.PROTECT)


class ProviderDispute(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.OneToOneField(ProviderPayment, on_delete=models.PROTECT)
    account = models.ForeignKey(ProviderAccount, on_delete=models.PROTECT)
    principal_minor = models.BigIntegerField()
    state = models.CharField(max_length=24, default='OPEN')
    selected_outcome = models.CharField(max_length=16)
    version = models.PositiveBigIntegerField(default=1)
    opened_at = models.DateTimeField()
    deadline = models.DateTimeField()
    response = models.CharField(max_length=2000, blank=True)
    response_version = models.PositiveIntegerField(default=0)
    first_response_at = models.DateTimeField(null=True)
    decision_at = models.DateTimeField(null=True)


class ProviderEvent(models.Model):
    sequence = models.PositiveBigIntegerField(primary_key=True)
    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    account = models.ForeignKey(ProviderAccount, on_delete=models.PROTECT)
    body = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    delivered = models.BooleanField(default=False)
    attempts = models.PositiveIntegerField(default=0)
    due_at = models.DateTimeField(db_index=True)
    status = models.CharField(max_length=24, default='READY')


class StatementManifest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(ProviderAccount, on_delete=models.PROTECT)
    statement_id = models.CharField(max_length=64, unique=True)
    kind = models.CharField(max_length=32)
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    source_high_water = models.PositiveBigIntegerField()
    revision = models.PositiveIntegerField(default=1)
    content_hash = models.CharField(max_length=64, default='')
    manifest = models.JSONField()
    csv_content = models.BinaryField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['account', 'kind', 'period_start', 'period_end', 'revision'], name='sim_unique_statement_revision'), models.UniqueConstraint(fields=['account', 'kind', 'period_start', 'period_end', 'content_hash'], name='sim_unique_statement_content')]


class RetiredNamespace(models.Model):
    """Permanent safe receipt survives deletion and prohibits account or merchant reuse."""
    account_id = models.UUIDField(primary_key=True)
    merchant_id = models.UUIDField(unique=True)
    retirement_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    environment_id = models.CharField(max_length=64)
    merchant_closed_at = models.DateTimeField()
    approval_id = models.UUIDField()
    inventory_hash = models.CharField(max_length=64)
    counts = models.JSONField(default=dict)
    purged_counts = models.JSONField(default=dict)
    phase = models.PositiveSmallIntegerField(default=0)
    retired_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True)


class ControlAudit(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    approval_id=models.UUIDField(null=True,unique=True)
    action=models.CharField(max_length=48)
    operator_id=models.CharField(max_length=128)
    reason=models.CharField(max_length=2000)
    summary=models.JSONField(default=dict)
    created_at=models.DateTimeField(auto_now_add=True)
