import secrets
import uuid
from datetime import datetime, time
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from merchants.domain import require_merchant
from operations.models import Operation
from . import domain
from .forms import ActionForm, CaptureForm, CheckoutForm, LinkForm, RefundForm
from .models import CheckoutLink, CheckoutSession, Payment


def see_other(url):
    response = HttpResponse(status=303)
    response['Location'] = url
    return response


def _errors(form, exc):
    form.add_error(None, exc)


def _payment(merchant, payment_id):
    return get_object_or_404(Payment.objects.select_related('link'), pk=payment_id, merchant=merchant)


def form_status(form):
    codes={error.code for errors in form.errors.as_data().values() for error in errors}
    return 503 if 'capability_unavailable' in codes else 409 if codes & {'stale_version','key_conflict'} else 200


@login_required
@require_GET
def payment_list(request, merchant_id):
    merchant = require_merchant(request.user, merchant_id)
    items = Payment.objects.filter(merchant=merchant)
    query = request.GET.get('q', '').strip()
    if len(query) > 100 or request.GET.get('sort', '-created_at') not in ('-created_at', 'created_at'):
        return HttpResponse('Invalid payment filter.', status=400)
    if query:
        items = items.filter(reference__icontains=query)
    state = request.GET.get('state', '').strip()
    if state:
        if state not in ('PROCESSING', 'AUTHORIZED', 'CAPTURED', 'DECLINED', 'VOIDED', 'EXPIRED', 'UNKNOWN', 'CANCELLED', 'REJECTED'):
            return HttpResponse('Invalid payment state.', status=400)
        items = items.filter(state=state)
    for name, lookup in [('from', 'created_at__date__gte'), ('to', 'created_at__date__lte')]:
        if request.GET.get(name):
            try:
                items = items.filter(**{lookup: datetime.strptime(request.GET[name], '%Y-%m-%d').date()})
            except ValueError:
                return HttpResponse('Dates must use YYYY-MM-DD.', status=400)
    if request.GET.get('amount'):
        from .forms import parse_amount
        try:
            items = items.filter(amount_minor=parse_amount(request.GET['amount']))
        except ValidationError:
            return HttpResponse('Invalid amount filter.', status=400)
    sort = request.GET.get('sort', '-created_at')
    items = items.order_by(sort, '-id' if sort.startswith('-') else 'id')
    try:
        size = min(100, max(1, int(request.GET.get('page_size', 25))))
    except ValueError:
        return HttpResponse('Invalid page size.', status=400)
    return render(request, 'payments/list.html', {'merchant': merchant, 'page_obj': Paginator(items, size).get_page(request.GET.get('page')), 'q': query, 'state_filter': state, 'links': CheckoutLink.objects.filter(merchant=merchant)[:10]})


@login_required
@require_GET
def payment_detail(request, merchant_id, payment_id):
    merchant = require_merchant(request.user, merchant_id)
    payment = _payment(merchant, payment_id)
    initial = {'expected_version': payment.version, 'action_key': secrets.token_hex(32), 'amount': f'{payment.authorized_minor // 100}.{payment.authorized_minor % 100:02d}'}
    return render(request, 'payments/detail.html', {'merchant': merchant, 'payment': payment, 'capture_form': CaptureForm(initial=initial, auto_id='capture_%s'), 'void_form': ActionForm(auto_id='void_%s', initial={'expected_version': payment.version, 'action_key': secrets.token_hex(32)}), 'operations': Operation.objects.filter(merchant=merchant, resource_id=payment.pk).order_by('created_at', 'id'), 'refunds': payment.refunds.select_related('operation').order_by('-created_at'), 'refundable_minor': payment.refundable_minor})


@login_required
@require_http_methods(['GET', 'POST'])
def link_new(request, merchant_id):
    merchant = require_merchant(request.user, merchant_id, domain.FINANCE)
    form = LinkForm(request.POST or None)
    if request.method == 'POST' and form.unknown_fields:
        return HttpResponse('Unsupported form fields.', status=400)
    if request.method == 'POST' and form.is_valid():
        try:
            link, token = domain.create_link(request.user, merchant, form.cleaned_data['amount'], form.cleaned_data['capture_mode'], form.cleaned_data['expiry_minutes'])
            from accounts.domain import encrypt
            request.session['issued_checkout_link'] = {'merchant': str(merchant.pk), 'link': str(link.pk), 'secret': encrypt(token)}
            return see_other(reverse('payments:link_issued', args=[merchant.pk]))
        except ValidationError as exc:
            _errors(form, exc)
    return render(request, 'payments/link_form.html', {'merchant': merchant, 'form': form})


@login_required
@require_GET
def link_issued(request, merchant_id):
    merchant = require_merchant(request.user, merchant_id, domain.FINANCE)
    retained = request.session.pop('issued_checkout_link', None)
    if not retained or retained['merchant'] != str(merchant.pk):
        messages.info(request, 'The checkout URL was already displayed. Create a new link if you did not save it.')
        return see_other(reverse('payments:list', args=[merchant.pk]))
    from accounts.domain import decrypt
    link = get_object_or_404(CheckoutLink, pk=retained['link'], merchant=merchant)
    checkout_url = request.build_absolute_uri(reverse('payments:checkout', args=[decrypt(retained['secret'])]))
    response = render(request, 'payments/link_issued.html', {'merchant': merchant, 'link': link, 'checkout_url': checkout_url})
    response['Cache-Control'] = 'no-store'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@login_required
