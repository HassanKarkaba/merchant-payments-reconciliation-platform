from django.urls import path
from . import views
app_name='disputes'
urlpatterns=[path('m/<uuid:merchant_id>/disputes/',views.list_view,name='list'),path('m/<uuid:merchant_id>/disputes/<uuid:dispute_id>/',views.detail,name='detail'),path('m/<uuid:merchant_id>/disputes/<uuid:dispute_id>/respond/',views.respond,name='respond')]
