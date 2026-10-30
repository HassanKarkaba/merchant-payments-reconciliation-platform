from django.urls import path
from . import views
app_name = 'settlements'
urlpatterns = [
    path('m/<uuid:merchant_id>/beneficiaries/new/', views.beneficiary_new, name='beneficiary_new'),
    path('m/<uuid:merchant_id>/beneficiaries/<uuid:proposal_id>/approve/', views.beneficiary_approve, name='beneficiary_approve'),
    path('m/<uuid:merchant_id>/payouts/', views.payout_list, name='payouts'),
    path('m/<uuid:merchant_id>/payouts/new/', views.payout_new, name='payout_new'),
    path('m/<uuid:merchant_id>/payouts/<uuid:payout_id>/', views.payout_detail, name='payout_detail'),
    path('m/<uuid:merchant_id>/payouts/<uuid:payout_id>/approve/', views.payout_approve, name='payout_approve'),
    path('m/<uuid:merchant_id>/payouts/<uuid:payout_id>/cancel/', views.payout_cancel, name='payout_cancel'),
    path('m/<uuid:merchant_id>/settlements/<uuid:batch_id>/', views.batch_detail, name='batch_detail'),
]
