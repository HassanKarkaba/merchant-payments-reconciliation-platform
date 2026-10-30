import uuid
import secrets
from django.conf import settings
from django.db import models
from django.db.models import Q


def new_action_key():
    return secrets.token_hex(32)


class CheckoutLink(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey('merchants.Merchant', on_delete=models.PROTECT)
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, default='USD', editable=False)
    capture_mode = models.CharField(max_length=12, choices=[('AUTOMATIC', 'Automatic'), ('MANUAL', 'Manual')])
    expires_at = models.DateTimeField()
    status = models.CharField(max_length=16, default='ACTIVE')
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [models.CheckConstraint(condition=Q(amount_minor__gte=100, amount_minor__lte=100000000), name='checkout_amount_bounds'), models.CheckConstraint(condition=Q(currency='USD'), name='checkout_usd')]

    @property
    def state(self):
        return self.status


class Payment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey('merchants.Merchant', on_delete=models.PROTECT)
    link = models.OneToOneField(CheckoutLink, on_delete=models.PROTECT, related_name='payment')
    reference = models.CharField(max_length=64, unique=True)
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, default='USD')
    customer_name = models.CharField(max_length=100, blank=True)
    authorized_minor = models.BigIntegerField(default=0)
    captured_minor = models.BigIntegerField(default=0)
    refunded_minor = models.BigIntegerField(default=0)
    fee_minor = models.BigIntegerField(default=0)
    state = models.CharField(max_length=24, default='PROCESSING')
    provider_id = models.UUIDField(null=True, blank=True)
    provider_version = models.PositiveBigIntegerField(default=0)
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    auto_capture_pending = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [models.CheckConstraint(condition=Q(amount_minor__gte=100, amount_minor__lte=100000000), name='payment_amount_bounds'), models.CheckConstraint(condition=Q(currency='USD'), name='payment_usd'), models.CheckConstraint(condition=Q(captured_minor__gte=0, refunded_minor__gte=0) & Q(refunded_minor__lte=models.F('captured_minor')), name='payment_principal_conservation')]

    @property
    def refundable_minor(self):
        from disputes.models import Dispute
        disputed = Dispute.objects.filter(payment=self, state__in=['OPEN', 'RESPONDED', 'LOST']).values_list('principal_minor', flat=True).first() or 0
        reserved = self.refunds.filter(reserved=True).aggregate(total=models.Sum('amount_minor'))['total'] or 0
        return max(0, self.captured_minor - self.refunded_minor - reserved - disputed)


class CheckoutSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    link = models.ForeignKey(CheckoutLink, on_delete=models.PROTECT)
    browser_key_hash = models.CharField(max_length=64)
    attempt_key = models.CharField(max_length=64, default=new_action_key, unique=True)
    payment = models.OneToOneField(Payment, on_delete=models.PROTECT, null=True, blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)


class Refund(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey('merchants.Merchant', on_delete=models.PROTECT)
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name='refunds')
    operation = models.OneToOneField('operations.Operation', on_delete=models.PROTECT, related_name='refund', null=True)
    amount_minor = models.BigIntegerField()
    reason = models.TextField(max_length=2000)
    status = models.CharField(max_length=20, default='QUEUED')
    reserved = models.BooleanField(default=True)
    provider_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(amount_minor__gte=1, amount_minor__lte=100000000), name='refund_amount_bounds')]
