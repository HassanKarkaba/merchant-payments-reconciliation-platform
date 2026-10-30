from django.core.paginator import Paginator
from django.shortcuts import render
from merchants.authz import require_merchant
from .domain import balances,trial_balance
from .models import Journal


def balance_view(request,merchant_id):
    merchant=require_merchant(request,merchant_id)
    amounts=balances(merchant)
    from settlements.models import Payout
    from payments.models import Refund
    from django.db.models import Sum
    payouts=Payout.objects.filter(merchant=merchant,reserved=True)
    reserved_payout=payouts.aggregate(n=Sum('amount_minor'))['n'] or 0
    reserved_refund=Refund.objects.filter(payment__merchant=merchant,reserved=True).aggregate(n=Sum('amount_minor'))['n'] or 0
    reserved=reserved_payout+reserved_refund
    available=max(0,min(amounts['merchant_payable'],amounts['bank_control'])-reserved)
    try:
        from settlements.models import SettlementBatch
        batches=SettlementBatch.objects.filter(merchant=merchant).order_by('-created_at')[:20]
    except (ImportError,AttributeError):batches=[]
    return render(request,'accounting/balances.html',{'merchant':merchant,'amounts':amounts,'reserved':reserved,'available':available,'trial':trial_balance(merchant),'batches':batches})


def ledger(request,merchant_id):
    merchant=require_merchant(request,merchant_id)
    journals=Journal.objects.filter(merchant=merchant).select_related('source','period').prefetch_related('entries__account')
    kind=request.GET.get('kind','')
    if kind:journals=journals.filter(purpose=kind)
    page=Paginator(journals,50).get_page(request.GET.get('page'))
    return render(request,'accounting/ledger.html',{'merchant':merchant,'page':page,'kind':kind,'trial':trial_balance(merchant)})


def close_report(request,merchant_id,report_id):
    from django.shortcuts import get_object_or_404
    from .models import CloseReport
    merchant=require_merchant(request,merchant_id)
    report=get_object_or_404(CloseReport.objects.select_related('period','maker','checker'),pk=report_id,period__merchant=merchant)
    return render(request,'accounting/close_report.html',{'merchant':merchant,'report':report})
