from django import template
register=template.Library()
@register.filter
def money(value):
    try:
        value=int(value or 0);return ('−' if value<0 else '')+'$'+format(abs(value)//100,',')+'.'+str(abs(value)%100).zfill(2)
    except (ValueError,TypeError):return '—'
register.filter('minor',money)
@register.filter
def signed_money(value):
    try:return ('+' if int(value or 0)>0 else '')+money(value)
    except (ValueError,TypeError):return '—'

@register.filter
def merchant_time(value,zone='UTC'):
    if not value:return 'Not available'
    from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
    try:target=ZoneInfo(zone or 'UTC')
    except (ZoneInfoNotFoundError,TypeError):target=ZoneInfo('UTC')
    return value.astimezone(target).strftime('%b %d, %Y %H:%M:%S %Z (UTC%z)')
