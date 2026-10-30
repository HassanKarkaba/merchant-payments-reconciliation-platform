import uuid
from django.http import HttpResponse
from .protection import RateLimited
class ProtectionMiddleware:
    def __init__(self,get_response):self.get_response=get_response
    def __call__(self,request):
        request.correlation_id=str(uuid.uuid4())
        maximum=6*1024*1024 if '/imports/' in request.path else 256*1024 if request.path.startswith('/callbacks/') else 64*1024
        try:length=int(request.META.get('CONTENT_LENGTH','0') or 0)
        except ValueError:length=maximum+1
        if length>maximum:return HttpResponse('Request is too large.',status=413)
        response=self.get_response(request)
        if request.method=='POST' and response.status_code==302:response.status_code=303
        response['Cache-Control']='no-store'
        response['Content-Security-Policy']="default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        response['Referrer-Policy']='same-origin'
        response['X-Correlation-ID']=request.correlation_id
        return response
    def process_exception(self,request,exception):
        if isinstance(exception,RateLimited):
            response=HttpResponse('Too many attempts. Wait before trying again.',status=429);response['Retry-After']='60';return response
