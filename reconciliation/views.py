from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404,redirect,render
from django.views.decorators.http import require_POST
from django.utils import timezone
from accounts.domain import require_step_up
from merchants.authz import require_platform
from merchants.models import Merchant
from accounting.models import AccountingPeriod,EconomicSource
from operations.domain import action_key,enqueue
from operations.models import Job
from operations.audit import record
from django.db import transaction
from operations.provider import ProviderError
from .models import StatementImport,StatementLine,MatchGroup,ReconciliationException,AdjustmentProposal
from .forms import StatementUploadForm,VersionForm,ReasonForm,ExceptionActionForm,MatchForm,AdjustmentForm,SimulatorStatementForm,ApprovalDecisionForm
from . import domain

READ_ROLES=['RECONCILIATION','FINANCE_APPROVER','AUDITOR','OPERATIONS']

def _error(request,exc):
    for text in exc.messages:messages.error(request,text)


def _version_form(request,version=1):
    return VersionForm(initial={'expected_version':version,'action_key':action_key()})


def imports_new(request):
    require_platform(request,['RECONCILIATION'])
    form=StatementUploadForm(request.POST or None,request.FILES or None)
    if request.method=='POST' and form.is_valid():
        values=dict(form.cleaned_data);merchant=values.pop('merchant');upload=values.pop('file')
        try:
            statement=domain.stage_import(request.user,merchant,upload.read(),filename=upload.name,**values)
            return redirect('reconciliation:import_detail',import_id=statement.pk)
        except ValidationError as exc:form.add_error(None,exc)
    return render(request,'reconciliation/import_new.html',{'form':form,'simulator_form':SimulatorStatementForm(prefix='sim')})


def import_detail(request,import_id):
    require_platform(request,READ_ROLES)
    statement=get_object_or_404(StatementImport.objects.select_related('merchant','uploader'),pk=import_id)
    rows=Paginator(statement.parsed_rows,50).get_page(request.GET.get('page'))
    return render(request,'reconciliation/import_detail.html',{'statement':statement,'rows':rows,'form':_version_form(request,statement.version),'can_manage':request.user.platform_role=='RECONCILIATION'})


@require_POST
def import_action(request,import_id,action):
    require_platform(request,['RECONCILIATION'])
    statement=get_object_or_404(StatementImport,pk=import_id)
    form=VersionForm(request.POST)
    if not form.is_valid():messages.error(request,'Invalid or expired form. Refresh and try again.')
    else:
        try:
            if action=='confirm':
                statement=domain.confirm_import(request.user,statement,form.cleaned_data['expected_version'])
                if statement.status=='CONFIRMED':messages.success(request,'Statement evidence confirmed. No money was posted by this import.')
                else:messages.error(request,'The statement was quarantined. Review the manifest discrepancies.')
            elif action=='cancel':domain.cancel_import(request.user,statement);messages.success(request,'Unconfirmed import cancelled.')
            else:raise Http404
        except (ValidationError,ProviderError) as exc:
            if isinstance(exc,ValidationError):_error(request,exc)
            else:messages.error(request,"The simulator evidence service is unavailable. Your existing state is preserved; inspect and retry.")
    return redirect('reconciliation:import_detail',import_id=statement.pk)


def workspace(request):
    require_platform(request,READ_ROLES)
    merchants=Merchant.objects.order_by('name')
    merchant=merchants.filter(pk=request.GET.get('merchant')).first() if request.GET.get('merchant') else merchants.first()
    lines=StatementLine.objects.filter(merchant=merchant).order_by('-occurred_at') if merchant else StatementLine.objects.none()
    exceptions=ReconciliationException.objects.filter(merchant=merchant).exclude(state__in=['RESOLVED','ACCEPTED_NONFINANCIAL']) if merchant else ReconciliationException.objects.none()
    imports=StatementImport.objects.filter(merchant=merchant)[:10]
    groups=MatchGroup.objects.filter(merchant=merchant)[:20]
    sources=EconomicSource.objects.filter(merchant=merchant).order_by('-occurred_at')[:100]
    page=Paginator(lines,50).get_page(request.GET.get('page'))
    return render(request,'reconciliation/workspace.html',{'merchants':merchants,'merchant':merchant,'page':page,'exceptions':exceptions[:50],'imports':imports,'groups':groups,'sources':sources,'match_form':MatchForm(),'matching_jobs':Job.objects.filter(merchant=merchant,kind='reconciliation_match').order_by('-created_at')[:10],'can_manage':request.user.platform_role=='RECONCILIATION'})


