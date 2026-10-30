from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404,redirect,render
from django.views.decorators.http import require_POST
from merchants.authz import require_merchant
from merchants.models import Membership
from operations.domain import action_key
from .models import Dispute
from .forms import ResponseForm
from .domain import submit_response


def list_view(request,merchant_id):
    merchant=require_merchant(request,merchant_id)
    disputes=Dispute.objects.filter(merchant=merchant).select_related('payment')
    state=request.GET.get('state','')
    if state in ['OPEN','RESPONDED','WON','LOST']:disputes=disputes.filter(state=state)
    return render(request,'disputes/list.html',{'merchant':merchant,'page':Paginator(disputes,50).get_page(request.GET.get('page')),'state':state})


def detail(request,merchant_id,dispute_id):
    merchant=require_merchant(request,merchant_id)
    dispute=get_object_or_404(Dispute.objects.select_related('payment'),pk=dispute_id,merchant=merchant)
    can_respond=Membership.objects.filter(merchant=merchant,user=request.user,active=True,role__in=['OWNER','FINANCE']).exists()
    form=ResponseForm(initial={'expected_version':dispute.version,'action_key':action_key(),'text':dispute.response_text})
    return render(request,'disputes/detail.html',{'merchant':merchant,'dispute':dispute,'form':form,'can_respond':can_respond,'responses':dispute.responses.order_by('-created_at')})


@require_POST
def respond(request,merchant_id,dispute_id):
    merchant=require_merchant(request,merchant_id,roles=['OWNER','FINANCE'])
    dispute=get_object_or_404(Dispute,pk=dispute_id,merchant=merchant)
    form=ResponseForm(request.POST);status=400
    if form.is_valid():
        try:
            op=submit_response(request.user,dispute,form.cleaned_data['text'],form.cleaned_data['action_key'],form.cleaned_data['expected_version'])
            return redirect(f'/m/{merchant.pk}/operations/{op.pk}/')
        except ValidationError as exc:
            form.add_error(None,exc)
            if getattr(exc,'code',None) in ['key_conflict','stale_version']:status=409
    return render(request,'disputes/detail.html',{'merchant':merchant,'dispute':dispute,'form':form,'can_respond':True,'responses':dispute.responses.order_by('-created_at')},status=status)
