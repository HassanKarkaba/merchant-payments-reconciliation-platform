from django.urls import path
from . import views
app_name='accounting'
urlpatterns=[path('m/<uuid:merchant_id>/balances/',views.balance_view,name='balances'),path('m/<uuid:merchant_id>/ledger/',views.ledger,name='ledger'),path('m/<uuid:merchant_id>/reports/close/<uuid:report_id>/',views.close_report,name='close_report')]
