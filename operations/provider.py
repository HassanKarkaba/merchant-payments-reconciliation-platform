"""Bounded authenticated HTTP to the fixed local synthetic provider origin."""
import hashlib
import hmac
import json
import secrets
import time
import http.client
import ipaddress
import re
import socket
import os
import select
import signal
import threading
from urllib.parse import urlencode,urlsplit
from django.conf import settings
from .audit import canonical


class ProviderError(Exception):
    pass


class ProviderUncertain(ProviderError):
    pass


def _addresses(host,port,deadline):
    """Resolve in a bounded minimal POSIX child without interpreter startup cost."""
    try:
        literal=ipaddress.ip_address(host)
        family=socket.AF_INET6 if literal.version==6 else socket.AF_INET
        return [(family,socket.SOCK_STREAM,0,'',(host,port))]
    except ValueError:
        pass
    if deadline<=time.monotonic():raise ProviderUncertain('Provider connect budget expired')
    read_fd,write_fd=os.pipe()
    try:pid=os.fork()
    except BaseException:
        os.close(read_fd);os.close(write_fd)
        raise
    if pid==0:
        try:
            # The child performs no Django work and never retains inherited DB
            # connections. os._exit avoids running parent cleanup handlers.
            try:descriptors=[int(value) for value in os.listdir('/proc/self/fd')]
            except OSError:descriptors=range(3,int(os.sysconf('SC_OPEN_MAX')))
            for descriptor in descriptors:
                if descriptor>2 and descriptor!=write_fd:
                    try:os.close(descriptor)
                    except OSError:pass
            raw=json.dumps(socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)[:16]).encode()
            os.write(write_fd,raw)
        except BaseException:
            pass
        finally:os._exit(0)
    os.close(write_fd)
    pid_fd=None
    if hasattr(os,'pidfd_open'):
        try:pid_fd=os.pidfd_open(pid)
        except OSError:pass
    raw=b''
    try:
        while True:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise ProviderUncertain('Provider DNS resolution exceeded its connect budget')
            readable,_,_=select.select([read_fd],[],[],remaining)
            if not readable:raise ProviderUncertain('Provider DNS resolution exceeded its connect budget')
            part=os.read(read_fd,16385-len(raw))
            if not part:break
            raw+=part
            if len(raw)>16384:raise ProviderUncertain('Provider DNS response exceeds its bound')
        try:
            addresses=json.loads(raw)
            if not isinstance(addresses,list) or not addresses:raise ValueError
            return addresses
        except (ValueError,TypeError):
            raise ProviderUncertain('Provider DNS resolution failed') from None
    finally:
        os.close(read_fd)
        try:
            child,status=os.waitpid(pid,os.WNOHANG)
            if child==0:
                if pid_fd is not None and hasattr(signal,'pidfd_send_signal'):
                    signal.pidfd_send_signal(pid_fd,signal.SIGKILL)
                else:os.kill(pid,signal.SIGKILL)
                os.waitpid(pid,0)
        except (ChildProcessError,ProcessLookupError):
            pass
        finally:
            if pid_fd is not None:os.close(pid_fd)


def _http_json(method,path,body,headers,limit,timeout):
    started=time.monotonic();deadline=started+timeout;connect_deadline=min(deadline,started+2)
    origin=urlsplit(settings.SIMULATOR_ORIGIN)
    if origin.scheme!='http' or not origin.hostname or origin.username or origin.password or origin.path not in {'','/'} or origin.query or origin.fragment:
        raise ProviderError('Unsupported fixed provider origin')
    port=origin.port or 80
    sock=None;connection=None;timer=None
    expired=threading.Event()
    try:
        addresses=_addresses(origin.hostname,port,connect_deadline)
        for family,kind,protocol,_,address in addresses:
            remaining=connect_deadline-time.monotonic()
            if remaining<=0:break
            candidate=socket.socket(int(family),int(kind),int(protocol))
            candidate.settimeout(remaining)
            try:
                candidate.connect(tuple(address))
                sock=candidate
                break
            except OSError:
                candidate.close()
        if sock is None:raise ProviderUncertain('Provider connection exceeded its connect budget')
        remaining=deadline-time.monotonic()
        if remaining<=0:raise ProviderUncertain('Provider attempt deadline expired')
        sock.settimeout(remaining)
        owned_socket=sock
        def expire():
            expired.set()
            try:owned_socket.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            try:owned_socket.close()
            except OSError:pass
        timer=threading.Timer(remaining,expire)
        timer.daemon=True
        timer.start()
        connection=http.client.HTTPConnection(origin.hostname,port,timeout=remaining)
        # DNS and connect have already completed under their shared 2s budget.
        connection.sock=sock
        connection.request(method,path,body=body if method!='GET' else None,headers=headers)
        response=connection.getresponse()
        raw=response.read(limit+1)
        if expired.is_set() or time.monotonic()>=deadline or len(raw)>limit:
            raise ProviderUncertain('Provider response exceeded its size or total time budget')
        try:value=json.loads(raw)
        except (ValueError,UnicodeDecodeError):raise ProviderUncertain('Provider returned malformed JSON') from None
        if not isinstance(value,dict):raise ProviderUncertain('Provider response is not an object')
        if time.monotonic()>=deadline:raise ProviderUncertain('Provider attempt deadline expired')
        if not 200<=response.status<300:
            code=value.get('code','')
            if not isinstance(code,str) or not re.fullmatch('[a-z_]{1,64}',code):
                raise ProviderUncertain('Provider returned an invalid error schema')
            raise ProviderError('Provider rejected request: '+code)
        return value
    except (OSError,ValueError,TimeoutError,http.client.HTTPException):
        raise ProviderUncertain('Provider outcome requires inspection') from None
    finally:
        if timer:timer.cancel()
        try:
            if connection:connection.close()
        except (OSError,http.client.HTTPException):
            pass
        finally:
            if sock:
                try:sock.close()
                except OSError:pass
            if timer and timer.is_alive():timer.join()