@require_POST
def run(request):
    require_platform(request,['RECONCILIATION'])
    merchant=get_object_or_404(Merchant,pk=request.POST.get('merchant'))
    try:
        with transaction.atomic():
            domain.actor_role(request.user,{'RECONCILIATION'})
            Merchant.objects.select_for_update().get(pk=merchant.pk)
            job=Job.objects.filter(merchant=merchant,kind='reconciliation_match',status__in=['READY','CLAIMED']).first()
            if job is None:
                job=enqueue('reconciliation_match',merchant,{'actor_id':str(request.user.pk)})
                record(request.user,merchant,'reconciliation.admitted',job.pk,{'state':'READY'})
            messages.success(request,'Exact matching is durably queued. Inspect its progress below; refreshing this page does not repeat allocations.')
    except ValidationError as exc:_error(request,exc)
    return redirect('/operations/reconciliation/?merchant='+str(merchant.pk))


@require_POST
def match_new(request):
    require_platform(request,['RECONCILIATION'])
    form=MatchForm(request.POST)
    if form.is_valid():
        line=get_object_or_404(StatementLine,pk=form.cleaned_data['line_id'])
        try:
            domain.allocate_match(request.user,line.merchant,[{key:form.cleaned_data[key] for key in ('line_id','source_id','amount_minor')}],form.cleaned_data['reason'])
            messages.success(request,'Exact evidence allocation recorded.')
        except (ValidationError,ProviderError) as exc:
            if isinstance(exc,ValidationError):_error(request,exc)
            else:messages.error(request,"The simulator evidence service is unavailable. Your existing state is preserved; inspect and retry.")
        return redirect('/operations/reconciliation/?merchant='+str(line.merchant_id))
    messages.error(request,'Select valid evidence and a source, with exact signed USD cents.')
    return redirect('reconciliation:workspace')


@require_POST
def match_reverse(request,group_id):
    require_platform(request,['RECONCILIATION'])
    group=get_object_or_404(MatchGroup,pk=group_id)
    try:domain.reverse_match(request.user,group,request.POST.get('reason',''));messages.success(request,'Matching allocation reversed; its history remains available.')
    except (ValidationError,ProviderError) as exc:
            if isinstance(exc,ValidationError):_error(request,exc)
            else:messages.error(request,"The simulator evidence service is unavailable. Your existing state is preserved; inspect and retry.")
    return redirect('/operations/reconciliation/?merchant='+str(group.merchant_id))


def exception_detail(request,exception_id):
    require_platform(request,READ_ROLES)
    exception=get_object_or_404(ReconciliationException.objects.select_related('merchant','line','owner'),pk=exception_id)
    sources=EconomicSource.objects.filter(merchant=exception.merchant,event_type__in=['CORRECTION_CREDIT','CORRECTION_DEBIT'],recognized=False)
    return render(request,'reconciliation/exception.html',{'exception':exception,'sources':sources,'form':ExceptionActionForm(),'adjustment_form':AdjustmentForm(),'adjustments':exception.adjustments.all(),'can_manage':request.user.platform_role=='RECONCILIATION','can_approve':request.user.platform_role=='FINANCE_APPROVER','approval_action_key':action_key()})


