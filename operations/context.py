from .models import Control,Notification
from .notifications import visible_notifications

def navigation(request):
    if not request.user.is_authenticated:return {'public_checkout':request.path.startswith('/checkout/')}
    control=Control.objects.first()
    memberships=request.user.memberships.filter(active=True).select_related('merchant').order_by('merchant__name')
    match=getattr(request,'resolver_match',None)
    merchant_id=match.kwargs.get('merchant_id') if match else None
    member=memberships.filter(merchant_id=merchant_id).first() if merchant_id else None
    role=member.role if member else None
    return {'can_finance':role in ('OWNER','FINANCE'),'can_owner':role=='OWNER','public_checkout':request.path.startswith('/checkout/'),'nav_memberships':memberships,'recovery_hold':control.restore_hold if control else True,'unread_count':visible_notifications(request.user).filter(read_at=None).count(),'step_up_reference':request.session.get('step_up_reference','')}
