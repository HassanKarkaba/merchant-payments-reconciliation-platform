import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class LedgerAccount(models.Model):
    CODES = [(v, v.replace('_', ' ').title()) for v in ('processor_clearing','bank_control','merchant_payable','adjustment_suspense')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey('merchants.Merchant', on_delete=models.PROTECT)
    code = models.CharField(max_length=32, choices=CODES)
    currency = models.CharField(max_length=3, default='USD', editable=False)
    normal_side = models.CharField(max_length=6, choices=[('DEBIT','Debit'),('CREDIT','Credit')])
    class Meta:
        constraints = [models.UniqueConstraint(fields=['merchant','code'], name='ledger_account_scope'), models.CheckConstraint(condition=Q(currency='USD'), name='ledger_usd_only')]


class AccountingPeriod(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey('merchants.Merchant', on_delete=models.PROTECT)
    month = models.DateField()
    state = models.CharField(max_length=24, default='OPEN')
    proposal_hash = models.CharField(max_length=64, blank=True)
    proposal_snapshot = models.JSONField(default=dict)
    proposed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name='period_proposals')
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name='period_approvals')
    reason = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=1)
    closed_at = models.DateTimeField(null=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['merchant','month'], name='merchant_utc_month')]
        ordering = ['-month']


class EconomicSource(models.Model):
    """An authenticated provider observation. Uploaded CSV never creates these."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey('merchants.Merchant', on_delete=models.PROTECT)
    provider_account_id = models.CharField(max_length=128)
    source_id = models.CharField(max_length=128)
    event_type = models.CharField(max_length=32)
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, default='USD')
    occurred_at = models.DateTimeField()
    reference = models.CharField(max_length=128, blank=True)
    batch_id = models.CharField(max_length=128, blank=True)
    provider_version = models.PositiveBigIntegerField(default=1)
    payload = models.JSONField(default=dict)
    recognized = models.BooleanField(default=False)
    recorded_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['merchant','source_id','event_type'], name='economic_source_identity'), models.CheckConstraint(condition=Q(currency='USD'), name='economic_source_usd')]
        indexes = [models.Index(fields=['merchant','reference']), models.Index(fields=['merchant','occurred_at'])]


class Journal(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey('merchants.Merchant', on_delete=models.PROTECT)
    period = models.ForeignKey(AccountingPeriod, on_delete=models.PROTECT)
    source = models.ForeignKey(EconomicSource, on_delete=models.PROTECT)
    purpose = models.CharField(max_length=32)
    sequence = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3, default='USD')
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField(auto_now_add=True)
    explanation = models.CharField(max_length=500)
    late = models.BooleanField(default=False)
    reversal_of = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['source','purpose'], name='journal_economic_purpose'),models.UniqueConstraint(fields=['merchant','sequence'], name='journal_merchant_sequence'),models.CheckConstraint(condition=Q(currency='USD'), name='journal_usd_only')]
        ordering = ['-sequence']
    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Posted journals are immutable.')
        super().save(*args, **kwargs)
    def delete(self, *args, **kwargs):
        raise ValidationError('Posted journals are immutable.')


class JournalEntry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    journal = models.ForeignKey(Journal, on_delete=models.PROTECT, related_name='entries')
    account = models.ForeignKey(LedgerAccount, on_delete=models.PROTECT, related_name='entries')
    side = models.CharField(max_length=6, choices=[('DEBIT','Debit'),('CREDIT','Credit')])
    amount_minor = models.BigIntegerField()
    class Meta:
        constraints = [models.CheckConstraint(condition=Q(amount_minor__gt=0), name='entry_positive_cents'),models.CheckConstraint(condition=Q(side__in=['DEBIT','CREDIT']), name='entry_one_side')]
    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Posted entries are immutable.')
        super().save(*args, **kwargs)
    def delete(self, *args, **kwargs):
        raise ValidationError('Posted entries are immutable.')


class CloseReport(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    period = models.ForeignKey(AccountingPeriod, on_delete=models.PROTECT, related_name='close_reports')
    version = models.PositiveIntegerField()
    snapshot = models.JSONField()
    snapshot_hash = models.CharField(max_length=64)
    maker = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    checker = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    invalidated_at = models.DateTimeField(null=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['period','version'], name='close_report_version')]
