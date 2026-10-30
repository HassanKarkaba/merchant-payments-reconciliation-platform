"""Independent provider scheduler and durable callback delivery."""
import json
import logging
from datetime import timedelta
from urllib.parse import urlparse
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from django.conf import settings
from django.db import transaction, close_old_connections, connections, OperationalError, InterfaceError
from urllib.error import URLError
from .auth import signed_headers
from .domain import daily_settlements, progress_disputes, expire_authorizations, now, ProviderError
from .models import ProviderEvent

logger = logging.getLogger(__name__)
BACKOFF = (1, 5, 30, 120, 600)
DEPENDENCY_BACKOFF = (1, 2, 5, 10, 30)
DEPENDENCY_ERRORS = (OperationalError, InterfaceError, ProviderError, URLError, TimeoutError, ConnectionError)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def deliver_one():
    origin = urlparse(settings.CALLBACK_ORIGIN)
    if origin.scheme != 'http' or origin.hostname not in {'web', 'main', 'localhost', '127.0.0.1'} or origin.path not in {'', '/'} or origin.username or origin.password:
        logger.error('callback_origin_rejected')
        return False
    secret = settings.CALLBACK_HMAC_KEY
    if len(secret) < 32:
        return False
    with transaction.atomic():
        at = now()
        event = ProviderEvent.objects.select_for_update(skip_locked=True).filter(delivered=False, status='READY', due_at__lte=at).order_by('due_at', 'sequence').first()
        if not event:
            return False
        event.attempts += 1
        event.due_at = at + timedelta(seconds=30)
        event.save(update_fields=['attempts', 'due_at'])
        sequence, body, created_at, attempts = event.sequence, event.body, event.created_at, event.attempts
    path = '/callbacks/processor/v1/'
    raw = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    headers = signed_headers('POST', path, raw, secret,settings.CALLBACK_KEY_ID)
    headers['Content-Type'] = 'application/json'
    accepted = False
    try:
        request = Request(settings.CALLBACK_ORIGIN.rstrip('/')+path, data=raw, headers=headers, method='POST')
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=2) as response:
            accepted = response.status == 202
    except Exception as error:
        logger.info('callback_delivery_deferred', extra={'event_sequence': sequence, 'error_class': type(error).__name__})
    with transaction.atomic():
        at = now()
        event = ProviderEvent.objects.select_for_update().get(sequence=sequence)
        if event.delivered:
            return True
        if accepted:
            event.delivered = True
            event.status = 'DELIVERED'
        elif at-created_at >= timedelta(hours=24):
            event.status = 'NEEDS_REVIEW'
        else:
            event.due_at = at + timedelta(seconds=BACKOFF[min(attempts-1, len(BACKOFF)-1)] if attempts <= 5 else 300)
        event.save(update_fields=['delivered', 'status', 'due_at'])
    return True


def tick(stop_requested=lambda: False):
    close_old_connections()
    expire_authorizations()
    progress_disputes()
    daily_settlements()
    for _ in range(50):
        if stop_requested() or not deliver_one():
            break
    close_old_connections()


def run_scheduler(stopping, interval=0.5, once=False):
    """Retry durable duties after dependency loss without changing their identity."""
    failures = 0
    try:
        while not stopping.is_set():
            delay = interval
            try:
                tick(stopping.is_set)
            except DEPENDENCY_ERRORS as error:
                # A one-shot operator invocation must report that its duty failed.
                if once:
                    raise
                failures += 1
                delay = DEPENDENCY_BACKOFF[min(failures - 1, len(DEPENDENCY_BACKOFF) - 1)]
                # Messages/tracebacks can contain DSNs, callback tokens or payloads.
                logger.warning('simulator_iteration_deferred error_class=%s attempts=%d retry_seconds=%s',
                               type(error).__name__[:80], failures, delay)
                try:
                    connections.close_all()
                except (OperationalError, InterfaceError):
                    logger.warning('simulator_connection_cleanup_deferred')
            else:
                failures = 0
            if once:
                break
            stopping.wait(delay)
    finally:
        connections.close_all()
