"""Bounded transport key rotation; infrastructure configuration never changes money policy."""
import json
from datetime import datetime,timezone
from django.core.exceptions import ImproperlyConfigured

def transport_keys(current_id,current_secret,previous_json='[]'):
    keys={current_id:{'secret':current_secret,'expires_at':None}}
    try:
        previous=json.loads(previous_json)
        if not isinstance(previous,list) or len(previous)>2:raise ValueError
        for item in previous:
            if set(item)!={'key_id','secret','activated_at','expires_at'}:raise ValueError
            start=datetime.fromisoformat(item['activated_at'].replace('Z','+00:00'));end=datetime.fromisoformat(item['expires_at'].replace('Z','+00:00'))
            if not start.tzinfo or not end.tzinfo or not 0<(end-start).total_seconds()<=86400 or len(item['secret'])<32 or item['key_id'] in keys:raise ValueError
            keys[item['key_id']]={'secret':item['secret'],'expires_at':end,'activated_at':start}
    except (ValueError,TypeError,KeyError):raise ImproperlyConfigured('Previous service keys require unique IDs and an explicit rotation interval of at most 24 hours') from None
    return keys

def verification_secret(keys,key_id):
    key=keys.get(key_id)
    if not key:return None
    now=datetime.now(timezone.utc)
    if key.get('expires_at') and (now>=key['expires_at'] or now<key['activated_at']):return None
    return key['secret']