def request(method, path, payload=None, *, timeout=5, hydrate=True):
    if not path.startswith('/v1/') or '://' in path or '\n' in path or '\r' in path or not 0 < timeout <= 5:
        raise ProviderError('Invalid bounded provider request')
    body = canonical(payload).encode() if payload is not None else b''
    stamp = str(int(time.time()))
    nonce = secrets.token_hex(24)
    material = '\n'.join([method.upper(), path, stamp, nonce, hashlib.sha256(body).hexdigest()])
    signature = hmac.new(settings.SIMULATOR_API_KEY.encode(), material.encode(), hashlib.sha256).hexdigest()
    headers = {'Content-Type': 'application/json', 'X-MPRP-Key-Id': settings.SIMULATOR_KEY_ID, 'X-MPRP-Timestamp': stamp,
               'X-MPRP-Nonce': nonce, 'X-MPRP-Signature': signature}
    # A 10000-row manifest can exceed 1 MiB; this separate response bound remains finite.
    limit = 20 * 1024 * 1024 if path.startswith(('/v1/statements/', '/v1/history/', '/v1/inventory/')) else 1024 * 1024
    value=_http_json(method,path,body,headers,limit,timeout)
    if hydrate and isinstance(value.get('snapshot'), dict) and value['snapshot'].get('history_complete') is False:
        snapshot = value['snapshot']
        identity = snapshot.get('object_id')
        high = snapshot.get('source_high_water')
        if not identity or type(high) is not int:
            raise ProviderUncertain('Provider omitted stable history identity')
        sources = []
        after = 0
        while True:
            page = request('GET', f'/v1/history/{identity}/?' + urlencode({'after': after, 'snapshot': high}), timeout=timeout, hydrate=False)
            if page.get('snapshot') != high or not isinstance(page.get('sources'), list) or len(page['sources']) > 500:
                raise ProviderUncertain('Provider history snapshot changed')
            for source in page['sources']:
                sequence = source.get('sequence')
                if type(sequence) is not int or sequence <= after or sequence > high:
                    raise ProviderUncertain('Provider history sequence is inconsistent')
                sources.append(source)
                after = sequence
            if page.get('complete') is True:
                break
            next_after = page.get('next_after')
            if next_after != after or not page['sources']:
                raise ProviderUncertain('Provider history made no forward progress')
        if len(sources) != snapshot.get('source_count'):
            raise ProviderUncertain('Provider history omitted required sources')
        snapshot['sources'] = sources
        snapshot['history_complete'] = True
    return value


def envelope(merchant, operation_key, payload):
    from .models import Control
    control = Control.objects.get(pk=1)
    from .run_identity import bind_process_identity,RunIdentityError
    try:identity=bind_process_identity(control)
    except RunIdentityError:
        raise ProviderError('This process belongs to an unavailable or replaced run; no provider request was sent') from None
    return {'schema_version': 1, 'policy_version': 'mprp_v1', 'environment_id': identity.environment_id,
            'main_instance_id': identity.main_instance_id, 'dispatch_epoch': identity.dispatch_epoch,
            'operation_key': str(operation_key), 'provider_account_id': str(merchant.provider_account_id),
            'payload_hash': hashlib.sha256(canonical(payload).encode()).hexdigest(), 'payload': payload}