@require_POST
def link_revoke(request, merchant_id, link_id):
    merchant = require_merchant(request.user, merchant_id, domain.FINANCE)
    get_object_or_404(CheckoutLink, pk=link_id, merchant=merchant)
    try:
        domain.revoke_link(request.user, merchant, link_id)
        messages.success(request, 'The checkout link has been revoked.')
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
    return see_other(reverse('payments:list', args=[merchant.pk]))


def _guest_context(request, token):
    if len(token) > 128:
        raise Http404
    link = get_object_or_404(CheckoutLink.objects.select_related('merchant', 'creator'), token_hash=domain.digest(token))
    secret = request.session.get('checkout_browser_secret')
    if not secret:
        secret = secrets.token_urlsafe(32)
        request.session['checkout_browser_secret'] = secret
    checkout = domain.get_checkout_session(link, secret)
    return link, checkout, secret


@require_GET
def checkout(request, token):
    link, session, _ = _guest_context(request, token)
    if session.payment_id:
        return see_other(reverse('payments:checkout_result', args=[session.pk]))
    form = CheckoutForm(initial={'attempt_key': session.attempt_key, 'scenario': 'tok_demo_success'})
    return render(request, 'payments/checkout.html', {'link': link, 'merchant': link.merchant, 'form': form, 'checkout_token': token, 'unavailable': link.status != 'ACTIVE' or timezone.now() >= link.expires_at or link.merchant.status != 'ACTIVE'})


@require_POST
def checkout_confirm(request, token):
    link, session, secret = _guest_context(request, token)
    form = CheckoutForm(request.POST)
    if form.unknown_fields:
        return HttpResponse('Unsupported checkout fields.', status=400)
    if form.is_valid():
        from operations.protection import rate_limit
        rate_limit('checkout-link:' + str(link.pk), 10, 60)
        rate_limit('checkout-ip:' + request.META.get('REMOTE_ADDR', ''), 10, 60)
        try:
            domain.confirm_checkout(token, session.pk, secret, form.cleaned_data['attempt_key'], form.cleaned_data['scenario'], form.cleaned_data['customer_name'])
            return see_other(reverse('payments:checkout_result', args=[session.pk]))
        except (CheckoutSession.DoesNotExist, CheckoutLink.DoesNotExist):
            raise Http404
        except ValidationError as exc:
            _errors(form, exc)
    return render(request, 'payments/checkout.html', {'link': link, 'merchant': link.merchant, 'form': form, 'checkout_token': token},status=form_status(form))


@require_GET
def checkout_result(request, session_ref):
    secret = request.session.get('checkout_browser_secret')
    if not secret:
        raise Http404
    session = get_object_or_404(CheckoutSession.objects.select_related('payment', 'link__merchant'), pk=session_ref, browser_key_hash=domain.digest(secret))
    if not session.payment_id:
        raise Http404
    payment = session.payment
    return render(request, 'payments/receipt.html', {'checkout_session': session, 'payment': payment, 'merchant': session.link.merchant, 'pending': Operation.objects.filter(resource_id=payment.pk, status__in=domain.ACTIVE_OPERATIONS).exists()})


@login_required
@require_POST
def capture(request, merchant_id, payment_id):
    merchant = require_merchant(request.user, merchant_id, domain.FINANCE)
    payment = _payment(merchant, payment_id)
    form = CaptureForm(request.POST)
    return _submit_action(request, merchant, payment, form, domain.capture, 'Capture accepted for processing. Inspect the operation for its result.')


@login_required
@require_POST
def void(request, merchant_id, payment_id):
    merchant = require_merchant(request.user, merchant_id, domain.FINANCE)
    payment = _payment(merchant, payment_id)
    form = ActionForm(request.POST)
    return _submit_action(request, merchant, payment, form, domain.void, 'Void accepted for processing. This acknowledgment does not confirm cancellation.')


def _submit_action(request, merchant, payment, form, function, message):
    if form.unknown_fields:
        return HttpResponse('Unsupported action fields.', status=400)
    if form.is_valid():
        try:
            fields = dict(form.cleaned_data)
            if 'amount' in fields:
                fields['amount_minor'] = fields.pop('amount')
            op = function(request.user, merchant, payment.pk, **fields)
            messages.success(request, message)
            return see_other(f'/m/{merchant.pk}/operations/{op.pk}/')
        except ValidationError as exc:
            _errors(form, exc)
            if getattr(exc, 'code', None) in ('stale_version', 'key_conflict'):
                return render(request, 'payments/action_error.html', {'merchant': merchant, 'payment': payment, 'form': form}, status=409)
    return render(request, 'payments/action_error.html', {'merchant': merchant, 'payment': payment, 'form': form},status=form_status(form))


@login_required
@require_http_methods(['GET', 'POST'])
def refund_new(request, merchant_id, payment_id):
    merchant = require_merchant(request.user, merchant_id, domain.FINANCE)
    payment = _payment(merchant, payment_id)
    form = RefundForm(request.POST or None, initial={'expected_version': payment.version, 'action_key': secrets.token_hex(32)})
    if request.method == 'POST' and form.unknown_fields:
        return HttpResponse('Unsupported refund fields.', status=400)
    if request.method == 'POST' and form.is_valid():
        try:
            _, op = domain.refund(request.user, merchant, payment.pk, form.cleaned_data['amount'], form.cleaned_data['reason'], form.cleaned_data['expected_version'], form.cleaned_data['action_key'])
            messages.success(request, 'Refund accepted for processing. Its principal stays reserved until the outcome is known.')
            return see_other(f'/m/{merchant.pk}/operations/{op.pk}/')
        except ValidationError as exc:
            _errors(form, exc)
    return render(request, 'payments/refund_form.html', {'merchant': merchant, 'payment': payment, 'form': form, 'refundable_minor': payment.refundable_minor},status=form_status(form))
