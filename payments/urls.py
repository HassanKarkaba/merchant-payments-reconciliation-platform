from django.urls import path
from . import views
app_name = 'payments'
urlpatterns = [
    path('m/<uuid:merchant_id>/payments/', views.payment_list, name='list'),
    path('m/<uuid:merchant_id>/payments/<uuid:payment_id>/', views.payment_detail, name='detail'),
    path('m/<uuid:merchant_id>/links/new/', views.link_new, name='link_new'),
    path('m/<uuid:merchant_id>/links/issued/', views.link_issued, name='link_issued'),
    path('m/<uuid:merchant_id>/links/<uuid:link_id>/revoke/', views.link_revoke, name='link_revoke'),
    path('m/<uuid:merchant_id>/payments/<uuid:payment_id>/capture/', views.capture, name='capture'),
    path('m/<uuid:merchant_id>/payments/<uuid:payment_id>/void/', views.void, name='void'),
    path('m/<uuid:merchant_id>/payments/<uuid:payment_id>/refunds/new/', views.refund_new, name='refund_new'),
    path('checkout/<str:token>/', views.checkout, name='checkout'),
    path('checkout/<str:token>/confirm/', views.checkout_confirm, name='checkout_confirm'),
    path('checkout/result/<uuid:session_ref>/', views.checkout_result, name='checkout_result'),
]