def validate_result(result,operation=None,*,provider_account_id=None):
    """Reject foreign or unsupported service bodies before any local financial mutation."""
    from .models import Control
    control=Control.objects.get(pk=1)
    required={'schema_version','policy_version','environment_id','simulator_instance_id','snapshot','source_high_water'}
    if operation is not None:required|={'operation_key','state','reason','payload_hash'}
    if not isinstance(result,dict) or set(result)!=required:
        raise ProviderUncertain('Provider result fields do not match schema version1')
    if result['schema_version']!=1 or result['policy_version']!='mprp_v1' or str(result['environment_id'])!=str(control.environment_id) or str(result['simulator_instance_id'])!=control.simulator_instance_id:
        raise ProviderUncertain('Provider result environment or database instance differs')
    if type(result['source_high_water']) is not int or result['source_high_water']<0:
        raise ProviderUncertain('Provider source cutoff is invalid')
    if operation is not None:
        if str(result['operation_key'])!=str(operation.provider_key) or result['state'] not in {'SUCCEEDED','FAILED','NO_EFFECT'}:
            raise ProviderUncertain('Provider operation identity or outcome differs')
        if result['state']!='NO_EFFECT' and result['payload_hash']!=operation.fingerprint:
            raise ProviderUncertain('Provider canonical request fingerprint differs')
        if not isinstance(result['reason'],str) or len(result['reason'])>2000:
            raise ProviderUncertain('Provider reason is outside its bound')
        provider_account_id=operation.merchant.provider_account_id
        if result['state']!='SUCCEEDED':
            if result['snapshot'] is not None:raise ProviderUncertain('No-effect result unexpectedly contains an economic snapshot')
            return result
    snapshot=result['snapshot']
    if not isinstance(snapshot,dict):raise ProviderUncertain('Provider omitted its authoritative snapshot')
    object_type=snapshot.get('object_type')
    fields={
        'payment':{'object_type','object_id','payment_id','provider_id','provider_account_id','currency','authorized_minor','captured_minor','refunded_minor','fee_minor','state','version','provider_version','authorization_expires_at','dispute_id','dispute_state','sources','refund_id'},
        'dispute':{'object_type','object_id','dispute_id','payment_id','provider_account_id','currency','principal_minor','amount_minor','state','version','provider_version','opened_at','deadline','response_version','response_stored','decision_at','sources'},
        'payout':{'object_type','object_id','payout_id','provider_account_id','currency','amount_minor','beneficiary_token','state','version','provider_version','reference','sources'},
        'settlement':{'object_type','object_id','batch_id','provider_account_id','currency','net_minor','amount_minor','cutoff','state','version','provider_version','occurred_at','created_at','transfer_id','transfer_reference','sources','source_count','history_complete','source_high_water'},
        'correction':{'object_type','object_id','provider_account_id','currency','amount_minor','state','version','sources'},
    }
    if object_type not in fields or set(snapshot)-fields[object_type]:
        raise ProviderUncertain('Unsupported provider snapshot schema')
    if snapshot.get('currency')!='USD' or str(snapshot.get('provider_account_id'))!=str(provider_account_id or snapshot.get('provider_account_id')):
        raise ProviderUncertain('Provider snapshot account or currency differs')
    import uuid
    try:uuid.UUID(str(snapshot['object_id']))
    except (KeyError,ValueError,TypeError):raise ProviderUncertain('Provider object identity is invalid') from None
    if type(snapshot.get('version')) is not int or snapshot['version']<1:
        raise ProviderUncertain('Provider object version is invalid')
    sources=snapshot.get('sources')
    if not isinstance(sources,list):raise ProviderUncertain('Provider source history is missing')
    source_fields={'id','source_id','sequence','event_type','kind','object_id','payment_id','provider_account_id','currency','amount_minor','occurred_at','reference','dependencies','batch_id','dispute_id','principal_minor','opened_at','deadline','state','provider_version','version','response_version'}
    source_required={'source_id','sequence','event_type','object_id','provider_account_id','currency','amount_minor','occurred_at','reference','dependencies'}
    previous=0
    from django.utils.dateparse import parse_datetime
    for source in sources:
        if not isinstance(source,dict) or not source_required<=set(source) or set(source)-source_fields:
            raise ProviderUncertain('Unsupported economic source schema')
        amount=source['amount_minor'];sequence=source['sequence']
        if type(amount) is not int or not -(2**63)<amount<2**63 or type(sequence) is not int or sequence<=previous or sequence>result['source_high_water']:
            raise ProviderUncertain('Provider source amount or sequence is invalid')
        previous=sequence
        if source['currency']!='USD' or str(source['provider_account_id'])!=str(snapshot['provider_account_id']) or source.get('id',source['source_id'])!=source['source_id'] or source.get('kind',source['event_type'])!=source['event_type']:
            raise ProviderUncertain('Provider economic source scope or aliases differ')
        timestamp=parse_datetime(source['occurred_at']) if isinstance(source['occurred_at'],str) else None
        if timestamp is None or timestamp.tzinfo is None or not isinstance(source['dependencies'],list):
            raise ProviderUncertain('Provider source provenance is invalid')
    return result
