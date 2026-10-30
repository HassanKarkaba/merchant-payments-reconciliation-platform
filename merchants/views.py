from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from accounts.models import AuthToken
from django.shortcuts import render,redirect,get_object_or_404
from django.views.decorators.http import require_POST
from accounts.domain import issue_token
from accounts.models import User
from operations.audit import record
from .domain import require_merchant,locked_authority
from .models import Merchant,Membership
from .forms import MerchantForm,InviteForm
@login_required
def overview(request,merchant_id):
    merchant=require_merchant(request.user,merchant_id)
    from settlements.domain import balances_and_availability
    from payments.models import Payment
    from disputes.models import Dispute
    return render(request,'merchants/overview.html',{'merchant':merchant,'balances':balances_and_availability(merchant),'payments':Payment.objects.filter(merchant=merchant).order_by('-created_at')[:6],'dispute_count':Dispute.objects.filter(merchant=merchant,state__in=['OPEN','RESPONDED']).count()})
@login_required
def settings(request,merchant_id):
    merchant=require_merchant(request.user,merchant_id,('OWNER',))
    form=MerchantForm(request.POST or None,initial={'name':merchant.name,'timezone':merchant.timezone,'expected_version':merchant.version})
    if request.method=='POST' and form.is_valid():
        with locked_authority(request.user,merchant,('OWNER',),active=False) as locked:
            if locked.version!=form.cleaned_data['expected_version']:return render(request,'accounts/form.html',{'title':'Settings changed','form':form},status=409)
            locked.name=form.cleaned_data['name'];locked.timezone=form.cleaned_data['timezone'];locked.version+=1;locked.save();record(request.user,locked,'merchant.settings',locked.pk)
        messages.success(request,'Merchant settings saved.');return redirect('merchants:settings',merchant_id=merchant.pk)
    return render(request,'accounts/form.html',{'merchant':merchant,'title':'Business settings','form':form})
@login_required
def team(request,merchant_id):
    merchant=require_merchant(request.user,merchant_id,('OWNER',))
    return render(request,'merchants/team.html',{'merchant':merchant,'members':merchant.memberships.select_related('user').order_by('created_at'),'form':InviteForm()})
@login_required
@require_POST
def invite(request,merchant_id):
    merchant=require_merchant(request.user,merchant_id,('OWNER',));form=InviteForm(request.POST)
    if form.is_valid():
        with locked_authority(request.user,merchant,('OWNER',),active=False) as locked:
            if locked.status in ['CLOSING','CLOSED']:raise PermissionDenied('Closing and closed merchants cannot invite members.')
            if locked.memberships.filter(active=True,user__email=form.cleaned_data['email']).exists():raise PermissionDenied('This account is already an active member. Use the protected team change workflow.')
            if locked.memberships.filter(active=True).count()>=100:raise PermissionDenied('The membership limit has been reached.')
            issue_token('invite',form.cleaned_data['email'],user=request.user,merchant=locked,role=form.cleaned_data['role']);record(request.user,locked,'team.invited',locked.pk,{'role':form.cleaned_data['role']})
        messages.success(request,'Invitation queued for the local mailbox.');return redirect('merchants:team',merchant_id=merchant.pk)
    return render(request,'merchants/team.html',{'merchant':merchant,'members':merchant.memberships.select_related('user'),'form':form})
@login_required
@require_POST
def change_member(request,merchant_id,membership_id):
    merchant=require_merchant(request.user,merchant_id,('OWNER',));target=get_object_or_404(Membership,pk=membership_id,merchant=merchant)
    with transaction.atomic():
        current_users = {user.pk:user for user in User.objects.select_for_update().filter(pk__in=sorted([request.user.pk,target.user_id])).order_by('pk')}
        current_actor = current_users[request.user.pk]
        if not current_actor.is_active or not current_actor.verified or not current_actor.totp_confirmed:
            raise PermissionDenied('Current authenticated account authority is required.')
        locked=Merchant.objects.select_for_update().get(pk=merchant.pk)
        require_merchant(current_actor,merchant_id,('OWNER',))
        target=Membership.objects.select_for_update().get(pk=target.pk)
        if str(target.version)!=request.POST.get('expected_version'):return render(request,'operations/message.html',{'title':'Team changed','message':'Reload the team page before editing.'},status=409)
        role=request.POST.get('role',target.role);active=request.POST.get('active','1')=='1'
        if role not in ['OWNER','FINANCE','ANALYST']:raise PermissionDenied
        if target.role=='OWNER' and (not active or role!='OWNER') and locked.memberships.filter(active=True,role='OWNER').count()<=1:raise PermissionDenied('Replace the last active owner first.')
        target.role=role;target.active=active;target.version+=1;target.save()
        # Reissuing an invitation is required after either recipient revocation
        # or a change to its issuing owner's authority. Old tokens never revive.
        AuthToken.objects.filter(kind='invite',merchant=locked,used_at=None).filter(Q(email=current_users[target.user_id].email)|Q(user_id=target.user_id)).update(used_at=timezone.now())
        record(request.user,locked,'team.changed',target.pk,{'role':role,'active':active})
    return redirect('merchants:team',merchant_id=merchant.pk)
@login_required
@require_POST
def request_close(request,merchant_id):
    merchant=require_merchant(request.user,merchant_id,('OWNER',))
    with locked_authority(request.user,merchant,('OWNER',),active=False) as locked:
        if locked.status not in ['ACTIVE','SUSPENDED']:raise PermissionDenied('This merchant cannot request closure.')
        locked.status='CLOSING';locked.status_reason='Closure requested by owner';locked.save();record(request.user,locked,'merchant.closure_requested',locked.pk)
    messages.success(request,'Closure requested. Operations must review all financial obligations.');return redirect('merchants:overview',merchant_id=merchant.pk)