@require_POST
def exception_action(request,exception_id,action):
    require_platform(request,['RECONCILIATION'])
    exception=get_object_or_404(ReconciliationException,pk=exception_id)
    try:
        if action=='adjustment':
            form=AdjustmentForm(request.POST)
            if not form.is_valid():raise ValidationError('Select a correction source and supply a rationale.')
            source=get_object_or_404(EconomicSource,pk=form.cleaned_data['source_id'],merchant=exception.merchant)
            domain.propose_adjustment(request.user,exception,source,form.cleaned_data['rationale'])
            messages.success(request,'Correction recognition proposed for independent finance approval.')
        else:
            form=ExceptionActionForm(request.POST)
            if not form.is_valid():raise ValidationError('Invalid exception action.')
            domain.update_exception(request.user,exception,action,owner=request.user,**form.cleaned_data)
            messages.success(request,'Exception review saved.')
    except (ValidationError,ProviderError) as exc:
            if isinstance(exc,ValidationError):_error(request,exc)
            else:messages.error(request,"The simulator evidence service is unavailable. Your existing state is preserved; inspect and retry.")
    return redirect('reconciliation:exception_detail',exception_id=exception.pk)


@require_POST
def adjustment_decide(request,proposal_id,decision):
    from operations.actions import local_action
    require_platform(request,['FINANCE_APPROVER'])
    if decision not in ['approve','reject']:raise Http404
    proposal=get_object_or_404(AdjustmentProposal.objects.select_related('merchant'),pk=proposal_id)
    form=ApprovalDecisionForm(request.POST)
    if not form.is_valid():
        messages.error(request,'Refresh the proposal to obtain its complete approval context.')
        return render(request,'operations/message.html',{'title':'Approval context invalid','message':'Refresh the proposal and submit its current version and immutable payload.'},status=400)
    payload={'expected_version':form.cleaned_data['expected_version'],'proposal_hash':form.cleaned_data['proposal_hash'],'decision':decision}
    def perform(actor,merchant):
        require_step_up(request)
        current=AdjustmentProposal.objects.select_for_update().get(pk=proposal.pk,merchant=merchant)
        if current.payload_hash!=payload['proposal_hash'] or current.state!='PROPOSED':raise ValidationError('This immutable proposal has changed or was already decided.',code='stale_version')
        return domain.decide_adjustment(actor,current,decision=='approve',payload['expected_version'])
    try:
        result=local_action(request.user,proposal.merchant,'ADJUSTMENT_DECISION',proposal.pk,form.cleaned_data['action_key'],payload,perform,participants=[proposal.maker_id],required_role='FINANCE_APPROVER',load_result=lambda identity,actor,merchant:get_object_or_404(AdjustmentProposal,pk=identity,merchant=merchant))
        messages.success(request,'Previously recorded correction decision retrieved.' if getattr(result,'_local_action_replayed',False) else 'Correction recognition decision recorded.')
    except ValidationError as error:
        _error(request,error)
        return render(request,'operations/message.html',{'title':'Approval unavailable' if getattr(error,'code',None)=='capability_unavailable' else 'Proposal changed','message':'Inspect system status and refresh the proposal before deciding. No additional financial recognition was posted.'},status=503 if getattr(error,'code',None)=='capability_unavailable' else 409)
    return redirect('reconciliation:exception_detail',exception_id=proposal.exception_id)


def periods(request):
    require_platform(request,READ_ROLES)
    merchant_id=request.GET.get('merchant')
    merchants=Merchant.objects.order_by('name')
    merchant=merchants.filter(pk=merchant_id).first() if merchant_id else merchants.first()
    records=AccountingPeriod.objects.filter(merchant=merchant).prefetch_related('close_reports').order_by('-month') if merchant else AccountingPeriod.objects.none()
    return render(request,'reconciliation/periods.html',{'merchants':merchants,'merchant':merchant,'periods':records,'can_propose':request.user.platform_role=='RECONCILIATION','can_approve':request.user.platform_role=='FINANCE_APPROVER','action_key':action_key()})


