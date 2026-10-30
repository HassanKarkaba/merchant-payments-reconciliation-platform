import json
import time
from functools import wraps
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from . import domain
from .auth import service_required
from .models import Control, ProviderAccount, ProviderPayment, StatementManifest, RetiredNamespace


def _json(request):
    if request.content_type != 'application/json':
        raise domain.ProviderError('Expected application/json')
    try:
        data = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise domain.ProviderError('Malformed JSON')
    if not isinstance(data, dict):
        raise domain.ProviderError('Expected a JSON object')
    return data


def api(method):
    def decorate(view):
        @csrf_exempt
        @service_required
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method != method:
                return JsonResponse({'code': 'method_not_allowed', 'message': f'Use {method}'}, status=405)
            try:
                result = view(request, *args, **kwargs)
                return result if isinstance(result, HttpResponse) else JsonResponse(result)
            except domain.ProviderError as error:
                return JsonResponse({'code': error.code, 'message': str(error)}, status=error.status)
            except (ValueError, TypeError, KeyError):
                return JsonResponse({'code': 'invalid_input', 'message': 'Invalid bounded service input'}, status=400)
        return wrapper
    return decorate


@api('GET')
def capabilities(request):
    return domain.capabilities()


@api('POST')
def handshake(request):
    return domain.handshake(_json(request))


@api('POST')
def mutation(request, kind):
    data = _json(request)
    result = domain.mutate(kind, data)
    # Commit has already completed before simulating transport ambiguity.
    scenario = data.get('payload', {}).get('scenario')
    if scenario == 'tok_demo_timeout_after_commit' and result['state'] == 'SUCCEEDED':
        time.sleep(6)
    if data.get('payload', {}).get('scenario') == 'demo_malformed' and result['state'] == 'SUCCEEDED':
        return HttpResponse('{invalid', content_type='application/json')
    return result


@api('POST')
def resolve(request):
    return domain.resolve_key(_json(request))


@api('GET')
def operation(request, operation_key):
    return domain.operation_lookup(operation_key, request.GET.get('provider_account_id'))


@api('GET')
def payment(request, object_id):
    snapshot = domain.object_snapshot(object_id)
    control = Control.objects.get(id=1)
    return {'schema_version': 1, 'policy_version': 'mprp_v1', 'environment_id': control.environment_id,
            'simulator_instance_id': str(control.instance_id), 'snapshot': snapshot,
            'source_high_water': control.next_source_sequence-1}


@api('GET')
def account(request, account_id):
    try:
        record = ProviderAccount.objects.get(id=domain.valid_uuid(account_id), retired=False)
    except ProviderAccount.DoesNotExist:
        raise domain.ProviderError('Provider account not found', 'not_found', 404)
    return domain.account_snapshot(record)


@api('GET')
def history(request, object_id):
    after = domain.integer(int(request.GET.get('after', '0')), 0, 2**63-1)
    snap = request.GET.get('snapshot')
    return domain.history(object_id, after, int(snap) if snap is not None else None)


@api('GET')
def events(request):
    return domain.event_feed(domain.integer(int(request.GET.get('after', '0')), 0, 2**63-1))


@api('POST')
def acknowledge(request):
    return domain.acknowledge(_json(request))


@api('GET')
def statement(request):
    expected = {'provider_account_id', 'kind', 'period_start', 'period_end'}
    if set(request.GET) != expected:
        raise domain.ProviderError('Statement requires exact account, kind and period fields')
    result=domain.statement(**{key: request.GET[key] for key in expected if key != 'provider_account_id'}, account_id=request.GET['provider_account_id']).manifest
    if result['row_count']>10000:
        # The bounded importer selects smaller half-open windows. Counts and
        # controls are available without transferring an oversized row payload.
        return {**result,'rows':[],'row_hashes':[],'requires_split':True}
    return result


@api('GET')
def manifest(request, manifest_id, csv_output=False):
    try:
        record = StatementManifest.objects.get(id=domain.valid_uuid(manifest_id))
    except StatementManifest.DoesNotExist:
        raise domain.ProviderError('Immutable statement manifest not found', 'not_found', 404)
    if csv_output:
        response = HttpResponse(bytes(record.csv_content), content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{record.statement_id}.csv"'
        return response
    return record.manifest


def live(request):
    return JsonResponse({'live': True, 'sandbox': True})


@api('GET')
def inventory(request):
    after = domain.integer(int(request.GET.get('after', '0')), 0, 10000000)
    return domain.operation_inventory(after, request.GET.get('snapshot'))


@api('GET')
def source_inventory(request):
    after = domain.integer(int(request.GET.get('after', '0')), 0, 2**63-1)
    snapshot = request.GET.get('snapshot')
    return domain.source_inventory(after, int(snapshot) if snapshot is not None else None)


@api('POST')
def retire(request,account_id):
    data=_json(request)
    if str(data.get('provider_account_id'))!=str(account_id):
        raise domain.ProviderError('Retirement path and envelope differ')
    return domain.mutate('accounts/retire',data)


@api('GET')
def retirement(request,account_id):
    receipt=RetiredNamespace.objects.filter(account_id=account_id).first()
    if not receipt:
        raise domain.ProviderError('No retired namespace receipt', 'not_found',404)
    remaining=sum(max(0,count-receipt.purged_counts.get(kind,0)) for kind,count in receipt.counts.items())
    return {'retired':True,'retirement_id':str(receipt.retirement_id),'provider_account_id':str(receipt.account_id),
            'counts':receipt.counts,'purged_counts':receipt.purged_counts,'remaining_rows':0 if receipt.completed_at else remaining+1,
            'purge_complete':bool(receipt.completed_at),'phase':receipt.phase}


@api('POST')
def restoration_release(request):
    return domain.release_restoration(_json(request))
