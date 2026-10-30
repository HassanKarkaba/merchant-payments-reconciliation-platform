import hashlib
from datetime import timedelta
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from .models import RateBucket
class RateLimited(Exception):pass

@transaction.atomic
def rate_limit(key,limit,seconds):
    key=hashlib.sha256(key.encode()).hexdigest();now=timezone.now()
    RateBucket.objects.get_or_create(key=key,defaults={'expires_at':now+timedelta(seconds=seconds)})
    bucket=RateBucket.objects.select_for_update().get(pk=key)
    if now>=bucket.expires_at:bucket.count=0;bucket.expires_at=now+timedelta(seconds=seconds)
    if bucket.count>=limit:raise RateLimited
    bucket.count+=1;bucket.save()


def _auth_keys(email, source_ip):
    return [('auth-account:' + str(email).strip().lower(), 5), ('auth-source:' + source_ip, 30)]


@transaction.atomic
def _auth_buckets(email, source_ip, increment):
    now = timezone.now()
    keys = sorted((hashlib.sha256(key.encode()).hexdigest(), limit) for key, limit in _auth_keys(email, source_ip))
    buckets = []
    for key, limit in keys:
        RateBucket.objects.get_or_create(key=key, defaults={'expires_at': now + timedelta(seconds=900)})
        bucket = RateBucket.objects.select_for_update().get(pk=key)
        if now >= bucket.expires_at:
            bucket.count = 0
            bucket.expires_at = now + timedelta(seconds=900)
            bucket.save(update_fields=['count', 'expires_at'])
        if bucket.count >= limit:
            raise RateLimited
        buckets.append(bucket)
    if increment:
        for bucket in buckets:
            bucket.count += 1
            bucket.save(update_fields=['count'])


def check_auth_budget(email, source_ip):
    return _auth_buckets(email, source_ip, False)


def record_auth_failure(email, source_ip):
    return _auth_buckets(email, source_ip, True)
