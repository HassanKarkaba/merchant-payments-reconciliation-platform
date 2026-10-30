from django.core.exceptions import PermissionDenied
from .domain import require_merchant as scoped

def require_merchant(request,merchant_id,roles=None):
    return scoped(request.user,merchant_id,roles)

def require_platform(request,roles):
    if not request.user.is_authenticated or not request.user.is_active or request.user.platform_role not in roles:
        raise PermissionDenied
    return request.user
