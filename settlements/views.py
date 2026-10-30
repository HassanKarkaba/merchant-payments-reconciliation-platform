import uuid
import secrets
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from accounts.domain import require_step_up
from operations.actions import local_action
from merchants.domain import require_merchant
from payments.domain import FINANCE
from payments.forms import ActionForm
from payments.views import see_other,form_status
from . import domain
from .forms import ApprovalForm, BeneficiaryForm, PayoutForm
from .models import Beneficiary, Payout, SettlementBatch


def _approval_initial(request, version):
    return {'action_key': secrets.token_hex(32), 'proposal_version': version, 'step_up_reference': request.session.get('step_up_reference', '')}


@login_required
@require_http_methods(['GET', 'POST'])
def beneficiary_new(request, merchant_id):
    merchant = require_merchant(request.user, merchant_id)
    form = BeneficiaryForm(request.POST or None)
    if request.method == 'POST':
        require_merchant(request.user, merchant_id, FINANCE)
        if form.unknown_fields:
            return HttpResponse('Unsupported beneficiary fields.', status=400)
        if form.is_valid():
            try:
                domain.propose_beneficiary(request.user, merchant, form.cleaned_data['token'])
                messages.success(request, 'Beneficiary proposal recorded. A different owner must review and approve it.')
                return see_other(reverse('settlements:beneficiary_new', args=[merchant.pk]))
            except ValidationError as exc:
                form.add_error(None, exc)
    proposals = Beneficiary.objects.filter(merchant=merchant).select_related('maker', 'checker')
    return render(request, 'settlements/beneficiaries.html', {'merchant': merchant, 'form': form, 'proposals': [(proposal, ApprovalForm(initial=_approval_initial(request, proposal.version), auto_id='approval_' + str(proposal.pk) + '_%s')) for proposal in proposals], 'synthetic_token': domain.expected_beneficiary_token(merchant)})


@login_required
@require_POST
def beneficiary_approve(request, merchant_id, proposal_id):
    merchant = require_merchant(request.user, merchant_id, ('OWNER',))
    proposal = get_object_or_404(Beneficiary, pk=proposal_id, merchant=merchant)
    form = ApprovalForm(request.POST)
    if form.unknown_fields:
        return HttpResponse('Unsupported approval fields.', status=400)
    if form.is_valid():
        try:
            def perform(actor, locked):
                require_step_up(request, form.cleaned_data['step_up_reference'])
                return domain.approve_beneficiary(actor, locked, proposal_id, form.cleaned_data['proposal_version'], step_up_verified=True)
            result = local_action(request.user, merchant, 'BENEFICIARY_APPROVE', proposal_id, form.cleaned_data['action_key'], {'proposal_version': form.cleaned_data['proposal_version'], 'decision': 'APPROVE'}, perform, participants=(proposal.maker_id,), required_role='OWNER', load_result=lambda ref, actor, locked: get_object_or_404(Beneficiary, pk=ref, merchant=locked))
            if result._local_action_replayed:
                messages.info(request, 'The recorded beneficiary approval was found. Inspect its current status below.')
            else:
                messages.success(request, 'Synthetic beneficiary approved. Older unsent payout approvals require review.')
            return see_other(reverse('settlements:beneficiary_new', args=[merchant.pk]))
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, 'settlements/approval_error.html', {'merchant': merchant, 'form': form, 'back_url': reverse('settlements:beneficiary_new', args=[merchant.pk])}, status=form_status(form))


@login_required
@require_GET
def payout_list(request, merchant_id):
    merchant = require_merchant(request.user, merchant_id)
    items = Payout.objects.filter(merchant=merchant).select_related('beneficiary', 'operation')
    status = request.GET.get('status', '')
    if status:
        if status not in ('PROPOSED', 'APPROVED', 'DISPATCHING', 'PAID', 'DECLINED', 'CANCELLED', 'NEEDS_REAPPROVAL', 'UNKNOWN'):
            return HttpResponse('Invalid payout status.', status=400)
        items = items.filter(operation__status='UNKNOWN') if status == 'UNKNOWN' else items.filter(status=status)
    if request.GET.get('sort', '-created_at') not in ('-created_at', 'created_at'):
        return HttpResponse('Invalid payout sort.', status=400)
    items = items.order_by(request.GET.get('sort', '-created_at'), '-id')
    try:
        size = min(100, max(1, int(request.GET.get('page_size', 25))))
    except ValueError:
        return HttpResponse('Invalid page size.', status=400)
    return render(request, 'settlements/payouts.html', {'merchant': merchant, 'page_obj': Paginator(items, size).get_page(request.GET.get('page')), 'funds': domain.balances_and_availability(merchant), 'status_filter': status})


