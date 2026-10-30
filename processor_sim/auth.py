"""Authenticated, replay-protected service transport, separate from browser sessions."""
import hashlib
import hmac
import math
import secrets
import time
from datetime import timedelta
from functools import wraps
from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from .models import TransportNonce
from .domain import now

MAX_BODY = 1048576


def signing_bytes(method, path, timestamp, nonce, body):
    return '\n'.join((method.upper(), path, timestamp, nonce, hashlib.sha256(body).hexdigest())).encode('utf-8')


def signed_headers(method, path, body, secret, key_id='local-v1'):
    stamp = str(int(time.time()))
    nonce = secrets.token_hex(24)
    signature = hmac.new(secret.encode('utf-8'), signing_bytes(method, path, stamp, nonce, body), hashlib.sha256).hexdigest()
    return {'X-MPRP-Key-Id': key_id, 'X-MPRP-Timestamp': stamp, 'X-MPRP-Nonce': nonce, 'X-MPRP-Signature': signature}


def service_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            content_length = int(request.META.get('CONTENT_LENGTH') or 0)
        except ValueError:
            return JsonResponse({'code':'invalid_input','message':'Invalid body length'}, status=400)
        if content_length > MAX_BODY:
            return JsonResponse({'code': 'body_too_large', 'message': 'Service body exceeds its bound'}, status=413)
        body = request.body
        if len(body) > MAX_BODY:
            return JsonResponse({'code': 'body_too_large', 'message': 'Service body exceeds its bound'}, status=413)
        key_id = request.headers.get('X-MPRP-Key-Id', '')
        timestamp = request.headers.get('X-MPRP-Timestamp', '')
        nonce = request.headers.get('X-MPRP-Nonce', '')
        signature = request.headers.get('X-MPRP-Signature', '')
        try:
            stamp = float(timestamp)
        except ValueError:
            stamp = 0
        from config.security import verification_secret
        transport_secret=verification_secret(settings.SIMULATOR_VERIFICATION_KEYS,key_id)
        expected = hmac.new((transport_secret or '').encode('utf-8'), signing_bytes(request.method, request.get_full_path(), timestamp, nonce, body), hashlib.sha256).hexdigest()
        if key_id != settings.SIMULATOR_KEY_ID or not math.isfinite(stamp) or abs(time.time()-stamp) > 300 or not 32 <= len(nonce) <= 128 or not hmac.compare_digest(expected, signature):
            return JsonResponse({'code': 'invalid_service_auth', 'message': 'Service authentication failed'}, status=401)
        try:
            with transaction.atomic():
                at = now()
                TransportNonce.objects.filter(expires_at__lte=at).delete()
                TransportNonce.objects.create(nonce=nonce, expires_at=at+timedelta(minutes=10))
        except IntegrityError:
            return JsonResponse({'code': 'replayed_transport', 'message': 'Service nonce has already been used'}, status=401)
        return view(request, *args, **kwargs)
    return wrapper
