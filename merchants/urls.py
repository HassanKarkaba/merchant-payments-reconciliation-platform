from django.urls import path
from . import views
app_name='merchants'
urlpatterns=[path('<uuid:merchant_id>/',views.overview,name='overview'),path('<uuid:merchant_id>/settings/',views.settings,name='settings'),path('<uuid:merchant_id>/team/',views.team,name='team'),path('<uuid:merchant_id>/team/invite/',views.invite,name='invite'),path('<uuid:merchant_id>/team/<uuid:membership_id>/change/',views.change_member,name='change_member'),path('<uuid:merchant_id>/request-close/',views.request_close,name='request_close')]