@login_required
@require_http_methods(['GET', 'POST'])
def payout_new(request, merchant_id):
    merchant = require_merchant(request.user, merchant_id, FINANCE)
    beneficiary = Beneficiary.objects.filter(merchant=merchant, status='ACTIVE').first()
    form = PayoutForm(request.POST or None, initial={'action_key': secrets.token_hex(32), 'expected_version': merchant.version, 'beneficiary_version': beneficiary.version if beneficiary else None})
    if request.method == 'POST' and form.unknown_fields:
        return HttpResponse('Unsupported payout fields.', status=400)
    if request.method == 'POST' and form.is_valid():
        try:
            payout = domain.propose_payout(request.user, merchant, form.cleaned_data['amount'], form.cleaned_data['beneficiary_version'], form.cleaned_data['reason'], form.cleaned_data['action_key'], form.cleaned_data['expected_version'])
            messages.success(request, 'Payout proposal recorded. It has not reserved funds or sent a transfer.')
            return see_other(reverse('settlements:payout_detail', args=[merchant.pk, payout.pk]))
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, 'settlements/payout_form.html', {'merchant': merchant, 'form': form, 'beneficiary': beneficiary, 'funds': domain.balances_and_availability(merchant)},status=form_status(form))


@login_required
@require_GET
def payout_detail(request, merchant_id, payout_id):
    merchant = require_merchant(request.user, merchant_id)
    payout = get_object_or_404(Payout.objects.select_related('maker', 'checker', 'beneficiary', 'operation'), pk=payout_id, merchant=merchant)
    return render(request, 'settlements/payout_detail.html', {'merchant': merchant, 'payout': payout, 'approval_form': ApprovalForm(auto_id='approval_%s', initial=_approval_initial(request, payout.version)), 'cancel_form': ActionForm(auto_id='cancel_%s', initial={'action_key': secrets.token_hex(32), 'expected_version': payout.version})})


@login_required
@require_POST
def payout_approve(request, merchant_id, payout_id):
    merchant = require_merchant(request.user, merchant_id, ('OWNER',))
    payout = get_object_or_404(Payout, pk=payout_id, merchant=merchant)
    form = ApprovalForm(request.POST)
    if form.unknown_fields:
        return HttpResponse('Unsupported approval fields.', status=400)
    if form.is_valid():
        try:
            def perform(actor, locked):
                require_step_up(request, form.cleaned_data['step_up_reference'])
                return domain.approve_payout(actor, locked, payout_id, form.cleaned_data['proposal_version'], step_up_verified=True)
            result = local_action(request.user, merchant, 'PAYOUT_APPROVE', payout_id, form.cleaned_data['action_key'], {'proposal_version': form.cleaned_data['proposal_version'], 'decision': 'APPROVE'}, perform, participants=(payout.maker_id,), required_role='OWNER', load_result=lambda ref, actor, locked: get_object_or_404(Payout, pk=ref, merchant=locked))
            if result._local_action_replayed:
                messages.info(request, 'The recorded payout approval was found. Inspect the current payout and operation status.')
            else:
                messages.success(request, 'Payout approved and principal reserved. The worker rechecks the approval before dispatch.')
            return see_other(reverse('settlements:payout_detail', args=[merchant.pk, payout_id]))
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, 'settlements/approval_error.html', {'merchant': merchant, 'form': form, 'back_url': reverse('settlements:payout_detail', args=[merchant.pk, payout_id])}, status=form_status(form))


@login_required
@require_POST
def payout_cancel(request, merchant_id, payout_id):
    merchant = require_merchant(request.user, merchant_id, FINANCE)
    payout = get_object_or_404(Payout, pk=payout_id, merchant=merchant)
    form = ActionForm(request.POST)
    if form.unknown_fields:
        return HttpResponse('Unsupported cancellation fields.', status=400)
    if form.is_valid():
        try:
            result = local_action(request.user, merchant, 'PAYOUT_CANCEL', payout_id, form.cleaned_data['action_key'], {'expected_version': form.cleaned_data['expected_version'], 'decision': 'CANCEL'},
                lambda actor, locked: domain.cancel_payout(actor, locked, payout_id, expected_version=form.cleaned_data['expected_version']),
                participants=(payout.maker_id, payout.checker_id), required_role=FINANCE,
                load_result=lambda ref, actor, locked: get_object_or_404(Payout, pk=ref, merchant=locked))
            if result._local_action_replayed:
                messages.info(request, 'The recorded cancellation was found. Inspect the current payout status.')
            else:
                messages.success(request, 'Unsent payout cancelled; its reservation was released.')
            return see_other(reverse('settlements:payout_detail', args=[merchant.pk, payout_id]))
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, 'settlements/approval_error.html', {'merchant': merchant, 'form': form, 'back_url': reverse('settlements:payout_detail', args=[merchant.pk, payout_id])},status=form_status(form))


@login_required
@require_GET
def batch_detail(request, merchant_id, batch_id):
    merchant = require_merchant(request.user, merchant_id)
    batch = get_object_or_404(SettlementBatch, pk=batch_id, merchant=merchant)
    return render(request, 'settlements/batch.html', {'merchant': merchant, 'batch': batch})