@require_POST
def period_action(request,period_id,action):
    role='FINANCE_APPROVER' if action.startswith('approve') else 'RECONCILIATION'
    require_platform(request,[role])
    period=get_object_or_404(AccountingPeriod.objects.select_related('merchant'),pk=period_id)
    try:
        if action in ['approve-close','approve-reopen']:
            from operations.actions import local_action
            form=ApprovalDecisionForm(request.POST)
            if not form.is_valid():return render(request,'operations/message.html',{'title':'Approval context invalid','message':'Refresh the accounting period before approving its immutable proposal.'},status=400)
            payload={'expected_version':form.cleaned_data['expected_version'],'proposal_hash':form.cleaned_data['proposal_hash'],'decision':action}
            kind='PERIOD_CLOSE_APPROVAL' if action=='approve-close' else 'PERIOD_REOPEN_APPROVAL'
            # Immutable provider evidence is fetched outside the local action's
            # User/Merchant locks. A retained result needs no provider service.
            from operations.models import ApprovalReceipt
            retained=ApprovalReceipt.objects.filter(actor=request.user,merchant=period.merchant,kind=kind,resource_id=period.pk,action_key=form.cleaned_data['action_key']).exists()
            manifests=domain.month_manifests(period.merchant,period.month) if action=='approve-close' and not retained else None
            def perform(actor,merchant):
                require_step_up(request)
                current=AccountingPeriod.objects.select_for_update().get(pk=period.pk,merchant=merchant)
                if current.proposal_hash!=payload['proposal_hash']:raise ValidationError('The frozen accounting proposal changed.',code='stale_version')
                if action=='approve-close':return domain.approve_close(actor,current,payload['expected_version'],manifests=manifests)
                return domain.approve_reopen(actor,current,payload['expected_version'])
            result=local_action(request.user,period.merchant,kind,period.pk,form.cleaned_data['action_key'],payload,perform,participants=[period.proposed_by_id] if period.proposed_by_id else [],required_role='FINANCE_APPROVER',load_result=lambda identity,actor,merchant:get_object_or_404(AccountingPeriod,pk=identity,merchant=merchant))
            messages.success(request,'Previously recorded accounting decision retrieved.' if getattr(result,'_local_action_replayed',False) else 'Accounting period transition recorded.')
        elif action=='propose-close':domain.propose_close(request.user,period);messages.success(request,'Close proposal recorded.')
        elif action=='propose-reopen':domain.propose_reopen(request.user,period,request.POST.get('reason',''));messages.success(request,'Reopen proposal recorded.')
        else:raise Http404
    except (ValidationError,ValueError) as error:
        if isinstance(error,ValidationError):_error(request,error)
        else:messages.error(request,'Invalid period version.')
        if action.startswith('approve'):return render(request,'operations/message.html',{'title':'Approval unavailable' if getattr(error,'code',None)=='capability_unavailable' else 'Period changed','message':'Inspect system status and refresh the accounting period before approving. No additional close or reopen was committed.'},status=503 if getattr(error,'code',None)=='capability_unavailable' else 409)
    except ProviderError:messages.error(request,'Authenticated simulator evidence is unavailable. The accounting period remains inspectable; retry when evidence is available.')
    return redirect('/operations/periods/?merchant='+str(period.merchant_id))


@require_POST
def period_new(request):
    require_platform(request,['RECONCILIATION'])
    merchant=get_object_or_404(Merchant,pk=request.POST.get('merchant'))
    try:
        from datetime import datetime
        month=datetime.strptime(request.POST.get('month',''),'%Y-%m').date().replace(day=1)
        domain.create_period(request.user,merchant,month)
        messages.success(request,'UTC accounting month is available for control checks.')
    except (ValidationError,ValueError) as exc:
        if isinstance(exc,ValidationError):_error(request,exc)
        else:messages.error(request,'Choose a valid UTC month in YYYY-MM format.')
    return redirect('/operations/periods/?merchant='+str(merchant.pk))


@require_POST
def from_simulator(request):
    from .forms import SimulatorStatementForm
    require_platform(request,['RECONCILIATION'])
    form=SimulatorStatementForm(request.POST,prefix='sim')
    if form.is_valid():
        try:
            statement=domain.stage_simulator_statement(request.user,**form.cleaned_data)
            return redirect('reconciliation:import_detail',import_id=statement.pk)
        except ValidationError as exc:form.add_error(None,exc)
        except ProviderError:form.add_error(None,'Simulator evidence is unavailable. Ensure activity is settled, choose a completed interval and try again.')
    return render(request,'reconciliation/import_new.html',{'form':StatementUploadForm(),'simulator_form':form},status=400)
