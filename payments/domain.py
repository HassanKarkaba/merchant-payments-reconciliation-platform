"""Payment admission and authoritative processor-state application.

All principal changes share the merchant control lock. Provider calls happen in
operations' worker after an immutable operation has been durably admitted.
"""
import hashlib
import secrets
import uuid
from datetime import timedelta
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Sum, Exists, OuterRef, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from merchants.domain import locked_authority, require_role
from operations.audit import record
from .models import CheckoutLink, CheckoutSession, Payment, Refund

FINANCE = ('OWNER', 'FINANCE')
ACTIVE_OPERATIONS = ('QUEUED', 'DISPATCHING', 'UNKNOWN')
SCENARIOS = {'tok_demo_success', 'tok_demo_decline', 'tok_demo_timeout_after_commit', 'tok_demo_requires_review'}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def money(value, minimum=1, maximum=100000000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValidationError('Invalid simulated USD amount.')
    return value


def _admit(actor, merchant, kind, resource_id, action_key, payload):
    from operations.domain import admit_operation
    return admit_operation(actor=actor, merchant=merchant, kind=kind, resource_id=resource_id, action_key=action_key, payload=payload)


def create_link(actor, merchant, amount_minor, capture_mode, expiry_minutes=1440):
    money(amount_minor, minimum=100)
    if capture_mode not in ('AUTOMATIC', 'MANUAL') or type(expiry_minutes) is not int or not 5 <= expiry_minutes <= 10080:
        raise ValidationError('Invalid capture mode or link expiry.')
    with locked_authority(actor, merchant, FINANCE) as locked:
        if CheckoutLink.objects.filter(merchant=locked, status='ACTIVE', expires_at__gt=timezone.now()).count() >= 20:
            raise ValidationError('This merchant already has 20 active checkout links. Revoke an unused link before creating another.')
        token = secrets.token_urlsafe(32)
        link = CheckoutLink.objects.create(merchant=locked, creator=actor, token_hash=digest(token), amount_minor=amount_minor, capture_mode=capture_mode, expires_at=timezone.now() + timedelta(minutes=expiry_minutes))
        record(actor, locked, 'checkout_link.created', str(link.pk), {'amount_minor': amount_minor, 'capture_mode': capture_mode})
        return link, token


def revoke_link(actor, merchant, link_id):
    with locked_authority(actor, merchant, FINANCE) as locked:
        link = CheckoutLink.objects.select_for_update().get(pk=link_id, merchant=locked)
        if link.status == 'ACTIVE':
            link.status = 'REVOKED'
            link.version += 1
            link.save(update_fields=['status', 'version'])
            record(actor, locked, 'checkout_link.revoked', str(link.pk))
        return link


def get_checkout_session(link, browser_secret):
    with transaction.atomic():
        existing = CheckoutSession.objects.filter(link=link, browser_key_hash=digest(browser_secret)).order_by('-created_at').first()
        if existing:
            return existing
        return CheckoutSession.objects.create(link=link, browser_key_hash=digest(browser_secret), expires_at=link.expires_at)


def confirm_checkout(token, session_id, browser_secret, attempt_key, scenario, customer_name=''):
    if scenario not in SCENARIOS or len(customer_name) > 100:
        raise ValidationError('Choose a supported synthetic scenario.')
    link_hint = CheckoutLink.objects.select_related('merchant', 'creator').get(token_hash=digest(token))
    with locked_authority(link_hint.creator, link_hint.merchant, FINANCE) as merchant:
        link = CheckoutLink.objects.select_for_update().get(pk=link_hint.pk)
        checkout = CheckoutSession.objects.select_for_update().get(pk=session_id, link=link, browser_key_hash=digest(browser_secret), attempt_key=attempt_key)
        if checkout.payment_id:
            payment = checkout.payment
            op = _admit(link.creator, merchant, 'AUTHORIZE', payment.pk, attempt_key, {'payment_id': str(payment.pk), 'amount_minor': link.amount_minor, 'currency': 'USD', 'scenario': scenario, 'customer_name': customer_name, 'checkout_session_id': str(checkout.pk), 'provider_account_id': str(merchant.provider_account_id)})
            return payment, op
        if link.status != 'ACTIVE':
            raise ValidationError('This single-use link has already been used or revoked. Ask the merchant for a new link.')
        if timezone.now() >= link.expires_at or timezone.now() >= checkout.expires_at:
            raise ValidationError('This checkout link has expired. Ask the merchant for a new link.')
        payment = Payment.objects.create(merchant=merchant, link=link, reference=f'pay_{uuid.uuid4().hex}', amount_minor=link.amount_minor, customer_name=customer_name, auto_capture_pending=link.capture_mode == 'AUTOMATIC')
        payload = {'payment_id': str(payment.pk), 'amount_minor': link.amount_minor, 'currency': 'USD', 'scenario': scenario, 'customer_name': customer_name, 'checkout_session_id': str(checkout.pk), 'provider_account_id': str(merchant.provider_account_id)}
        op = _admit(link.creator, merchant, 'AUTHORIZE', payment.pk, attempt_key, payload)
        link.status = 'CONSUMED'
        link.version += 1
        link.save(update_fields=['status', 'version'])
        checkout.payment = payment
        checkout.save(update_fields=['payment'])
        record(link.creator, merchant, 'checkout.confirmed', str(payment.pk), {'amount_minor': payment.amount_minor, 'checkout_session_id': str(checkout.pk)})
        return payment, op


def _payment_action(actor, merchant, payment_id, expected_version):
    payment = Payment.objects.select_for_update().get(pk=payment_id, merchant=merchant)
    if payment.version != expected_version:
        raise ValidationError('This payment changed. Reload its detail page before submitting again.', code='stale_version')
    return payment


def _pending(payment, kinds):
    from operations.models import Operation
    return Operation.objects.filter(merchant=payment.merchant, resource_id=payment.pk, kind__in=kinds, status__in=ACTIVE_OPERATIONS).exists()


def _replay(actor, merchant, kind, resource_id, action_key, fields):
    from operations.models import Operation
    existing = Operation.objects.select_for_update().filter(actor=actor, merchant=merchant, kind=kind, resource_id=resource_id, action_key=str(action_key)).first()
    if existing:
        if any(existing.payload.get(key) != value for key, value in fields.items()):
            raise ValidationError('This action key was already used for different input.', code='key_conflict')
        return existing
    return None


def capture(actor, merchant, payment_id, amount_minor, expected_version, action_key):
    from accounting.domain import fee_for
    money(amount_minor, minimum=100)
    with locked_authority(actor, merchant, FINANCE) as locked:
        existing = _replay(actor, locked, 'CAPTURE', payment_id, action_key, {'amount_minor': amount_minor})
        if existing:
            return existing
        payment = _payment_action(actor, locked, payment_id, expected_version)
        if payment.state != 'AUTHORIZED' or payment.captured_minor or _pending(payment, ('CAPTURE', 'VOID')):
            raise ValidationError('Only an authorized payment with no unresolved capture or void can be captured.')
        if amount_minor > payment.authorized_minor or not payment.authorization_expires_at or timezone.now() >= payment.authorization_expires_at:
            raise ValidationError('The amount exceeds the authorization or the authorization has expired.')
        payload = {'payment_id': str(payment.pk), 'amount_minor': amount_minor, 'fee_minor': fee_for(amount_minor), 'fee_policy': 'mprp_v1', 'currency': 'USD', 'provider_account_id': str(locked.provider_account_id)}
        op = _admit(actor, locked, 'CAPTURE', payment.pk, action_key, payload)
        record(actor, locked, 'capture.admitted', str(payment.pk), {'operation_id': str(op.pk), 'amount_minor': amount_minor})
        return op


def void(actor, merchant, payment_id, expected_version, action_key):
    with locked_authority(actor, merchant, FINANCE) as locked:
        existing = _replay(actor, locked, 'VOID', payment_id, action_key, {})
        if existing:
            return existing
        payment = _payment_action(actor, locked, payment_id, expected_version)
        if payment.state != 'AUTHORIZED' or payment.captured_minor or _pending(payment, ('CAPTURE', 'VOID')):
            raise ValidationError('A sent or unresolved capture cannot be cancelled by voiding. Inspect its result first.')
        op = _admit(actor, locked, 'VOID', payment.pk, action_key, {'payment_id': str(payment.pk), 'currency': 'USD', 'provider_account_id': str(locked.provider_account_id)})
        record(actor, locked, 'void.admitted', str(payment.pk), {'operation_id': str(op.pk)})
        return op


def refund(actor, merchant, payment_id, amount_minor, reason, expected_version, action_key):
    money(amount_minor)
    reason = reason.strip()
    if not reason or len(reason) > 2000:
        raise ValidationError('Provide a refund reason of up to 2,000 characters.')
    with locked_authority(actor, merchant, FINANCE) as locked:
        existing = _replay(actor, locked, 'REFUND', payment_id, action_key, {'amount_minor': amount_minor, 'reason': reason})
        if existing:
            return Refund.objects.get(operation=existing), existing
        payment = _payment_action(actor, locked, payment_id, expected_version)
        from disputes.models import Dispute
        if Dispute.objects.filter(payment=payment, state__in=['OPEN', 'RESPONDED']).exists():
            raise ValidationError('An open dispute blocks new refunds.')
        if payment.refunds.filter(status='SUCCEEDED').count() + payment.refunds.filter(reserved=True).count() >= 100:
            raise ValidationError('This payment has reached its 100 active or successful refund limit.')
        if amount_minor > payment.refundable_minor:
            raise ValidationError('This refund exceeds the remaining unreserved refundable principal.')
        item = Refund.objects.create(merchant=locked, payment=payment, amount_minor=amount_minor, reason=reason)
        op = _admit(actor, locked, 'REFUND', payment.pk, action_key, {'payment_id': str(payment.pk), 'refund_id': str(item.pk), 'amount_minor': amount_minor, 'currency': 'USD', 'reason': reason, 'provider_account_id': str(locked.provider_account_id)})
        item.operation = op
        item.save(update_fields=['operation'])
        locked.funds_version += 1
        locked.save(update_fields=['funds_version'])
        record(actor, locked, 'refund.admitted', str(item.pk), {'payment_id': str(payment.pk), 'amount_minor': amount_minor, 'operation_id': str(op.pk)})
        return item, op


def prepare_operation(op):
    """Called inside the worker's admission transaction before DISPATCHING."""
    with locked_authority(op.actor, op.merchant, FINANCE) as merchant:
        payment = Payment.objects.select_for_update().select_related('link').get(pk=op.resource_id, merchant=merchant)
        if op.kind == 'AUTHORIZE':
            if payment.link.status != 'CONSUMED' or payment.state not in ('PROCESSING', 'UNKNOWN'):
                raise ValidationError('Checkout is no longer eligible for authorization.')
        elif op.kind == 'CAPTURE':
            if payment.state != 'AUTHORIZED' or payment.captured_minor or not payment.authorization_expires_at or timezone.now() >= payment.authorization_expires_at:
                raise ValidationError('Authorization is no longer eligible for final capture.')
        elif op.kind == 'VOID':
            if payment.state != 'AUTHORIZED' or payment.captured_minor:
                raise ValidationError('Payment can no longer be voided.')
        elif op.kind == 'REFUND':
            item = Refund.objects.select_for_update().get(operation=op, merchant=merchant)
            from disputes.models import Dispute
            if not item.reserved or Dispute.objects.filter(payment=payment, state__in=['OPEN', 'RESPONDED']).exists():
                raise ValidationError('Refund reservation or dispute status changed before dispatch.')
        return dict(op.payload)


def _timestamp(value):
    if isinstance(value, str):
        value = parse_datetime(value)
    if value is not None and timezone.is_naive(value):
        raise ValidationError('Processor timestamps must include a UTC offset.')
    return value


@transaction.atomic
def apply_snapshot(merchant, snapshot, actor=None):
    """Ingest every economic source even when its aggregate version is old."""
    from accounting.domain import apply_economic_event
    snapshot = snapshot.get('snapshot', snapshot)
    if snapshot.get('object_type', 'payment') != 'payment' or str(snapshot.get('provider_account_id')) != str(merchant.provider_account_id) or snapshot.get('currency') != 'USD':
        raise ValidationError('Processor payment scope does not match the admitted request.')
    from merchants.models import Merchant
    merchant = Merchant.objects.select_for_update().get(pk=merchant.pk)
    object_id = snapshot.get('payment_id') or snapshot.get('object_id')
    payment = Payment.objects.select_for_update().get(pk=object_id, merchant=merchant)
    authorized = snapshot.get('authorized_minor', 0)
    captured = snapshot.get('captured_minor', 0)
    refunded = snapshot.get('refunded_minor', 0)
    fee = snapshot.get('fee_minor', 0)
    for amount in (authorized, captured, refunded, fee):
        money(amount, minimum=0)
    if authorized > payment.amount_minor or captured > authorized or refunded > captured or type(snapshot.get('version')) is not int:
        raise ValidationError('Processor principal or version is inconsistent.')
    if captured:
        from accounting.domain import fee_for
        if fee != fee_for(captured):
            raise ValidationError('Processor fee conflicts with the frozen fee policy. Inspect this operation.')
    if snapshot.get('state') not in ('PROCESSING', 'AUTHORIZED', 'CAPTURED', 'DECLINED', 'REJECTED', 'VOIDED', 'EXPIRED', 'CANCELLED'):
        raise ValidationError('Processor payment state is unsupported.')
    if authorized and not _timestamp(snapshot.get('authorization_expires_at')):
        raise ValidationError('Processor authorization is missing its expiry.')
    sources = snapshot.get('sources', [])
    if not isinstance(sources, list) or len(sources) > 10000:
        raise ValidationError('Processor history is malformed or exceeds the bounded snapshot.')
    if any(not isinstance(item, dict) or type(item.get('amount_minor')) is not int or item.get('currency') != 'USD' for item in sources):
        raise ValidationError('Processor economic history has an invalid representation.')
    kinds = lambda item: item.get('event_type') or item.get('kind')
    totals = {kind: sum(item.get('amount_minor', 0) for item in sources if kinds(item) == kind) for kind in ('CAPTURE', 'FEE', 'REFUND')}
    if totals['CAPTURE'] != captured or totals['FEE'] != -fee or totals['REFUND'] != -refunded:
        raise ValidationError('Processor history does not establish its aggregate principal and fee amounts.')
    source_ids = {str(item.get('source_id') or item.get('id')) for item in sources}
    for source in sources:
        if not isinstance(source, dict) or type(source.get('amount_minor')) is not int or source.get('currency') != 'USD':
            raise ValidationError('Processor economic history has an invalid representation.')
        if any(str(dependency) not in source_ids for dependency in source.get('dependencies', [])):
            raise ValidationError('Processor economic history is missing a prerequisite source.')
    for source in sorted(sources, key=lambda item: (item.get('sequence', 0), str(item.get('source_id', item.get('id', ''))))):
        kind = source.get('event_type') or source.get('kind')
        if kind in ('DISPUTE_OPEN', 'DISPUTE_OPENED', 'DISPUTE_WON', 'DISPUTE_LOST'):
            from disputes.domain import apply_dispute_event
            apply_dispute_event(merchant, source)
        elif kind in ('CAPTURE', 'CAPTURE_FEE', 'FEE', 'REFUND'):
            apply_economic_event(merchant, source)
    if snapshot['version'] > payment.provider_version:
        payment.provider_id = payment.pk
        payment.provider_version = snapshot['version']
        payment.authorized_minor = authorized
        payment.captured_minor = captured
        payment.refunded_minor = refunded
        payment.fee_minor = fee
        payment.state = snapshot.get('state', payment.state)
        payment.authorization_expires_at = _timestamp(snapshot.get('authorization_expires_at'))
        payment.version += 1
        if captured or payment.state in ('DECLINED', 'REJECTED', 'VOIDED', 'EXPIRED'):
            payment.auto_capture_pending = False
        payment.save()
        record(actor, merchant, 'payment.observed', str(payment.pk), {'state': payment.state, 'provider_version': payment.provider_version, 'captured_minor': captured, 'refunded_minor': refunded})
    return payment


def complete_operation(op, snapshot):
    payment = apply_snapshot(op.merchant, snapshot, actor=op.actor)
    if op.kind == 'REFUND':
        item = Refund.objects.select_for_update().get(operation=op)
        sources = snapshot.get('snapshot', snapshot).get('sources', [])
        if not any((source.get('event_type') or source.get('kind')) == 'REFUND' and str(source.get('object_id')) == str(item.pk) and source.get('amount_minor') == -item.amount_minor for source in sources):
            raise ValidationError('Processor refund result lacks its exact economic source.')
        item.status = 'SUCCEEDED'
        item.reserved = False
        item.provider_id = item.pk
        item.save(update_fields=['status', 'reserved', 'provider_id'])
        record(op.actor, op.merchant, 'refund.completed', str(item.pk), {'amount_minor': item.amount_minor})
    if payment.auto_capture_pending and payment.state == 'AUTHORIZED':
        ensure_automatic_capture(payment)
    return payment


def automatic_capture_candidates(limit=50):
    from operations.models import Operation
    blockers = Operation.objects.filter(merchant_id=OuterRef('merchant_id'), resource_id=OuterRef('pk')).filter(Q(kind='CAPTURE') | Q(kind='VOID', status__in=ACTIVE_OPERATIONS))
    return Payment.objects.filter(state='AUTHORIZED', auto_capture_pending=True).annotate(capture_already_admitted=Exists(blockers)).filter(capture_already_admitted=False).select_related('link', 'merchant').order_by('created_at', 'id')[:min(50, max(1, limit))]


def ensure_automatic_capture(payment):
    from accounting.domain import fee_for
    try:
        with locked_authority(payment.link.creator, payment.merchant, FINANCE) as merchant:
            payment = Payment.objects.select_for_update().select_related('link', 'merchant').get(pk=payment.pk)
            if payment.auto_capture_pending and payment.state == 'AUTHORIZED' and not _pending(payment, ('CAPTURE', 'VOID')):
                return _admit(payment.link.creator, merchant, 'CAPTURE', payment.pk, uuid.uuid5(payment.pk, 'automatic-final-capture'), {'payment_id': str(payment.pk), 'amount_minor': payment.authorized_minor, 'fee_minor': fee_for(payment.authorized_minor), 'fee_policy': 'mprp_v1', 'currency': 'USD', 'provider_account_id': str(merchant.provider_account_id)})
    except (ValidationError, PermissionDenied):
        # The durable eligibility flag survives temporary unavailability; the
        # worker may retry it, while every attempt rechecks current authority.
        return None
    return None


def fail_operation(op, reason='', cancelled=False):
    """Only a proven no-effect outcome or an unsent cancellation calls this."""
    if op.kind == 'REFUND':
        Refund.objects.filter(operation=op).update(status='CANCELLED' if cancelled else 'FAILED', reserved=False)
    if op.kind == 'CAPTURE':
        Payment.objects.filter(pk=op.resource_id, merchant=op.merchant).update(auto_capture_pending=False)
    if op.kind == 'AUTHORIZE':
        Payment.objects.filter(pk=op.resource_id).update(state='CANCELLED' if cancelled else 'DECLINED', auto_capture_pending=False)
    record(op.actor, op.merchant, 'payment_operation.cancelled' if cancelled else 'payment_operation.failed', str(op.pk), {'reason': str(reason)[:500]})
