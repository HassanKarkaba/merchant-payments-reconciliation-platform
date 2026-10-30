import time
from django.contrib.auth import logout
from django.http import JsonResponse
from django.shortcuts import redirect


class SessionAuthorityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        status_json = request.path.endswith('/status.json')
        if request.user.is_authenticated:
            now = time.time()
            expired = (not request.user.is_active or request.session.get('auth_epoch') != request.user.auth_epoch or now - request.session.get('last_activity', 0) >= 1800 or now - request.session.get('absolute_start', 0) >= 43200)
            if expired:
                logout(request)
                if status_json:
                    return self.unauthenticated(request)
                if not request.path.startswith(('/accounts/', '/health/', '/callbacks/', '/static/')):
                    return redirect('/accounts/login/?next=' + request.path)
            else:
                request.session['last_activity'] = now
                if not request.path.startswith(('/accounts/', '/health/', '/static/')):
                    if not request.user.verified:
                        return self.unauthenticated(request) if status_json else redirect('/accounts/profile/')
                    if not request.user.totp_confirmed:
                        return self.unauthenticated(request) if status_json else redirect('/accounts/mfa/enroll/')
                    if not request.session.get('mfa_ok'):
                        return self.unauthenticated(request) if status_json else redirect('/accounts/mfa/')
        elif status_json:
            return self.unauthenticated(request)
        return self.get_response(request)

    @staticmethod
    def unauthenticated(request):
        return JsonResponse({'code': 'authentication_required', 'message': 'Sign in and complete your authenticator check to inspect this operation.', 'field_errors': {}, 'correlation_id': getattr(request, 'correlation_id', ''), 'inspection_url': None}, status=401)
